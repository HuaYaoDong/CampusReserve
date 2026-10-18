"""Redis 预约适配服务：组织键与 Lua 参数，返回原子占位和补偿结果。"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from uuid import UUID

from redis.asyncio import Redis

LUA_DIRECTORY = Path(__file__).parents[1] / "infrastructure" / "lua"
INITIALIZE_INVENTORY_LUA = (LUA_DIRECTORY / "initialize_inventory.lua").read_text(
    encoding="utf-8"
)
RESERVE_SLOTS_LUA = (LUA_DIRECTORY / "reserve_slots.lua").read_text(encoding="utf-8")
COMPENSATE_RESERVATION_LUA = (LUA_DIRECTORY / "compensate_reservation.lua").read_text(
    encoding="utf-8"
)


class RedisInventoryCode(StrEnum):
    INITIALIZED = "INITIALIZED"
    INVENTORY_CONFLICT = "INVENTORY_CONFLICT"
    REDIS_STATE_INVALID = "REDIS_STATE_INVALID"


class RedisReservationCode(StrEnum):
    RESERVED = "RESERVED"
    INVALID_SLOT_COUNT = "INVALID_SLOT_COUNT"
    SLOTS_MUST_BE_UNIQUE = "SLOTS_MUST_BE_UNIQUE"
    SLOTS_NOT_FOUND = "SLOTS_NOT_FOUND"
    SLOT_NOT_AVAILABLE = "SLOT_NOT_AVAILABLE"
    DAILY_LIMIT_EXCEEDED = "DAILY_LIMIT_EXCEEDED"
    TIME_CONFLICT = "TIME_CONFLICT"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    BATCH_DATE_MISMATCH = "BATCH_DATE_MISMATCH"
    BATCH_NOT_OPEN = "BATCH_NOT_OPEN"
    REDIS_STATE_INVALID = "REDIS_STATE_INVALID"


class RedisSlotState(StrEnum):
    AVAILABLE = "AVAILABLE"
    BOOKED = "BOOKED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class RedisInventorySlot:
    slot_id: int
    period_token: str
    state: RedisSlotState = RedisSlotState.AVAILABLE


@dataclass(frozen=True, slots=True)
class RedisConfirmedUsage:
    user_id: int
    period_tokens: tuple[str, ...]

    @property
    def used_slot_count(self) -> int:
        """根据已确认的时段标识计算学生已使用的额度。"""
        return len(self.period_tokens)


@dataclass(frozen=True, slots=True)
class RedisInventoryResult:
    code: RedisInventoryCode
    replayed: bool
    slot_count: int


@dataclass(frozen=True, slots=True)
class RedisReservationCommand:
    request_id: UUID
    user_id: int
    batch_id: int
    service_date: date
    slot_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class RedisReservationResult:
    code: RedisReservationCode
    replayed: bool
    used_slot_count: int
    stream_id: str | None

    @property
    def reserved(self) -> bool:
        """判断 Redis 返回的结果是否为成功占位。"""
        return self.code is RedisReservationCode.RESERVED


@dataclass(frozen=True, slots=True)
class RedisCompensationResult:
    code: str
    replayed: bool
    released_slot_count: int
    used_slot_count: int


# 相同 hash tag 让 Lua 涉及的键落在一个 Redis Cluster 槽位。
class RedisReservationKeyspace:
    def __init__(self, prefix: str = "campus-reserve:{booking}") -> None:
        """校验并保存带 hash tag 的 Redis 键前缀，保证相关键可用于同一脚本。"""
        if "{" not in prefix or "}" not in prefix:
            raise ValueError("Redis key prefix must contain a hash tag")
        self.prefix = prefix.rstrip(":")

    def slots(self, batch_id: int) -> str:
        """生成某批次场地库存状态的 Redis 键。"""
        return f"{self.prefix}:batch:{batch_id}:slots"

    def periods(self, batch_id: int) -> str:
        """生成某批次时段元数据及开放门闩的 Redis 键。"""
        return f"{self.prefix}:batch:{batch_id}:periods"

    def inventory_marker(self, batch_id: int) -> str:
        """生成某批次库存初始化标记的 Redis 键。"""
        return f"{self.prefix}:batch:{batch_id}:initialized"

    def usage(self, batch_id: int, user_id: int) -> str:
        """生成某学生在某批次已占用数量的 Redis 键。"""
        return f"{self.prefix}:batch:{batch_id}:user:{user_id}:usage"

    def user_periods(self, batch_id: int, user_id: int) -> str:
        """生成某学生在某批次已占用时段集合的 Redis 键。"""
        return f"{self.prefix}:batch:{batch_id}:user:{user_id}:periods"

    def request(self, request_id: UUID) -> str:
        """生成请求 UUID 对应的临时结果 Redis 键。"""
        return f"{self.prefix}:request:{request_id}"

    @property
    def stream(self) -> str:
        """返回预约事件 Stream 的 Redis 键。"""
        return f"{self.prefix}:reservation-events"


class RedisAtomicReservationService:
    def __init__(
        self,
        redis: Redis,
        *,
        keyspace: RedisReservationKeyspace | None = None,
        ttl_seconds: int = 7 * 24 * 60 * 60,
        max_daily_slots: int = 2,
    ) -> None:
        """注册库存初始化、预约占位和失败补偿脚本，并保存额度及过期配置。"""
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be positive")
        if max_daily_slots < 1:
            raise ValueError("max_daily_slots must be positive")
        self.redis = redis
        self.keyspace = keyspace or RedisReservationKeyspace()
        self.ttl_seconds = ttl_seconds
        self.max_daily_slots = max_daily_slots
        self._initialize_script = redis.register_script(INITIALIZE_INVENTORY_LUA)
        self._reserve_script = redis.register_script(RESERVE_SLOTS_LUA)
        self._compensate_script = redis.register_script(COMPENSATE_RESERVATION_LUA)

    # 用批次快照初始化 Redis；相同快照重试不得覆盖已有占位。
    async def initialize_inventory(
        self,
        batch_id: int,
        service_date: date,
        slots: list[RedisInventorySlot],
        confirmed_usages: list[RedisConfirmedUsage] | None = None,
    ) -> RedisInventoryResult:
        """用数据库批次快照初始化 Redis 库存；相同快照重试不会覆盖已有占位。"""
        normalized = sorted(slots, key=lambda slot: slot.slot_id)
        normalized_usages = sorted(confirmed_usages or [], key=lambda usage: usage.user_id)
        payload = [
            {"slot_id": slot.slot_id, "state": slot.state, "period": slot.period_token}
            for slot in normalized
        ]
        usage_payload = [
            {
                "user_id": usage.user_id,
                "period_tokens": tuple(sorted(usage.period_tokens)),
            }
            for usage in normalized_usages
        ]
        fingerprint = _fingerprint(
            {
                "batch_id": batch_id,
                "service_date": service_date.isoformat(),
                "slots": payload,
                "confirmed_usages": usage_payload,
            }
        )
        args: list[str | int] = [
            fingerprint,
            self.ttl_seconds,
            service_date.isoformat(),
            len(normalized),
            len(normalized_usages),
        ]
        for slot in normalized:
            args.extend((slot.slot_id, slot.state.value, slot.period_token))
        for usage in normalized_usages:
            periods = tuple(sorted(usage.period_tokens))
            args.extend((usage.user_id, len(periods), len(periods), *periods))
        keys = [
            self.keyspace.slots(batch_id),
            self.keyspace.periods(batch_id),
            self.keyspace.inventory_marker(batch_id),
        ]
        for usage in normalized_usages:
            keys.extend(
                (
                    self.keyspace.usage(batch_id, usage.user_id),
                    self.keyspace.user_periods(batch_id, usage.user_id),
                )
            )
        response = await self._initialize_script(
            keys=keys,
            args=args,
        )
        return RedisInventoryResult(
            code=RedisInventoryCode(response[0]),
            replayed=response[1] == "1",
            slot_count=int(response[2]),
        )

    # 一次 Lua 调用同时检查额度、占库存和写入待处理事件。
    async def reserve(self, command: RedisReservationCommand) -> RedisReservationResult:
        """调用 Lua 一次完成规则检查、库存占位、额度更新和 Stream 入队。"""
        slot_ids = tuple(sorted(command.slot_ids))
        fingerprint = reservation_fingerprint(command)
        response = await self._reserve_script(
            keys=[
                self.keyspace.slots(command.batch_id),
                self.keyspace.periods(command.batch_id),
                self.keyspace.usage(command.batch_id, command.user_id),
                self.keyspace.user_periods(command.batch_id, command.user_id),
                self.keyspace.request(command.request_id),
                self.keyspace.stream,
            ],
            args=[
                fingerprint,
                str(command.request_id),
                command.user_id,
                command.batch_id,
                command.service_date.isoformat(),
                self.max_daily_slots,
                self.ttl_seconds,
                len(slot_ids),
                *slot_ids,
            ],
        )
        return RedisReservationResult(
            code=RedisReservationCode(response[0]),
            replayed=response[1] == "1",
            used_slot_count=int(response[2]),
            stream_id=response[3] or None,
        )

    # 只释放仍由该请求持有的占位，避免误放其他请求的库存。
    async def compensate(
        self,
        command: RedisReservationCommand,
        error_code: str,
        *,
        compensated_at: datetime | None = None,
    ) -> RedisCompensationResult:
        """数据库确认失败时，仅释放仍由本请求持有的库存及额度。"""
        slot_ids = tuple(sorted(command.slot_ids))
        response = await self._compensate_script(
            keys=[
                self.keyspace.slots(command.batch_id),
                self.keyspace.periods(command.batch_id),
                self.keyspace.usage(command.batch_id, command.user_id),
                self.keyspace.user_periods(command.batch_id, command.user_id),
                self.keyspace.request(command.request_id),
            ],
            args=[
                str(command.request_id),
                command.user_id,
                self.ttl_seconds,
                error_code,
                (compensated_at or datetime.now(UTC)).isoformat(),
                len(slot_ids),
                *slot_ids,
            ],
        )
        return RedisCompensationResult(
            code=response[0],
            replayed=response[1] == "1",
            released_slot_count=int(response[2]),
            used_slot_count=int(response[3]),
        )


def _fingerprint(value: object) -> str:
    """把结构化数据规范化并计算 SHA-256 指纹，供幂等比较。"""
    serialized = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()


# 请求指纹用于识别同 UUID 但内容不同的幂等冲突。
def reservation_fingerprint(command: RedisReservationCommand) -> str:
    """将预约命令的关键字段组成指纹，识别同 UUID 的不同请求内容。"""
    return _fingerprint(
        {
            "request_id": str(command.request_id),
            "user_id": command.user_id,
            "batch_id": command.batch_id,
            "service_date": command.service_date.isoformat(),
            "slot_ids": tuple(sorted(command.slot_ids)),
        }
    )
