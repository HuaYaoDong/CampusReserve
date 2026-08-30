import asyncio
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import campus_reserve.models  # noqa: F401
from campus_reserve.bookings.infrastructure.sqlalchemy_repository import (
    SqlAlchemyBookingRepository,
)
from campus_reserve.bookings.models import (
    Booking,
    BookingItem,
    BookingRequest,
    BookingStatusLog,
)
from campus_reserve.bookings.services.redis_reservation import (
    RedisReservationCommand,
    reservation_fingerprint,
)
from campus_reserve.bookings.services.stream_booking import (
    BookingEventProcessor,
    BookingPersistenceStatus,
)
from campus_reserve.bookings.services.sync_booking import BookingRuleError, SyncBookingService
from campus_reserve.inventory.models import BookingSlot, ReleaseBatch
from campus_reserve.shared.config import get_settings
from campus_reserve.shared.enums import (
    BookingRequestStatus,
    BookingSlotStatus,
    BookingStatus,
    ReleaseBatchStatus,
    UserRole,
    UserStatus,
)
from campus_reserve.users.models import User, UserDailyUsage


async def _create_user(session: AsyncSession, suffix: str) -> User:
    user = User(
        username=f"booking-test-{suffix}",
        password_hash="test-only",
        student_no=f"B{suffix}",
        display_name="预约测试学生",
        role=UserRole.STUDENT,
        status=UserStatus.ACTIVE,
    )
    session.add(user)
    await session.flush()
    return user


async def _create_inventory(
    session: AsyncSession,
    service_date: date,
    periods: list[tuple[int, int]],
) -> tuple[ReleaseBatch, list[BookingSlot]]:
    now = datetime.now(UTC)
    batch = ReleaseBatch(
        venue_id=1,
        service_date=service_date,
        release_at=now - timedelta(minutes=1),
        status=ReleaseBatchStatus.OPEN,
        slot_count=len(periods),
        rule_snapshot={"max_slots_per_user_per_day": 2},
        opened_at=now,
    )
    session.add(batch)
    await session.flush()
    slots = [
        BookingSlot(
            court_id=court_id,
            release_batch_id=batch.id,
            service_date=service_date,
            start_at=datetime(
                service_date.year,
                service_date.month,
                service_date.day,
                hour,
                tzinfo=UTC,
            ),
            end_at=datetime(
                service_date.year,
                service_date.month,
                service_date.day,
                hour,
                tzinfo=UTC,
            )
            + timedelta(hours=1),
            status=BookingSlotStatus.AVAILABLE,
        )
        for court_id, hour in periods
    ]
    session.add_all(slots)
    await session.flush()
    return batch, slots


@pytest.mark.integration
async def test_sync_booking_is_idempotent_and_enforces_daily_limit() -> None:
    engine = create_async_engine(get_settings().postgres_dsn, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async with sessions() as session:
        transaction = await session.begin()
        try:
            suffix = uuid4().hex[:20]
            user = await _create_user(session, suffix)
            other_user = await _create_user(session, f"x{suffix}")
            _, slots = await _create_inventory(
                session,
                date(2060, 1, 2),
                [(1, 0), (1, 2), (1, 4), (2, 6), (3, 6)],
            )
            service = SyncBookingService(SqlAlchemyBookingRepository(session))
            request_id = uuid4()

            first = await service.submit(user.id, request_id, [slots[0].id, slots[1].id])
            replay = await service.submit(user.id, request_id, [slots[1].id, slots[0].id])

            assert first.replayed is False
            assert first.request.status is BookingRequestStatus.CONFIRMED
            assert first.request.booking is not None
            assert first.request.booking.status is BookingStatus.CONFIRMED
            assert replay.replayed is True
            assert replay.request.booking_id == first.request.booking_id
            assert (
                await session.scalar(
                    select(func.count()).select_from(Booking).where(Booking.user_id == user.id)
                )
                == 1
            )
            usage = await session.get(UserDailyUsage, (user.id, date(2060, 1, 2)))
            assert usage is not None
            assert usage.used_slot_count == 2

            with pytest.raises(BookingRuleError, match="每天最多预约两个小时") as limit:
                await service.submit(user.id, uuid4(), [slots[2].id])
            assert limit.value.code == "DAILY_LIMIT_EXCEEDED"

            with pytest.raises(BookingRuleError, match="时间冲突") as overlap:
                await service.submit(
                    other_user.id,
                    uuid4(),
                    [slots[3].id, slots[4].id],
                )
            assert overlap.value.code == "TIME_CONFLICT"
        finally:
            await transaction.rollback()
    await engine.dispose()


@pytest.mark.integration
async def test_two_non_contiguous_slots_fail_as_one_atomic_request() -> None:
    engine = create_async_engine(get_settings().postgres_dsn, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async with sessions() as session:
        transaction = await session.begin()
        try:
            suffix = uuid4().hex[:20]
            student = await _create_user(session, f"atomic-{suffix}")
            other_student = await _create_user(session, f"other-{suffix}")
            _, slots = await _create_inventory(
                session,
                date(2065, 1, 2),
                [(1, 8), (1, 10)],
            )
            service = SyncBookingService(SqlAlchemyBookingRepository(session))
            await service.submit(other_student.id, uuid4(), [slots[1].id])

            with pytest.raises(BookingRuleError) as failure:
                await service.submit(
                    student.id,
                    uuid4(),
                    [slots[0].id, slots[1].id],
                )

            assert failure.value.code == "SLOT_NOT_AVAILABLE"
            await session.refresh(slots[0])
            assert slots[0].status is BookingSlotStatus.AVAILABLE
            assert await session.get(UserDailyUsage, (student.id, date(2065, 1, 2))) is None
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(BookingItem)
                    .where(BookingItem.user_id == student.id)
                )
                == 0
            )
        finally:
            await transaction.rollback()
    await engine.dispose()


