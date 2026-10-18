"""数据库预约基线：事务内校验时段、用户额度、幂等请求和防超卖约束。"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID

from campus_reserve.bookings.models import (
    Booking,
    BookingItem,
    BookingRequest,
    BookingStatusLog,
)
from campus_reserve.bookings.repositories import BookingRepository
from campus_reserve.inventory.models import BookingSlot
from campus_reserve.shared.enums import (
    BookingRequestStatus,
    BookingSlotStatus,
    BookingStatus,
    OperatorType,
    ReleaseBatchStatus,
)
from campus_reserve.users.models import UserDailyUsage

ERROR_MESSAGES = {
    "SLOTS_NOT_FOUND": "部分场地时段不存在",
    "INVALID_SLOT_COUNT": "一次只能选择一至两个时段",
    "SLOTS_MUST_BE_UNIQUE": "同一场地时段不能重复选择",
    "SLOTS_MUST_SHARE_DATE": "所选时段必须属于同一预约日期",
    "SLOTS_MUST_SHARE_BATCH": "所选时段必须属于同一放号批次",
    "BATCH_NOT_OPEN": "该批次尚未放号或已经关闭",
    "SLOT_NOT_AVAILABLE": "所选时段已被预约或不可用",
    "SLOT_EXPIRED": "所选时段已经开始，不能预约",
    "DAILY_LIMIT_EXCEEDED": "每天最多预约两个时段",
    "TIME_CONFLICT": "旧版本请求的时间冲突结果",
    "IDEMPOTENCY_CONFLICT": "同一请求编号不能提交不同的预约内容",
    "REQUEST_NOT_FOUND": "预约请求不存在",
    "BOOKING_NOT_FOUND": "预约不存在",
    "INVALID_EVENT_PAYLOAD": "预约事件异常，已终止处理",
    "PERSISTENCE_RETRY_EXHAUSTED": "预约处理失败，已终止重试",
}


class BookingRuleError(ValueError):
    def __init__(
        self,
        code: str,
        *,
        replayed: bool = False,
        request: BookingRequest | None = None,
    ) -> None:
        """保存业务错误码、是否重放及已有请求，供上层生成错误响应。"""
        self.code = code
        self.replayed = replayed
        self.request = request
        super().__init__(ERROR_MESSAGES.get(code, "预约失败"))


@dataclass(frozen=True, slots=True)
class BookingSubmission:
    request: BookingRequest
    replayed: bool


class SyncBookingService:
    def __init__(self, repository: BookingRepository) -> None:
        """注入预约仓储，使业务规则与数据库实现分离。"""
        self.repository = repository

    # 事务中锁定请求、用量和库存；数据库约束是最终防超卖保护。
    async def submit(
        self,
        user_id: int,
        request_id: UUID,
        slot_ids: list[int],
        now: datetime | None = None,
    ) -> BookingSubmission:
        """事务内完成幂等、日期批次、库存、时间及每日额度校验，并创建正式预约。"""
        requested_ids = sorted(slot_ids)
        if not 1 <= len(requested_ids) <= 2:
            raise BookingRuleError("INVALID_SLOT_COUNT")
        if len(set(requested_ids)) != len(requested_ids):
            raise BookingRuleError("SLOTS_MUST_BE_UNIQUE")

        await self.repository.acquire_request_lock(request_id)
        existing = await self.repository.get_request(request_id)
        if existing is not None:
            if existing.user_id != user_id or sorted(existing.slot_ids) != requested_ids:
                raise BookingRuleError("IDEMPOTENCY_CONFLICT", replayed=True)
            if existing.status is BookingRequestStatus.FAILED:
                raise BookingRuleError(
                    existing.error_code or "SLOT_NOT_AVAILABLE",
                    replayed=True,
                    request=existing,
                )
            return BookingSubmission(existing, replayed=True)

        preliminary_slots = await self.repository.get_slots(requested_ids, for_update=False)
        service_date, release_batch_id = self._validate_slot_shape(
            preliminary_slots, requested_ids
        )

        await self.repository.acquire_user_date_lock(user_id, service_date)
        slots = await self.repository.get_slots(requested_ids, for_update=True)
        service_date, release_batch_id = self._validate_slot_shape(slots, requested_ids)

        if slots[0].release_batch.status is not ReleaseBatchStatus.OPEN:
            await self._record_failure(
                user_id, request_id, release_batch_id, requested_ids, "BATCH_NOT_OPEN", now
            )
        if any(slot.status is not BookingSlotStatus.AVAILABLE for slot in slots):
            await self._record_failure(
                user_id, request_id, release_batch_id, requested_ids, "SLOT_NOT_AVAILABLE", now
            )
        if any(slot.start_at <= (now or datetime.now(UTC)) for slot in slots):
            await self._record_failure(
                user_id, request_id, release_batch_id, requested_ids, "SLOT_EXPIRED", now
            )

        usage = await self.repository.get_daily_usage(user_id, service_date)
        used_slot_count = usage.used_slot_count if usage else 0
        if used_slot_count + len(slots) > 2:
            await self._record_failure(
                user_id,
                request_id,
                release_batch_id,
                requested_ids,
                "DAILY_LIMIT_EXCEEDED",
                now,
            )

        confirmed_at = now or datetime.now(UTC)
        request = BookingRequest(
            request_id=request_id,
            user_id=user_id,
            release_batch_id=release_batch_id,
            slot_ids=requested_ids,
            status=BookingRequestStatus.PROCESSING,
        )
        await self.repository.add_request(request)

        booking = Booking(
            request_id=request_id,
            booking_no=f"CR-{service_date:%Y%m%d}-{request_id.hex[:12].upper()}",
            user_id=user_id,
            venue_id=slots[0].release_batch.venue_id,
            status=BookingStatus.CONFIRMED,
            confirmed_at=confirmed_at,
            rule_snapshot=slots[0].release_batch.rule_snapshot,
        )
        await self.repository.add_booking(booking)

        items = [
            BookingItem(
                booking=booking,
                slot=slot,
                user_id=user_id,
                service_date=slot.service_date,
                start_at=slot.start_at,
                end_at=slot.end_at,
            )
            for slot in slots
        ]
        await self.repository.add_items(items)
        for slot in slots:
            slot.status = BookingSlotStatus.BOOKED
            slot.current_booking_id = booking.id
            slot.version += 1

        if usage is None:
            usage = UserDailyUsage(
                user_id=user_id,
                service_date=service_date,
                used_slot_count=0,
                version=0,
            )
            await self.repository.add_daily_usage(usage)
        usage.used_slot_count = used_slot_count + len(slots)
        usage.version += 1

        request.status = BookingRequestStatus.CONFIRMED
        request.booking_id = booking.id
        request.finished_at = confirmed_at
        request.booking = booking
        await self.repository.add_status_log(
            BookingStatusLog(
                booking_id=booking.id,
                from_status=None,
                to_status=BookingStatus.CONFIRMED,
                operator_type=OperatorType.SYSTEM,
                reason="同步数据库预约原型确认",
                trace_id=str(request_id),
            )
        )
        await self.repository.flush()
        persisted = await self.repository.get_request(request_id)
        if persisted is None:  # pragma: no cover - guarded by the transaction above
            raise RuntimeError("预约结果写入后无法读取")
        return BookingSubmission(persisted, replayed=False)

    async def get_request(self, user_id: int, request_id: UUID) -> BookingRequest:
        """仅按所属学生读取请求编号对应的数据库结果。"""
        request = await self.repository.get_request(request_id)
        if request is None or request.user_id != user_id:
            raise BookingRuleError("REQUEST_NOT_FOUND")
        return request

    async def list_bookings(self, user_id: int) -> list[Booking]:
        """列出指定学生已经保存到数据库的预约。"""
        return await self.repository.list_user_bookings(user_id)

    async def get_booking(self, user_id: int, booking_id: int) -> Booking:
        """读取学生本人的预约详情，不存在或不属于本人时抛出业务错误。"""
        booking = await self.repository.get_user_booking(user_id, booking_id)
        if booking is None:
            raise BookingRuleError("BOOKING_NOT_FOUND")
        return booking

    # 把可确定的失败写入请求记录，供客户端用原 UUID 重查。
    async def _record_failure(
        self,
        user_id: int,
        request_id: UUID,
        release_batch_id: int,
        slot_ids: list[int],
        error_code: str,
        now: datetime | None,
    ) -> None:
        """保存可确定的失败请求及错误码，供客户端用原 UUID 查询。"""
        failed_at = now or datetime.now(UTC)
        request = BookingRequest(
            request_id=request_id,
            user_id=user_id,
            release_batch_id=release_batch_id,
            slot_ids=slot_ids,
            status=BookingRequestStatus.FAILED,
            error_code=error_code,
            finished_at=failed_at,
        )
        await self.repository.add_request(request)
        raise BookingRuleError(error_code, request=request)

    # 单次 1～2 个同日同批次时段；允许同时间不同场地。
    @staticmethod
    def _validate_slot_shape(
        slots: list[BookingSlot], requested_ids: list[int]
    ) -> tuple[date, int]:
        """检查所选时段存在且属于同一使用日期和同一放号批次。"""
        if len(slots) != len(requested_ids):
            raise BookingRuleError("SLOTS_NOT_FOUND")
        service_dates = {slot.service_date for slot in slots}
        if len(service_dates) != 1:
            raise BookingRuleError("SLOTS_MUST_SHARE_DATE")
        release_batch_ids = {slot.release_batch_id for slot in slots}
        if len(release_batch_ids) != 1:
            raise BookingRuleError("SLOTS_MUST_SHARE_BATCH")
        return next(iter(service_dates)), next(iter(release_batch_ids))
