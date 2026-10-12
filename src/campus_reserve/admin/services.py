from datetime import UTC, date, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from redis.asyncio import Redis
from sqlalchemy import and_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from campus_reserve.admin.models import AuditLog
from campus_reserve.admin.schemas import (
    AdminBookingView,
    AdminOverview,
    AuditView,
    BatchView,
    CheckInRequest,
    CheckInView,
    CourtPatch,
    CourtView,
    ExceptionUpsert,
    ExceptionView,
    GenerationPreview,
    TemplateCreate,
    TemplatePatch,
    TemplateView,
    VenuePatch,
    VenueView,
)
from campus_reserve.auth.security import AuthPrincipal
from campus_reserve.bookings.models import Booking, BookingItem, BookingStatusLog
from campus_reserve.bookings.services.redis_reservation import (
    RedisAtomicReservationService,
    RedisConfirmedUsage,
    RedisInventoryCode,
    RedisInventorySlot,
    RedisReservationKeyspace,
    RedisSlotState,
)
from campus_reserve.checkins.models import CheckIn
from campus_reserve.inventory.models import BookingSlot, ReleaseBatch
from campus_reserve.inventory.services.inventory import InventoryService
from campus_reserve.shared.enums import (
    BookingSlotStatus,
    BookingStatus,
    CourtStatus,
    OperatorType,
    ReleaseBatchStatus,
    TemplateStatus,
    VenueStatus,
)
from campus_reserve.users.models import User
from campus_reserve.venues.models import CalendarException, Court, OpeningTemplate, Venue


class AdminRuleError(ValueError):
    def __init__(self, code: str, message: str, *, not_found: bool = False) -> None:
        self.code = code
        self.not_found = not_found
        super().__init__(message)


