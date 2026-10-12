from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from campus_reserve.inventory.models import BookingSlot, ReleaseBatch
from campus_reserve.inventory.repositories import InventoryRepository, InventorySlotRow
from campus_reserve.shared.enums import (
    BookingSlotStatus,
    CalendarExceptionType,
    CourtStatus,
    ReleaseBatchStatus,
)
from campus_reserve.venues.models import CalendarException, Court, OpeningTemplate, Venue


class InventoryConfigurationError(RuntimeError):
    pass


class InventoryNotFoundError(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class InventorySyncResult:
    venue_id: int
    service_date: date
    batch_id: int
    slot_count: int
    status: ReleaseBatchStatus
    generated: bool
    released: bool


@dataclass(frozen=True, slots=True)
class InventoryView:
    venue: Venue
    template: OpeningTemplate
    courts: list[Court]
    batch: ReleaseBatch
    slot_rows: list[InventorySlotRow]


def build_slot_windows(
    service_date: date,
    open_time: time,
    close_time: time,
    slot_minutes: int,
    timezone_name: str,
) -> list[tuple[datetime, datetime]]:
    timezone = ZoneInfo(timezone_name)
    cursor = datetime.combine(service_date, open_time, tzinfo=timezone)
    closes_at = datetime.combine(service_date, close_time, tzinfo=timezone)
    duration = timedelta(minutes=slot_minutes)
    windows: list[tuple[datetime, datetime]] = []
    while cursor + duration <= closes_at:
        windows.append((cursor, cursor + duration))
        cursor += duration
    return windows


class InventoryService:
    def __init__(self, repository: InventoryRepository) -> None:
        self.repository = repository

    async def synchronize_target(self, now: datetime) -> InventorySyncResult:
        local_now = now.astimezone(ZoneInfo("Asia/Shanghai"))
        provisional_date = local_now.date() + timedelta(days=2)
        configuration = await self.repository.get_active_configuration(provisional_date)
        if configuration is None:
            raise InventoryConfigurationError("没有适用于目标日期的启用场馆和开放模板")

        venue, template, courts, exceptions = configuration
        service_date = local_now.date() + timedelta(days=template.advance_days)
        if service_date != provisional_date:
            configuration = await self.repository.get_active_configuration(service_date)
            if configuration is None:
                raise InventoryConfigurationError("没有适用于目标日期的启用场馆和开放模板")
            venue, template, courts, exceptions = configuration

        release_date = service_date - timedelta(days=template.advance_days)
        release_at = datetime.combine(
            release_date,
            template.release_time,
            tzinfo=ZoneInfo(venue.timezone),
        )
        should_release = now >= release_at

        await self.repository.acquire_generation_lock(venue.id, service_date)
        configuration = await self.repository.get_active_configuration(service_date)
        if configuration is None:
            raise InventoryConfigurationError("没有适用于目标日期的启用场馆和开放模板")
        venue, template, courts, exceptions = configuration
        existing = await self.repository.get_batch(venue.id, service_date)
        if existing is not None:
            released = False
            if should_release and existing.status is ReleaseBatchStatus.READY:
                await self.repository.release_batch(existing, now)
                released = True
            return InventorySyncResult(
                venue_id=venue.id,
                service_date=service_date,
                batch_id=existing.id,
                slot_count=existing.slot_count,
                status=existing.status,
                generated=False,
                released=released,
            )

        slot_status = (
            BookingSlotStatus.AVAILABLE if should_release else BookingSlotStatus.UNRELEASED
        )
        batch_status = ReleaseBatchStatus.OPEN if should_release else ReleaseBatchStatus.READY
        planned_slots = self._plan_slots(
            venue,
            template,
            courts,
            exceptions,
            service_date,
        )
        batch = ReleaseBatch(
            venue_id=venue.id,
            service_date=service_date,
            release_at=release_at,
            status=batch_status,
            slot_count=len(planned_slots),
            rule_snapshot={
                "open_time": template.open_time.isoformat(timespec="minutes"),
                "close_time": template.close_time.isoformat(timespec="minutes"),
                "slot_minutes": template.slot_minutes,
                "advance_days": template.advance_days,
                "release_time": template.release_time.isoformat(timespec="minutes"),
                **template.rule_json,
            },
            opened_at=now if should_release else None,
        )
        await self.repository.add_batch(batch)
        slots = [
            BookingSlot(
                court_id=court_id,
                release_batch_id=batch.id,
                service_date=service_date,
                start_at=start_at,
                end_at=end_at,
                status=slot_status,
            )
            for court_id, start_at, end_at in planned_slots
        ]
        await self.repository.add_slots(slots)
        return InventorySyncResult(
            venue_id=venue.id,
            service_date=service_date,
            batch_id=batch.id,
            slot_count=len(slots),
            status=batch.status,
            generated=True,
            released=should_release,
        )

    async def list_venues(self):
        return await self.repository.list_venue_configurations()

    async def get_inventory(self, venue_id: int, service_date: date) -> InventoryView:
        configurations = await self.repository.list_venue_configurations()
        configuration = next(
            (item for item in configurations if item[0].id == venue_id),
            None,
        )
        if configuration is None:
            raise InventoryNotFoundError("场馆不存在或未开放")
        venue, template, courts = configuration
        batch = await self.repository.get_batch(venue_id, service_date)
        if batch is None:
            raise InventoryNotFoundError("该日期库存尚未生成")
        rows = await self.repository.list_batch_slots(batch.id)
        return InventoryView(venue, template, courts, batch, rows)

    @staticmethod
    def _plan_slots(
        venue: Venue,
        template: OpeningTemplate,
        courts: list[Court],
        exceptions: list[CalendarException],
        service_date: date,
    ) -> list[tuple[int, datetime, datetime]]:
        if not template.weekday_mask & (1 << service_date.weekday()):
            return []

        venue_exception = next((item for item in exceptions if item.court_id is None), None)
        if venue_exception and venue_exception.type is CalendarExceptionType.CLOSED:
            return []

        default_open = template.open_time
        default_close = template.close_time
        if venue_exception and venue_exception.type is CalendarExceptionType.CUSTOM_HOURS:
            default_open = venue_exception.custom_open_time or default_open
            default_close = venue_exception.custom_close_time or default_close

        exceptions_by_court = {
            item.court_id: item for item in exceptions if item.court_id is not None
        }
        planned: list[tuple[int, datetime, datetime]] = []
        for court in courts:
            if court.status is not CourtStatus.ACTIVE:
                continue
            court_exception = exceptions_by_court.get(court.id)
            if court_exception and court_exception.type is CalendarExceptionType.CLOSED:
                continue
            open_time = default_open
            close_time = default_close
            if court_exception and court_exception.type is CalendarExceptionType.CUSTOM_HOURS:
                open_time = court_exception.custom_open_time or open_time
                close_time = court_exception.custom_close_time or close_time
            for start_at, end_at in build_slot_windows(
                service_date,
                open_time,
                close_time,
                template.slot_minutes,
                venue.timezone,
            ):
                planned.append((court.id, start_at, end_at))
        return planned
