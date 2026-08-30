from datetime import date
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from campus_reserve.bookings.models import (
    Booking,
    BookingItem,
    BookingRequest,
    BookingStatusLog,
)
from campus_reserve.inventory.models import BookingSlot
from campus_reserve.users.models import UserDailyUsage


class SqlAlchemyBookingRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def acquire_request_lock(self, request_id: UUID) -> None:
        await self._advisory_lock(f"booking-request:{request_id}")

    async def acquire_user_date_lock(self, user_id: int, service_date: date) -> None:
        await self._advisory_lock(f"booking-usage:{user_id}:{service_date.isoformat()}")

    async def _advisory_lock(self, lock_name: str) -> None:
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_name, 0))"),
            {"lock_name": lock_name},
        )

    async def get_request(self, request_id: UUID) -> BookingRequest | None:
        return await self.session.scalar(
            select(BookingRequest)
            .options(
                selectinload(BookingRequest.booking)
                .selectinload(Booking.items)
                .joinedload(BookingItem.slot)
                .joinedload(BookingSlot.court)
            )
            .where(BookingRequest.request_id == request_id)
            .execution_options(populate_existing=True)
        )

    async def get_slots(self, slot_ids: list[int], *, for_update: bool) -> list[BookingSlot]:
        statement = (
            select(BookingSlot)
            .options(
                joinedload(BookingSlot.release_batch),
                joinedload(BookingSlot.court),
            )
            .where(BookingSlot.id.in_(slot_ids))
            .order_by(BookingSlot.id)
        )
        if for_update:
            statement = statement.with_for_update(of=BookingSlot).execution_options(
                populate_existing=True
            )
        return list((await self.session.scalars(statement)).all())

    async def get_daily_usage(
        self, user_id: int, service_date: date
    ) -> UserDailyUsage | None:
        return await self.session.scalar(
            select(UserDailyUsage)
            .where(
                UserDailyUsage.user_id == user_id,
                UserDailyUsage.service_date == service_date,
            )
            .with_for_update()
        )

    async def list_user_items(self, user_id: int, service_date: date) -> list[BookingItem]:
        return list(
            (
                await self.session.scalars(
                    select(BookingItem).where(
                        BookingItem.user_id == user_id,
                        BookingItem.service_date == service_date,
                    )
                )
            ).all()
        )

    async def add_request(self, request: BookingRequest) -> None:
        self.session.add(request)
        await self.session.flush()

    async def add_booking(self, booking: Booking) -> None:
        self.session.add(booking)
        await self.session.flush()

    async def add_items(self, items: list[BookingItem]) -> None:
        self.session.add_all(items)

    async def add_daily_usage(self, usage: UserDailyUsage) -> None:
        self.session.add(usage)

    async def add_status_log(self, log: BookingStatusLog) -> None:
        self.session.add(log)

    async def flush(self) -> None:
        await self.session.flush()

    async def list_user_bookings(self, user_id: int) -> list[Booking]:
        return list(
            (
                await self.session.scalars(
                    select(Booking)
                    .options(
                        selectinload(Booking.items)
                        .joinedload(BookingItem.slot)
                        .joinedload(BookingSlot.court)
                    )
                    .where(Booking.user_id == user_id)
                    .order_by(Booking.created_at.desc())
                )
            ).all()
        )

    async def get_user_booking(self, user_id: int, booking_id: int) -> Booking | None:
        return await self.session.scalar(
            select(Booking)
            .options(
                selectinload(Booking.items)
                .joinedload(BookingItem.slot)
                .joinedload(BookingSlot.court)
            )
            .where(Booking.id == booking_id, Booking.user_id == user_id)
        )