class AdminService:
    def __init__(
        self,
        session: AsyncSession,
        redis: Redis,
        principal: AuthPrincipal,
        ip: str | None = None,
    ) -> None:
        self.session = session
        self.redis = redis
        self.principal = principal
        self.ip = ip
        self.keys = RedisReservationKeyspace()

    async def overview(self) -> AdminOverview:
        venue = await self._venue()
        courts = list((await self.session.scalars(select(Court).order_by(Court.code))).all())
        templates = list(
            (await self.session.scalars(select(OpeningTemplate).order_by(OpeningTemplate.id))).all()
        )
        exceptions = list(
            (
                await self.session.scalars(
                    select(CalendarException)
                    .where(
                        CalendarException.exception_date
                        >= datetime.now(ZoneInfo("Asia/Shanghai")).date()
                    )
                    .order_by(CalendarException.exception_date, CalendarException.id)
                    .limit(100)
                )
            ).all()
        )
        batches = list(
            (
                await self.session.scalars(
                    select(ReleaseBatch).order_by(ReleaseBatch.service_date.desc()).limit(30)
                )
            ).all()
        )
        return AdminOverview(
            venue=VenueView.model_validate(venue),
            courts=[self._court_view(court) for court in courts],
            templates=[TemplateView.model_validate(template) for template in templates],
            exceptions=[ExceptionView.model_validate(item) for item in exceptions],
            batches=[BatchView.model_validate(batch) for batch in batches],
        )

    async def update_venue(self, patch: VenuePatch) -> VenueView:
        venue = await self._venue(for_update=True)
        before = self._venue_data(venue)
        if (
            patch.status is not None
            and patch.status != venue.status
            and patch.status is not VenueStatus.ACTIVE
        ):
            await self._pause_open_batches()
        for field in patch.model_fields_set:
            value = getattr(patch, field)
            if field != "notice" and value is None:
                raise AdminRuleError("INVALID_FIELD", f"{field} 不能为空")
            setattr(venue, field, value)
        self._audit("VENUE_UPDATED", "venue", venue.id, before, self._venue_data(venue))
        await self.session.commit()
        return VenueView.model_validate(venue)

    async def update_court(self, court_id: int, patch: CourtPatch) -> CourtView:
        court = await self.session.scalar(
            select(Court).where(Court.id == court_id, Court.venue_id == 1).with_for_update()
        )
        if court is None:
            raise AdminRuleError("COURT_NOT_FOUND", "场地不存在", not_found=True)
        before = self._court_data(court)
        status_changed = patch.status is not None and patch.status != court.status
        if status_changed and patch.status is not CourtStatus.ACTIVE:
            await self._pause_open_batches()
        for field in patch.model_fields_set:
            value = getattr(patch, field)
            if value is None:
                raise AdminRuleError("INVALID_FIELD", f"{field} 不能为空")
            setattr(court, "details" if field == "facilities" else field, value)
        if status_changed:
            slots = list(
                (
                    await self.session.scalars(
                        select(BookingSlot).options(joinedload(BookingSlot.release_batch))
                        .join(ReleaseBatch)
                        .where(
                            BookingSlot.court_id == court_id,
                            ReleaseBatch.status.in_(
                                [ReleaseBatchStatus.DRAFT, ReleaseBatchStatus.READY,
                                 ReleaseBatchStatus.OPEN, ReleaseBatchStatus.PAUSED]
                            ),
                            BookingSlot.status.in_(
                                [BookingSlotStatus.AVAILABLE, BookingSlotStatus.UNRELEASED,
                                 BookingSlotStatus.BLOCKED]
                            ),
                        )
                        .with_for_update(of=BookingSlot)
                    )
                ).all()
            )
            for slot in slots:
                if patch.status is CourtStatus.ACTIVE and slot.block_reason == "场地停用":
                    slot.status = (
                        BookingSlotStatus.AVAILABLE
                        if slot.release_batch.status in (
                            ReleaseBatchStatus.OPEN, ReleaseBatchStatus.PAUSED
                        )
                        else BookingSlotStatus.UNRELEASED
                    )
                    slot.block_reason = None
                elif patch.status is not CourtStatus.ACTIVE and slot.status in (
                    BookingSlotStatus.AVAILABLE, BookingSlotStatus.UNRELEASED
                ):
                    slot.status = BookingSlotStatus.BLOCKED
                    slot.block_reason = "场地停用"
                else:
                    continue
                slot.version += 1
        self._audit("COURT_UPDATED", "court", court.id, before, self._court_data(court))
        await self.session.commit()
        return self._court_view(court)

    async def create_template(self, data: TemplateCreate) -> TemplateView:
        if data.status is TemplateStatus.ACTIVE:
            await self._archive_active_template()
        template = OpeningTemplate(
            venue_id=1,
            name=data.name,
            weekday_mask=data.weekday_mask,
            valid_from=data.valid_from,
            valid_to=data.valid_to,
            status=data.status,
            rule_json=self._frozen_rules(),
        )
        self.session.add(template)
        await self.session.flush()
        self._audit("TEMPLATE_CREATED", "opening_template", template.id, None,
                    self._template_data(template))
        await self.session.commit()
        return TemplateView.model_validate(template)

    async def update_template(self, template_id: int, patch: TemplatePatch) -> TemplateView:
        template = await self.session.get(OpeningTemplate, template_id, with_for_update=True)
        if template is None or template.venue_id != 1:
            raise AdminRuleError("TEMPLATE_NOT_FOUND", "开放模板不存在", not_found=True)
        before = self._template_data(template)
        if patch.status is TemplateStatus.ACTIVE and template.status is not TemplateStatus.ACTIVE:
            await self._archive_active_template(except_id=template.id)
        for field in patch.model_fields_set:
            value = getattr(patch, field)
            if field != "valid_to" and value is None:
                raise AdminRuleError("INVALID_FIELD", f"{field} 不能为空")
            setattr(template, field, value)
        if template.valid_to is not None and template.valid_to < template.valid_from:
            raise AdminRuleError("INVALID_DATE_RANGE", "结束日期不能早于开始日期")
        self._audit("TEMPLATE_UPDATED", "opening_template", template.id, before,
                    self._template_data(template))
        await self.session.commit()
        return TemplateView.model_validate(template)

    async def upsert_exception(self, data: ExceptionUpsert) -> ExceptionView:
        if data.exception_date < datetime.now(ZoneInfo("Asia/Shanghai")).date():
            raise AdminRuleError("PAST_DATE", "不能修改过去的例外日期")
        await self._lock_generation_date(data.exception_date)
        if await self._batch_for_date(data.exception_date):
            raise AdminRuleError("BATCH_ALREADY_GENERATED", "库存已生成，例外日期不能再修改")
        if data.court_id is not None:
            court = await self.session.get(Court, data.court_id)
            if court is None or court.venue_id != 1:
                raise AdminRuleError("COURT_NOT_FOUND", "场地不存在", not_found=True)
        court_filter = (
            CalendarException.court_id.is_(None)
            if data.court_id is None else CalendarException.court_id == data.court_id
        )
        item = await self.session.scalar(
            select(CalendarException)
            .where(
                CalendarException.venue_id == 1,
                CalendarException.exception_date == data.exception_date,
                court_filter,
            )
            .with_for_update()
        )
        before = self._exception_data(item) if item else None
        if item is None:
            item = CalendarException(venue_id=1, court_id=data.court_id,
                                     exception_date=data.exception_date)
            self.session.add(item)
        item.type = data.type
        item.custom_open_time = data.custom_open_time
        item.custom_close_time = data.custom_close_time
        item.reason = data.reason
        await self.session.flush()
        self._audit("EXCEPTION_SAVED", "calendar_exception", item.id, before,
                    self._exception_data(item))
        await self.session.commit()
        return ExceptionView.model_validate(item)

    async def preview_or_generate(
        self, service_date: date, preview_only: bool,
    ) -> GenerationPreview:
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        if not today <= service_date <= today + timedelta(days=30):
            raise AdminRuleError("DATE_OUT_OF_RANGE", "只能生成今天起 30 天内的库存")
        if not preview_only:
            await self._lock_generation_date(service_date)
        venue = await self._venue()
        template = await self.session.scalar(
            select(OpeningTemplate).where(
                OpeningTemplate.venue_id == venue.id,
                OpeningTemplate.status == TemplateStatus.ACTIVE,
                OpeningTemplate.valid_from <= service_date,
                (OpeningTemplate.valid_to.is_(None) |
                 (OpeningTemplate.valid_to >= service_date)),
            )
        )
        if template is None:
            raise AdminRuleError("TEMPLATE_NOT_FOUND", "目标日期没有启用的开放模板")
        courts = list((await self.session.scalars(select(Court).where(Court.venue_id == 1))).all())
        exceptions = list(
            (
                await self.session.scalars(
                    select(CalendarException).where(
                        CalendarException.venue_id == 1,
                        CalendarException.exception_date == service_date,
                    )
                )
            ).all()
        )
        planned = InventoryService._plan_slots(venue, template, courts, exceptions, service_date)
        release_at = datetime.combine(
            service_date - timedelta(days=template.advance_days), template.release_time,
            tzinfo=ZoneInfo(venue.timezone),
        )
        existing = await self._batch_for_date(service_date)
        preview = GenerationPreview(
            service_date=service_date,
            release_at=release_at,
            slot_count=len(planned),
            court_count=len({court_id for court_id, _, _ in planned}),
            existing_batch_id=existing.id if existing else None,
            batch=BatchView.model_validate(existing) if existing else None,
        )
        if preview_only or existing is not None:
            return preview
        if venue.status is not VenueStatus.ACTIVE:
            raise AdminRuleError("VENUE_NOT_ACTIVE", "场馆未开放，不能生成库存")
        existing = await self._batch_for_date(service_date)
        if existing is not None:
            return preview.model_copy(update={"existing_batch_id": existing.id,
                                      "batch": BatchView.model_validate(existing)})
        batch = ReleaseBatch(
            venue_id=venue.id,
            service_date=service_date,
            release_at=release_at,
            status=ReleaseBatchStatus.DRAFT,
            slot_count=len(planned),
            rule_snapshot={
                "open_time": template.open_time.isoformat(timespec="minutes"),
                "close_time": template.close_time.isoformat(timespec="minutes"),
                "slot_minutes": template.slot_minutes,
                "advance_days": template.advance_days,
                "release_time": template.release_time.isoformat(timespec="minutes"),
                **template.rule_json,
            },
        )
        self.session.add(batch)
        await self.session.flush()
        self.session.add_all(
            BookingSlot(court_id=court_id, release_batch_id=batch.id,
                        service_date=service_date, start_at=start, end_at=end,
                        status=BookingSlotStatus.UNRELEASED)
            for court_id, start, end in planned
        )
        self._audit("INVENTORY_GENERATED", "release_batch", batch.id, None,
                    {"service_date": service_date.isoformat(), "slot_count": len(planned)})
        await self.session.commit()
        return preview.model_copy(update={"existing_batch_id": batch.id,
                                  "batch": BatchView.model_validate(batch)})

    async def publish_batch(self, batch_id: int) -> BatchView:
        batch = await self._batch(batch_id)
        if batch.status is ReleaseBatchStatus.DRAFT:
            batch.status = ReleaseBatchStatus.READY
            self._audit("BATCH_PUBLISHED", "release_batch", batch.id,
                        {"status": "DRAFT"}, {"status": "READY"})
            await self.session.commit()
        elif batch.status is not ReleaseBatchStatus.READY:
            raise AdminRuleError("INVALID_BATCH_STATE", "只有草稿批次可以发布")
        return BatchView.model_validate(batch)

    async def change_batch(self, batch_id: int, action: str) -> BatchView:
        batch = await self._batch(batch_id)
        before = batch.status
        if action == "pause":
            if before not in (ReleaseBatchStatus.OPEN, ReleaseBatchStatus.PAUSED):
                raise AdminRuleError("INVALID_BATCH_STATE", "只有开放批次可以暂停")
            await self._set_redis_gate(batch.id, "PAUSED")
            batch.status = ReleaseBatchStatus.PAUSED
        elif action == "close":
            if before is ReleaseBatchStatus.CLOSED:
                return BatchView.model_validate(batch)
            if before is ReleaseBatchStatus.DRAFT:
                raise AdminRuleError("INVALID_BATCH_STATE", "草稿批次不能关闭")
            await self._set_redis_gate(batch.id, "CLOSED")
            batch.status = ReleaseBatchStatus.CLOSED
            batch.closed_at = datetime.now(UTC)
            batch.opened_at = batch.opened_at or batch.closed_at
            slots = list((await self.session.scalars(
                select(BookingSlot).where(
                    BookingSlot.release_batch_id == batch.id,
                    BookingSlot.status.in_([
                        BookingSlotStatus.AVAILABLE, BookingSlotStatus.UNRELEASED
                    ]),
                ).with_for_update()
            )).all())
            for slot in slots:
                slot.status = BookingSlotStatus.BLOCKED
                slot.block_reason = "批次关闭"
                slot.version += 1
        elif action in ("open", "resume"):
            expected = (ReleaseBatchStatus.READY if action == "open"
                        else ReleaseBatchStatus.PAUSED)
            if before is not expected and before is not ReleaseBatchStatus.OPEN:
                raise AdminRuleError("INVALID_BATCH_STATE", "批次状态不允许此操作")
            venue = await self._venue()
            if venue.status is not VenueStatus.ACTIVE:
                raise AdminRuleError("VENUE_NOT_ACTIVE", "场馆未开放")
            if datetime.now(UTC) < batch.release_at:
                raise AdminRuleError("RELEASE_NOT_DUE", "尚未到放号时间")
            if batch.slot_count == 0:
                raise AdminRuleError("EMPTY_BATCH", "批次没有可开放的时段")
            if before is ReleaseBatchStatus.READY:
                slots = list((await self.session.scalars(
                    select(BookingSlot).where(
                        BookingSlot.release_batch_id == batch.id,
                        BookingSlot.status == BookingSlotStatus.UNRELEASED,
                    ).with_for_update()
                )).all())
                for slot in slots:
                    slot.status = BookingSlotStatus.AVAILABLE
                    slot.version += 1
            batch.status = ReleaseBatchStatus.OPEN
            batch.opened_at = batch.opened_at or datetime.now(UTC)
        else:
            raise AdminRuleError("INVALID_ACTION", "不支持的批次操作")
        if before is not batch.status:
            self._audit(f"BATCH_{action.upper()}", "release_batch", batch.id,
                        {"status": before.value}, {"status": batch.status.value})
        await self.session.commit()
        if action in ("open", "resume"):
            await self._sync_open_batch(batch)
        return BatchView.model_validate(batch)

    async def search_bookings(
        self, *, booking_no: str | None, username: str | None,
        service_date: date | None, court_id: int | None, limit: int,
    ) -> list[AdminBookingView]:
        statement = (
            select(Booking)
            .join(User, Booking.user_id == User.id)
            .options(
                joinedload(Booking.user),
                selectinload(Booking.items).joinedload(BookingItem.slot)
                .joinedload(BookingSlot.court),
                selectinload(Booking.check_in),
            )
            .order_by(Booking.created_at.desc())
            .limit(limit)
        )
        if booking_no:
            statement = statement.where(Booking.booking_no.ilike(f"%{booking_no}%"))
        if username:
            statement = statement.where(User.username.ilike(f"%{username}%"))
        if service_date or court_id:
            conditions = []
            if service_date:
                conditions.append(BookingItem.service_date == service_date)
            if court_id:
                conditions.append(BookingItem.slot.has(BookingSlot.court_id == court_id))
            statement = statement.where(Booking.items.any(and_(*conditions)))
        bookings = list((await self.session.scalars(statement)).unique().all())
        return [self._booking_view(booking) for booking in bookings]

    async def check_in(self, booking_id: int, data: CheckInRequest) -> CheckInView:
        booking = await self.session.scalar(
            select(Booking).where(Booking.id == booking_id).with_for_update(of=Booking)
        )
        if booking is None:
            raise AdminRuleError("BOOKING_NOT_FOUND", "预约不存在", not_found=True)
        existing = await self.session.scalar(
            select(CheckIn).where(CheckIn.booking_id == booking_id)
        )
        if existing is not None:
            return CheckInView(booking_id=booking.id, booking_no=booking.booking_no,
                               booking_status=booking.status,
                               checked_in_at=existing.checked_in_at,
                               verifier_id=existing.verifier_id, replayed=True)
        if booking.status not in (
            BookingStatus.CONFIRMED, BookingStatus.CHECKED_IN, BookingStatus.COMPLETED
        ):
            raise AdminRuleError("BOOKING_NOT_CONFIRMABLE", "只有有效预约可以签到")
        check_in = CheckIn(booking_id=booking.id, user_id=booking.user_id,
                           verifier_id=self.principal.user_id, note=data.note)
        self.session.add(check_in)
        await self.session.flush()
        if booking.status is BookingStatus.CONFIRMED:
            booking.status = BookingStatus.CHECKED_IN
            self.session.add(BookingStatusLog(
                booking_id=booking.id, from_status=BookingStatus.CONFIRMED,
                to_status=BookingStatus.CHECKED_IN, operator_type=OperatorType.ADMIN,
                operator_id=self.principal.user_id, reason="管理员现场签到",
                trace_id=str(uuid4()),
            ))
        self._audit("BOOKING_CHECKED_IN", "booking", booking.id, None,
                    {"checked_in_at": check_in.checked_in_at.isoformat(),
                     "verifier_id": self.principal.user_id})
        await self.session.commit()
        return CheckInView(booking_id=booking.id, booking_no=booking.booking_no,
                           booking_status=booking.status,
                           checked_in_at=check_in.checked_in_at,
                           verifier_id=check_in.verifier_id, replayed=False)

    async def audit_logs(self, limit: int = 100) -> list[AuditView]:
        rows = list((await self.session.scalars(
            select(AuditLog).order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(limit)
        )).all())
        return [AuditView.model_validate(row) for row in rows]

    async def _sync_open_batch(self, batch: ReleaseBatch) -> None:
        rows = list((await self.session.scalars(
            select(BookingSlot).options(joinedload(BookingSlot.court))
            .where(BookingSlot.release_batch_id == batch.id)
            .order_by(BookingSlot.id)
        )).all())
        periods_key = self.keys.periods(batch.id)
        slots_key = self.keys.slots(batch.id)
        marker = await self.redis.get(self.keys.inventory_marker(batch.id))
        if marker is None:
            usage_rows = await self.session.execute(
                select(BookingItem.user_id, BookingItem.start_at, BookingItem.end_at)
                .join(BookingSlot, BookingItem.slot_id == BookingSlot.id)
                .where(BookingSlot.release_batch_id == batch.id)
            )
            usages: dict[int, list[str]] = {}
            for user_id, start, end in usage_rows:
                usages.setdefault(user_id, []).append(f"{start.isoformat()}|{end.isoformat()}")
            result = await RedisAtomicReservationService(self.redis).initialize_inventory(
                batch.id, batch.service_date,
                [RedisInventorySlot(
                    slot.id, f"{slot.start_at.isoformat()}|{slot.end_at.isoformat()}",
                    RedisSlotState.AVAILABLE if slot.status is BookingSlotStatus.AVAILABLE
                    and slot.court.status is CourtStatus.ACTIVE else RedisSlotState.UNAVAILABLE,
                ) for slot in rows],
                [RedisConfirmedUsage(user_id, tuple(periods))
                 for user_id, periods in usages.items()],
            )
            if result.code is not RedisInventoryCode.INITIALIZED:
                raise AdminRuleError("REDIS_INVENTORY_INVALID", "Redis 库存未能初始化")
        else:
            stored_date = await self.redis.hget(periods_key, "__service_date")
            if stored_date != batch.service_date.isoformat():
                raise AdminRuleError("REDIS_INVENTORY_INVALID", "Redis 批次日期不一致")
            await self.redis.hset(periods_key, "__batch_status", "PAUSED")
            cached = await self.redis.hgetall(slots_key)
            updates: dict[str, str] = {}
            for slot in rows:
                state = cached.get(str(slot.id))
                if state is None:
                    raise AdminRuleError("REDIS_INVENTORY_INVALID", "Redis 缺少库存时段")
                if state.startswith("RESERVED:"):
                    request_id = state.split(":", 2)[1]
                    result_state = await self.redis.hget(
                        f"{self.keys.prefix}:request:{request_id}", "persistence_status"
                    )
                    if result_state != "CONFIRMED" or slot.status is not BookingSlotStatus.BOOKED:
                        raise AdminRuleError("PENDING_CLAIMS", "存在未完成占位，暂不能恢复放号")
                    continue
                updates[str(slot.id)] = (
                    "AVAILABLE" if slot.status is BookingSlotStatus.AVAILABLE
                    and slot.court.status is CourtStatus.ACTIVE else "UNAVAILABLE"
                )
            if updates:
                await self.redis.hset(slots_key, mapping=updates)
        await self.redis.hset(periods_key, "__batch_status", "OPEN")

    async def _pause_open_batches(self) -> None:
        batches = list((await self.session.scalars(
            select(ReleaseBatch).where(ReleaseBatch.status == ReleaseBatchStatus.OPEN)
            .with_for_update()
        )).all())
        for batch in batches:
            await self._set_redis_gate(batch.id, "PAUSED")
            batch.status = ReleaseBatchStatus.PAUSED
            self._audit("BATCH_PAUSED", "release_batch", batch.id,
                        {"status": "OPEN"}, {"status": "PAUSED"})

    async def _set_redis_gate(self, batch_id: int, state: str) -> None:
        key = self.keys.periods(batch_id)
        if await self.redis.exists(key):
            await self.redis.hset(key, "__batch_status", state)

    async def _archive_active_template(self, except_id: int | None = None) -> None:
        active = await self.session.scalar(
            select(OpeningTemplate).where(
                OpeningTemplate.venue_id == 1,
                OpeningTemplate.status == TemplateStatus.ACTIVE,
            ).with_for_update()
        )
        if active is not None and active.id != except_id:
            active.status = TemplateStatus.ARCHIVED
            await self.session.flush()

    async def _venue(self, *, for_update: bool = False) -> Venue:
        statement = select(Venue).where(Venue.id == 1)
        if for_update:
            statement = statement.with_for_update()
        venue = await self.session.scalar(statement)
        if venue is None:
            raise AdminRuleError("VENUE_NOT_FOUND", "场馆不存在", not_found=True)
        return venue

    async def _batch(self, batch_id: int) -> ReleaseBatch:
        batch = await self.session.scalar(
            select(ReleaseBatch).where(ReleaseBatch.id == batch_id).with_for_update()
        )
        if batch is None:
            raise AdminRuleError("BATCH_NOT_FOUND", "放号批次不存在", not_found=True)
        return batch

    async def _batch_for_date(self, service_date: date) -> ReleaseBatch | None:
        return await self.session.scalar(select(ReleaseBatch).where(
            ReleaseBatch.venue_id == 1, ReleaseBatch.service_date == service_date))

    async def _lock_generation_date(self, service_date: date) -> None:
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_name, 0))"),
            {"lock_name": f"inventory:1:{service_date.isoformat()}"},
        )

    def _audit(
        self, action: str, resource_type: str, resource_id: int,
        before: dict | None, after: dict | None,
    ) -> None:
        self.session.add(AuditLog(
            actor_id=self.principal.user_id, actor_role=self.principal.role,
            action=action, resource_type=resource_type, resource_id=str(resource_id),
            before_data=before, after_data=after, ip=self.ip, trace_id=str(uuid4()),
        ))

    @staticmethod
    def _frozen_rules() -> dict:
        return {"max_slots_per_request": 2, "max_slots_per_user_per_day": 2,
                "allow_non_contiguous": True, "cancellation_allowed": False}

    @staticmethod
    def _venue_data(venue: Venue) -> dict:
        return {"name": venue.name, "address": venue.address, "notice": venue.notice,
                "status": venue.status.value}

    @staticmethod
    def _court_data(court: Court) -> dict:
        return {"name": court.name, "status": court.status.value,
                "facilities": court.details}

    @staticmethod
    def _court_view(court: Court) -> CourtView:
        return CourtView(id=court.id, code=court.code, name=court.name,
                         status=court.status, facilities=court.details)

    @staticmethod
    def _template_data(template: OpeningTemplate) -> dict:
        return {"name": template.name, "weekday_mask": template.weekday_mask,
                "valid_from": template.valid_from.isoformat(),
                "valid_to": template.valid_to.isoformat() if template.valid_to else None,
                "status": template.status.value}

    @staticmethod
    def _exception_data(item: CalendarException) -> dict:
        return {"exception_date": item.exception_date.isoformat(),
                "court_id": item.court_id, "type": item.type.value,
                "custom_open_time": item.custom_open_time.isoformat()
                if item.custom_open_time else None,
                "custom_close_time": item.custom_close_time.isoformat()
                if item.custom_close_time else None, "reason": item.reason}

    @staticmethod
    def _booking_view(booking: Booking) -> AdminBookingView:
        items = sorted(booking.items, key=lambda item: item.start_at)
        return AdminBookingView(
            id=booking.id, booking_no=booking.booking_no,
            username=booking.user.username, display_name=booking.user.display_name,
            status=booking.status,
            service_date=items[0].service_date,
            courts=", ".join(sorted({item.slot.court.name for item in items})),
            start_at=items[0].start_at, end_at=max(item.end_at for item in items),
            checked_in_at=booking.check_in.checked_in_at if booking.check_in else None,
        )


async def archive_completed_bookings(session: AsyncSession, now: datetime) -> int:
    bookings = list((await session.scalars(
        select(Booking)
        .options(selectinload(Booking.items))
        .where(
            Booking.status.in_([BookingStatus.CONFIRMED, BookingStatus.CHECKED_IN]),
            Booking.items.any(),
            ~Booking.items.any(BookingItem.end_at > now),
        )
        .order_by(Booking.id)
        .limit(500)
        .with_for_update(of=Booking, skip_locked=True)
    )).all())
    count = 0
    for booking in bookings:
        if not booking.items or max(item.end_at for item in booking.items) > now:
            continue
        old_status = booking.status
        booking.status = BookingStatus.COMPLETED
        session.add(BookingStatusLog(
            booking_id=booking.id, from_status=old_status,
            to_status=BookingStatus.COMPLETED, operator_type=OperatorType.SYSTEM,
            reason="预约时段已结束", trace_id=str(uuid4()),
        ))
        count += 1
    return count