@pytest.mark.integration
async def test_concurrent_sync_booking_never_oversells_one_slot() -> None:
    engine = create_async_engine(get_settings().postgres_dsn, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    suffix = uuid4().hex[:20]
    service_date = date(2070, 1, 2)
    request_ids = [uuid4(), uuid4()]
    user_ids: list[int] = []
    batch_id = 0
    slot_id = 0

    try:
        async with sessions() as setup_session, setup_session.begin():
            users = [
                await _create_user(setup_session, f"a{suffix}"),
                await _create_user(setup_session, f"b{suffix}"),
            ]
            user_ids = [user.id for user in users]
            batch, slots = await _create_inventory(setup_session, service_date, [(1, 1)])
            batch_id = batch.id
            slot_id = slots[0].id

        async def attempt(user_id: int, request_id: UUID) -> str:
            async with sessions() as session, session.begin():
                service = SyncBookingService(SqlAlchemyBookingRepository(session))
                try:
                    await service.submit(user_id, request_id, [slot_id])
                    return "CONFIRMED"
                except BookingRuleError as exc:
                    return exc.code

        results = await asyncio.gather(
            attempt(user_ids[0], request_ids[0]),
            attempt(user_ids[1], request_ids[1]),
        )
        assert sorted(results) == ["CONFIRMED", "SLOT_NOT_AVAILABLE"]

        async with sessions() as verify_session:
            booking_count = await verify_session.scalar(
                select(func.count()).select_from(BookingItem).where(BookingItem.slot_id == slot_id)
            )
            slot = await verify_session.get(BookingSlot, slot_id)
            assert booking_count == 1
            assert slot is not None
            assert slot.status is BookingSlotStatus.BOOKED
            assert slot.current_booking_id is not None
    finally:
        if batch_id:
            async with sessions() as cleanup_session, cleanup_session.begin():
                booking_ids = list(
                    (
                        await cleanup_session.scalars(
                            select(Booking.id).where(Booking.request_id.in_(request_ids))
                        )
                    ).all()
                )
                if booking_ids:
                    await cleanup_session.execute(
                        delete(BookingStatusLog).where(
                            BookingStatusLog.booking_id.in_(booking_ids)
                        )
                    )
                    await cleanup_session.execute(
                        delete(BookingItem).where(BookingItem.booking_id.in_(booking_ids))
                    )
                await cleanup_session.execute(
                    delete(BookingRequest).where(BookingRequest.request_id.in_(request_ids))
                )
                await cleanup_session.execute(
                    update(BookingSlot)
                    .where(BookingSlot.id == slot_id)
                    .values(
                        status=BookingSlotStatus.AVAILABLE,
                        current_booking_id=None,
                    )
                )
                if booking_ids:
                    await cleanup_session.execute(
                        delete(Booking).where(Booking.id.in_(booking_ids))
                    )
                await cleanup_session.execute(
                    delete(BookingSlot).where(BookingSlot.id == slot_id)
                )
                await cleanup_session.execute(
                    delete(ReleaseBatch).where(ReleaseBatch.id == batch_id)
                )
                if user_ids:
                    await cleanup_session.execute(
                        delete(UserDailyUsage).where(UserDailyUsage.user_id.in_(user_ids))
                    )
                    await cleanup_session.execute(delete(User).where(User.id.in_(user_ids)))
        await engine.dispose()


@pytest.mark.integration
async def test_worker_serializes_one_hundred_requests_to_two_confirmed_hours() -> None:
    engine = create_async_engine(
        get_settings().postgres_dsn,
        pool_size=10,
        max_overflow=0,
        pool_pre_ping=True,
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    suffix = uuid4().hex[:20]
    service_date = date(2090, 1, 2) + timedelta(days=int(suffix[:4], 16) % 3_000)
    request_ids = [uuid4() for _ in range(100)]
    user_id = 0
    batch_id = 0
    slot_ids: list[int] = []

    try:
        async with sessions() as setup_session, setup_session.begin():
            user = await _create_user(setup_session, f"quota-{suffix}")
            user_id = user.id
            periods = [
                (court_id, hour)
                for hour in range(8, 22)
                for court_id in range(1, 16)
            ][:100]
            batch, slots = await _create_inventory(setup_session, service_date, periods)
            batch_id = batch.id
            slot_ids = [slot.id for slot in slots]

        processor = BookingEventProcessor(sessions)

        def event_payload(request_id: UUID, slot_id: int) -> dict[str, str]:
            command = RedisReservationCommand(
                request_id=request_id,
                user_id=user_id,
                batch_id=batch_id,
                service_date=service_date,
                slot_ids=(slot_id,),
            )
            return {
                "request_id": str(command.request_id),
                "user_id": str(command.user_id),
                "batch_id": str(command.batch_id),
                "service_date": command.service_date.isoformat(),
                "slot_ids": str(slot_id),
                "fingerprint": reservation_fingerprint(command),
            }

        results = await asyncio.gather(
            *(
                processor.process(event_payload(request_id, slot_id))
                for request_id, slot_id in zip(request_ids, slot_ids, strict=True)
            )
        )

        assert sum(
            result.status is BookingPersistenceStatus.CONFIRMED for result in results
        ) == 2
        assert sum(result.status is BookingPersistenceStatus.FAILED for result in results) == 98

        async with sessions() as verify_session:
            usage = await verify_session.get(UserDailyUsage, (user_id, service_date))
            confirmed_item_count = await verify_session.scalar(
                select(func.count())
                .select_from(BookingItem)
                .where(
                    BookingItem.user_id == user_id,
                    BookingItem.service_date == service_date,
                )
            )
            confirmed_request_count = await verify_session.scalar(
                select(func.count())
                .select_from(BookingRequest)
                .where(
                    BookingRequest.user_id == user_id,
                    BookingRequest.status == BookingRequestStatus.CONFIRMED,
                )
            )
            assert usage is not None
            assert usage.used_slot_count == 2
            assert confirmed_item_count == 2
            assert confirmed_request_count == 2
    finally:
        if batch_id:
            async with sessions() as cleanup_session, cleanup_session.begin():
                booking_ids = list(
                    (
                        await cleanup_session.scalars(
                            select(Booking.id).where(Booking.request_id.in_(request_ids))
                        )
                    ).all()
                )
                if booking_ids:
                    await cleanup_session.execute(
                        delete(BookingStatusLog).where(
                            BookingStatusLog.booking_id.in_(booking_ids)
                        )
                    )
                    await cleanup_session.execute(
                        delete(BookingItem).where(BookingItem.booking_id.in_(booking_ids))
                    )
                await cleanup_session.execute(
                    delete(BookingRequest).where(BookingRequest.request_id.in_(request_ids))
                )
                if slot_ids:
                    await cleanup_session.execute(
                        update(BookingSlot)
                        .where(BookingSlot.id.in_(slot_ids))
                        .values(status=BookingSlotStatus.AVAILABLE, current_booking_id=None)
                    )
                if booking_ids:
                    await cleanup_session.execute(
                        delete(Booking).where(Booking.id.in_(booking_ids))
                    )
                await cleanup_session.execute(
                    delete(UserDailyUsage).where(UserDailyUsage.user_id == user_id)
                )
                await cleanup_session.execute(
                    delete(BookingSlot).where(BookingSlot.release_batch_id == batch_id)
                )
                await cleanup_session.execute(
                    delete(ReleaseBatch).where(ReleaseBatch.id == batch_id)
                )
                await cleanup_session.execute(delete(User).where(User.id == user_id))
        await engine.dispose()
