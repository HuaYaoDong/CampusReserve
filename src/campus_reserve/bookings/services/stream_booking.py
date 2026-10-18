"""异步确认链路：解析 Stream 事件、数据库幂等落库、重试与补偿。"""

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import ResponseError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from campus_reserve.bookings.infrastructure.sqlalchemy_repository import (
    SqlAlchemyBookingRepository,
)
from campus_reserve.bookings.models import BookingRequest
from campus_reserve.bookings.services.redis_reservation import (
    RedisAtomicReservationService,
    RedisReservationCommand,
    RedisReservationKeyspace,
    reservation_fingerprint,
)
from campus_reserve.bookings.services.sync_booking import BookingRuleError, SyncBookingService
from campus_reserve.shared.enums import BookingRequestStatus


class BookingEventPayloadError(ValueError):
    pass


class BookingPersistenceStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    DEAD_LETTERED = "DEAD_LETTERED"


@dataclass(frozen=True, slots=True)
class BookingReservationEvent:
    command: RedisReservationCommand
    fingerprint: str

    # 不信任 Stream 原始字段；落库前验证 UUID、日期、时段和指纹。
    @classmethod
    def from_payload(cls, payload: dict[str, str]) -> "BookingReservationEvent":
        """解析并校验 Stream 消息的字段和指纹，避免错误事件进入数据库。"""
        try:
            slot_ids = tuple(int(value) for value in payload["slot_ids"].split(","))
            command = RedisReservationCommand(
                request_id=UUID(payload["request_id"]),
                user_id=int(payload["user_id"]),
                batch_id=int(payload["batch_id"]),
                service_date=date.fromisoformat(payload["service_date"]),
                slot_ids=slot_ids,
            )
            fingerprint = payload["fingerprint"]
        except (KeyError, TypeError, ValueError) as exc:
            raise BookingEventPayloadError("预约事件字段缺失或格式错误") from exc
        if command.user_id < 1 or command.batch_id < 1 or not slot_ids:
            raise BookingEventPayloadError("预约事件标识或时段列表无效")
        if reservation_fingerprint(command) != fingerprint:
            raise BookingEventPayloadError("预约事件指纹不匹配")
        return cls(command=command, fingerprint=fingerprint)


@dataclass(frozen=True, slots=True)
class BookingPersistenceResult:
    request_id: UUID
    status: BookingPersistenceStatus
    replayed: bool
    command: RedisReservationCommand | None = None
    booking_id: int | None = None
    booking_no: str | None = None
    error_code: str | None = None


class BookingEventProcessor:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        """保存数据库会话工厂，供每条事件使用独立事务。"""
        self.sessions = sessions

    # 数据库事务再次校验预约规则；Redis 抢占不是最终事实。
    async def process(self, payload: dict[str, str]) -> BookingPersistenceResult:
        """在数据库事务中重新校验并确认预约，返回可写回 Redis 的结果。"""
        event = BookingReservationEvent.from_payload(payload)
        command = event.command
        async with self.sessions() as session, session.begin():
            service = SyncBookingService(SqlAlchemyBookingRepository(session))
            try:
                submission = await service.submit(
                    command.user_id,
                    command.request_id,
                    list(command.slot_ids),
                )
            except BookingRuleError as exc:
                return BookingPersistenceResult(
                    request_id=command.request_id,
                    status=BookingPersistenceStatus.FAILED,
                    replayed=exc.replayed,
                    command=command,
                    error_code=exc.code,
                )

            request = submission.request
            booking = request.booking
            if request.release_batch_id != command.batch_id:
                raise BookingEventPayloadError("预约事件批次与数据库库存不一致")
            if booking is None or any(
                item.service_date != command.service_date for item in booking.items
            ):
                raise BookingEventPayloadError("预约事件日期与数据库库存不一致")
            return BookingPersistenceResult(
                request_id=command.request_id,
                status=BookingPersistenceStatus.CONFIRMED,
                replayed=submission.replayed,
                command=command,
                booking_id=booking.id,
                booking_no=booking.booking_no,
            )

    # 处理提交成功但确认 Stream 前中断的窗口，以数据库结果恢复状态。
    async def reconcile(
        self, command: RedisReservationCommand, error_code: str
    ) -> BookingPersistenceResult:
        """Read the committed outcome before releasing an uncertain Redis claim."""
        async with self.sessions() as session, session.begin():
            repository = SqlAlchemyBookingRepository(session)
            await repository.acquire_request_lock(command.request_id)
            request = await repository.get_request(command.request_id)
            if request is None:
                finished_at = datetime.now(UTC)
                await repository.add_request(
                    BookingRequest(
                        request_id=command.request_id,
                        user_id=command.user_id,
                        release_batch_id=command.batch_id,
                        slot_ids=sorted(command.slot_ids),
                        status=BookingRequestStatus.FAILED,
                        error_code=error_code,
                        accepted_at=finished_at,
                        finished_at=finished_at,
                    )
                )
                return BookingPersistenceResult(
                    request_id=command.request_id,
                    status=BookingPersistenceStatus.FAILED,
                    replayed=False,
                    command=command,
                    error_code=error_code,
                )
            if (
                request.user_id != command.user_id
                or request.release_batch_id != command.batch_id
                or sorted(request.slot_ids) != sorted(command.slot_ids)
            ):
                raise BookingEventPayloadError("预约事件与数据库请求不一致")
            if request.status is BookingRequestStatus.CONFIRMED and request.booking is not None:
                if any(
                    item.service_date != command.service_date for item in request.booking.items
                ):
                    raise BookingEventPayloadError("预约事件日期与数据库库存不一致")
                return BookingPersistenceResult(
                    request_id=command.request_id,
                    status=BookingPersistenceStatus.CONFIRMED,
                    replayed=True,
                    command=command,
                    booking_id=request.booking.id,
                    booking_no=request.booking.booking_no,
                )
            if request.status is BookingRequestStatus.FAILED:
                return BookingPersistenceResult(
                    request_id=command.request_id,
                    status=BookingPersistenceStatus.FAILED,
                    replayed=True,
                    command=command,
                    error_code=request.error_code or error_code,
                )
            raise RuntimeError("预约数据库结果仍在处理中")


