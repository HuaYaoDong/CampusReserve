from datetime import date, datetime

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from campus_reserve.bookings.models import BookingItem
from campus_reserve.inventory.models import BookingSlot, ReleaseBatch
from campus_reserve.inventory.repositories import (
    ConfirmedUsageRow,
    InventoryConfiguration,
    InventorySlotRow,
    VenueConfiguration,
)
from campus_reserve.shared.enums import (
    BookingSlotStatus,
    ReleaseBatchStatus,
    TemplateStatus,
    VenueStatus,
)
from campus_reserve.venues.models import CalendarException, Court, OpeningTemplate, Venue


class SqlAlchemyInventoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_active_configuration(
        self, service_date: date
    ) -> InventoryConfiguration | None:
        statement = (
            select(OpeningTemplate)
            .join(OpeningTemplate.venue)
            .options(selectinload(OpeningTemplate.venue).selectinload(Venue.courts))
            .where(
                OpeningTemplate.status == TemplateStatus.ACTIVE,
                OpeningTemplate.valid_from <= service_date,
                (OpeningTemplate.valid_to.is_(None) | (OpeningTemplate.valid_to >= service_date)),
                Venue.status == VenueStatus.ACTIVE,
            )
            .order_by(OpeningTemplate.venue_id)
            .limit(1)
        )
        template = await self.session.scalar(statement)
        if template is None:
            return None

        exceptions = list(
            (
                await self.session.scalars(
                    select(CalendarException).where(
                        CalendarException.venue_id == template.venue_id,
                        CalendarException.exception_date == service_date,
                    )
                )
            ).all()
        )
        courts = sorted(template.venue.courts, key=lambda court: court.code)
        return template.venue, template, courts, exceptions

    async def acquire_generation_lock(self, venue_id: int, service_date: date) -> None:
        lock_name = f"inventory:{venue_id}:{service_date.isoformat()}"
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_name, 0))"),
            {"lock_name": lock_name},
        )

    async def get_batch(self, venue_id: int, service_date: date) -> ReleaseBatch | None:
        return await self.session.scalar(
            select(ReleaseBatch).where(
                ReleaseBatch.venue_id == venue_id,
                ReleaseBatch.service_date == service_date,
            )
        )

    async def add_batch(self, batch: ReleaseBatch) -> None:
        self.session.add(batch)
        await self.session.flush()

    async def add_slots(self, slots: list[BookingSlot]) -> None:
        self.session.add_all(slots)
        await self.session.flush()

    async def release_batch(self, batch: ReleaseBatch, opened_at: datetime) -> None:
        batch.status = ReleaseBatchStatus.OPEN
        batch.opened_at = opened_at
        await self.session.execute(
            update(BookingSlot)
            .where(
                BookingSlot.release_batch_id == batch.id,
                BookingSlot.status == BookingSlotStatus.UNRELEASED,
            )
            .values(status=BookingSlotStatus.AVAILABLE, updated_at=func.now())
        )
        await self.session.flush()

    async def list_venue_configurations(self) -> list[VenueConfiguration]:
        templates = list(
            (
                await self.session.scalars(
                    select(OpeningTemplate)
                    .join(OpeningTemplate.venue)
                    .options(
                        selectinload(OpeningTemplate.venue).selectinload(Venue.courts)
                    )
                    .where(
                        OpeningTemplate.status == TemplateStatus.ACTIVE,
                        Venue.status == VenueStatus.ACTIVE,
                    )
                    .order_by(OpeningTemplate.venue_id)
                )
            ).all()
        )
        return [
            (
                template.venue,
                template,
                sorted(template.venue.courts, key=lambda court: court.code),
            )
            for template in templates
        ]

    async def list_batch_slots(self, batch_id: int) -> list[InventorySlotRow]:
        rows = await self.session.execute(
            select(BookingSlot, Court)
            .join(Court, BookingSlot.court_id == Court.id)
            .where(BookingSlot.release_batch_id == batch_id)
            .order_by(Court.code, BookingSlot.start_at)
        )
        return list(rows.tuples().all())

    async def list_batch_confirmed_usage(
        self, batch_id: int
    ) -> list[ConfirmedUsageRow]:
        rows = await self.session.execute(
            select(BookingItem.user_id, BookingItem.start_at, BookingItem.end_at)
            .join(BookingSlot, BookingItem.slot_id == BookingSlot.id)
            .where(BookingSlot.release_batch_id == batch_id)
            .order_by(BookingItem.user_id, BookingItem.start_at)
        )
        return list(rows.tuples().all())