class BookingEventHandler(Protocol):
    # 处理原始预约事件并返回可持久化的最终结果。
    async def process(self, payload: dict[str, str]) -> BookingPersistenceResult: ...

    # 异常耗尽时核对数据库已提交结果，避免错误释放成功预约。
    async def reconcile(
        self, command: RedisReservationCommand, error_code: str
    ) -> BookingPersistenceResult: ...


class BookingStreamConsumer:
    def __init__(
        self,
        redis: Redis,
        processor: BookingEventHandler,
        *,
        stream: str,
        dead_letter_stream: str,
        group: str,
        consumer_name: str,
        keyspace: RedisReservationKeyspace | None = None,
        result_ttl_seconds: int = 7 * 24 * 60 * 60,
        max_deliveries: int = 5,
    ) -> None:
        """组装 Redis 消费组、事件处理器、补偿服务和重试配置。"""
        if max_deliveries < 1:
            raise ValueError("max_deliveries must be positive")
        self.redis = redis
        self.processor = processor
        self.stream = stream
        self.dead_letter_stream = dead_letter_stream
        self.group = group
        self.consumer_name = consumer_name
        self.keyspace = keyspace or RedisReservationKeyspace()
        self.result_ttl_seconds = result_ttl_seconds
        self.max_deliveries = max_deliveries
        self.reservations = RedisAtomicReservationService(
            redis,
            keyspace=self.keyspace,
            ttl_seconds=result_ttl_seconds,
        )

    async def ensure_group(self) -> None:
        """创建预约 Stream 消费组；消费组已存在时继续使用。"""
        try:
            await self.redis.xgroup_create(
                name=self.stream,
                groupname=self.group,
                id="0",
                mkstream=True,
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def read_new(self, *, count: int = 10, block_ms: int = 5_000):
        """通过消费组阻塞读取新的预约事件。"""
        messages = await self.redis.xreadgroup(
            groupname=self.group,
            consumername=self.consumer_name,
            streams={self.stream: ">"},
            count=count,
            block=block_ms,
        )
        return [entry for _, entries in messages for entry in entries]

    # 重领长时间未确认的消息，使 Worker 中断后仍能继续处理。
    async def claim_stale(self, *, min_idle_ms: int, count: int = 10):
        """重新领取超时未确认事件，让其他 Worker 接续处理。"""
        response = await self.redis.xautoclaim(
            name=self.stream,
            groupname=self.group,
            consumername=self.consumer_name,
            min_idle_time=min_idle_ms,
            start_id="0-0",
            count=count,
        )
        return response[1]

    # 消息处理必须幂等；异常路径交由重试、补偿或死信处理。
    async def process_message(
        self,
        message_id: str,
        payload: dict[str, str],
    ) -> BookingPersistenceResult:
        """处理一条事件；失败时重试，必要时补偿并写入死信流。"""
        try:
            result = await self.processor.process(payload)
        except BookingEventPayloadError as exc:
            return await self._resolve_and_dead_letter(
                message_id, payload, "INVALID_EVENT_PAYLOAD", str(exc)
            )
        except Exception as exc:
            pending = await self.redis.xpending_range(
                self.stream, self.group, min=message_id, max=message_id, count=1
            )
            deliveries = pending[0]["times_delivered"] if pending else 0
            if deliveries < self.max_deliveries:
                raise
            return await self._resolve_and_dead_letter(
                message_id,
                payload,
                "PERSISTENCE_RETRY_EXHAUSTED",
                f"{type(exc).__name__} after {deliveries} deliveries",
            )

        if result.status is BookingPersistenceStatus.FAILED and result.command is not None:
            compensation = await self.reservations.compensate(
                result.command,
                result.error_code or "DATABASE_PERSISTENCE_FAILED",
            )
            if compensation.code != "RELEASED":
                raise RuntimeError(f"预约补偿失败: {compensation.code}")

        await self._finish_message(message_id, result)
        return result

    # 确认或补偿完成后再 ACK，防止尚未落定的请求丢失。
    async def _finish_message(
        self,
        message_id: str,
        result: BookingPersistenceResult,
        *,
        dead_letter: tuple[dict[str, str], str] | None = None,
    ) -> None:

        """将最终结果写回 Redis，并在同一管道中确认 Stream 消息。"""
        request_key = self.keyspace.request(result.request_id)
        mapping = {
            "persistence_status": result.status.value,
            "persistence_replayed": "1" if result.replayed else "0",
            "persistence_error_code": result.error_code or "",
            "booking_id": str(result.booking_id or ""),
            "booking_no": result.booking_no or "",
            "persisted_at": datetime.now(UTC).isoformat(),
        }
        async with self.redis.pipeline(transaction=True) as pipeline:
            if result.command is not None:
                pipeline.hset(request_key, mapping=mapping)
                pipeline.expire(request_key, self.result_ttl_seconds)
            if dead_letter is not None:
                payload, error = dead_letter
                pipeline.xadd(
                    self.dead_letter_stream,
                    {
                        "source_message_id": message_id,
                        "error": error,
                        "payload": json.dumps(payload, ensure_ascii=False, sort_keys=True),
                        "resolution": result.status.value,
                        "failed_at": datetime.now(UTC).isoformat(),
                    },
                )
            pipeline.xack(self.stream, self.group, message_id)
            await pipeline.execute()

    # 超过重试上限时保留可追查的死信和最终失败原因。
    async def _resolve_and_dead_letter(
        self,
        message_id: str,
        payload: dict[str, str],
        error_code: str,
        error: str,
    ) -> BookingPersistenceResult:
        """达到重试上限后核对数据库结果、补偿失败占位并记录死信。"""
        command = await self._reserved_command_for_message(message_id, payload)
        if command is None:
            result = BookingPersistenceResult(
                request_id=_safe_request_id(payload),
                status=BookingPersistenceStatus.DEAD_LETTERED,
                replayed=False,
                error_code=error_code,
            )
        else:
            result = await self.processor.reconcile(command, error_code)
            if result.status is BookingPersistenceStatus.FAILED:
                compensation = await self.reservations.compensate(
                    command, result.error_code or error_code
                )
                if compensation.code != "RELEASED":
                    raise RuntimeError(f"预约补偿失败: {compensation.code}")
        await self._finish_message(message_id, result, dead_letter=(payload, error))
        return result

    async def _reserved_command_for_message(
        self, message_id: str, payload: dict[str, str]
    ) -> RedisReservationCommand | None:
        """只从匹配消息 ID 的有效 Redis 占位中恢复原始预约命令。"""
        request_id = _safe_request_id(payload)
        if request_id.int == 0:
            return None
        state = await self.redis.hgetall(self.keyspace.request(request_id))
        if state.get("code") != "RESERVED" or state.get("stream_id") != message_id:
            return None
        try:
            return RedisReservationCommand(
                request_id=request_id,
                user_id=int(state["user_id"]),
                batch_id=int(state["batch_id"]),
                service_date=date.fromisoformat(state["service_date"]),
                slot_ids=tuple(int(value) for value in state["slot_ids"].split(",")),
            )
        except (KeyError, ValueError) as exc:
            raise RuntimeError("预约占位状态无法解析，需要人工处理") from exc


def _safe_request_id(payload: dict[str, str]) -> UUID:
    """安全解析消息中的请求 UUID；无效值返回全零 UUID。"""
    try:
        return UUID(payload.get("request_id", ""))
    except (TypeError, ValueError):
        return UUID(int=0)
