from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import campus_reserve.models  # noqa: F401
from apps.api.main import app
from campus_reserve.auth.security import create_access_token
from campus_reserve.bookings.models import (
    Booking,
    BookingItem,
    BookingRequest,
    BookingStatusLog,
)
from campus_reserve.bookings.services.redis_reservation import (
    RedisAtomicReservationService,
    RedisInventorySlot,
    RedisReservationKeyspace,
)
from campus_reserve.bookings.services.stream_booking import (
    BookingEventProcessor,
    BookingStreamConsumer,
)
from campus_reserve.inventory.models import BookingSlot, ReleaseBatch
from campus_reserve.shared.config import get_settings
from campus_reserve.shared.database import get_session
from campus_reserve.shared.enums import (
    BookingSlotStatus,
    ReleaseBatchStatus,
    UserRole,
    UserStatus,
)
from campus_reserve.users.models import User, UserDailyUsage


@pytest.mark.integration
async def test_async_api_reserves_then_returns_worker_final_result() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.postgres_dsn, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    keyspace = RedisReservationKeyspace()
    token = uuid4().hex
    group = f"async-api-test-{token}"
    consumer_name = f"consumer-{token}"
    request_id = uuid4()
    service_date = date(2090, 1, 2)
    user_id = 0
    other_user_id = 0
    batch_id = 0
    slot_ids: list[int] = []
    message_ids: list[str] = []

    async def override_session():
        async with sessions() as session, session.begin():
            yield session

    try:
        async with sessions() as setup, setup.begin():
            user = User(
                username=f"async-api-{token}",
                password_hash="test-only",
                student_no=f"A{token[:20]}",
                display_name="异步 API 测试学生",
                role=UserRole.STUDENT,
                status=UserStatus.ACTIVE,
            )
            setup.add(user)
            await setup.flush()
            user_id = user.id
            access_token, _ = create_access_token(user)
            other_user = User(
                username=f"async-api-other-{token}",
                password_hash="test-only",
                student_no=f"B{token[:20]}",
                display_name="异步 API 测试学生二",
                role=UserRole.STUDENT,
                status=UserStatus.ACTIVE,
            )
            setup.add(other_user)
            await setup.flush()
            other_user_id = other_user.id
            other_access_token, _ = create_access_token(other_user)
            batch = ReleaseBatch(
                venue_id=1,
                service_date=service_date,
                release_at=datetime.now(UTC) - timedelta(minutes=1),
                status=ReleaseBatchStatus.OPEN,
                slot_count=1,
                rule_snapshot={"max_slots_per_user_per_day": 2},
                opened_at=datetime.now(UTC),
            )
            setup.add(batch)
            await setup.flush()
            batch_id = batch.id
            slot = BookingSlot(
                court_id=1,
                release_batch_id=batch.id,
                service_date=service_date,
                start_at=datetime(2090, 1, 2, 8, tzinfo=UTC),
                end_at=datetime(2090, 1, 2, 9, tzinfo=UTC),
                status=BookingSlotStatus.AVAILABLE,
            )
            setup.add(slot)
            await setup.flush()
            slot_ids = [slot.id]

        reservations = RedisAtomicReservationService(
            redis,
            keyspace=keyspace,
            ttl_seconds=60,
        )
        await reservations.initialize_inventory(
            batch_id,
            service_date,
            [RedisInventorySlot(slot_ids[0], "08:00-09:00")],
        )
        await redis.xgroup_create(keyspace.stream, group, id="$", mkstream=True)
        consumer = BookingStreamConsumer(
            redis,
            BookingEventProcessor(sessions),
            stream=keyspace.stream,
            dead_letter_stream=f"{keyspace.stream}:dead-letter",
            group=group,
            consumer_name=consumer_name,
            keyspace=keyspace,
            result_ttl_seconds=60,
        )
        app.dependency_overrides[get_session] = override_session
        transport = ASGITransport(app=app)
        headers = {"Authorization": f"Bearer {access_token}"}
        body = {
            "request_id": str(request_id),
            "release_batch_id": batch_id,
            "service_date": service_date.isoformat(),
            "slot_ids": slot_ids,
        }

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            before_reservation = await client.get(
                f"/api/v1/venues/1/inventory?service_date={service_date}",
                headers=headers,
            )
            assert before_reservation.status_code == 200
            assert before_reservation.json()["available_count"] == 1

            accepted = await client.post(
                "/api/v1/booking-requests",
                headers=headers,
                json=body,
            )
            assert accepted.status_code == 202
            assert accepted.json()["status"] == "PROCESSING"

            processing = await client.get(
                f"/api/v1/booking-requests/{request_id}",
                headers=headers,
            )
            assert processing.status_code == 200
            assert processing.json()["status"] == "PROCESSING"

            pending_inventory = await client.get(
                f"/api/v1/venues/1/inventory?service_date={service_date}",
                headers=headers,
            )
            assert pending_inventory.status_code == 200
            assert pending_inventory.json()["available_count"] == 0
            assert pending_inventory.json()["courts"][0]["slots"][0]["status"] == "PROCESSING"

            conflicting_body = {**body, "slot_ids": [slot_ids[0] + 1]}
            pending_conflict = await client.post(
                "/api/v1/booking-requests", headers=headers, json=conflicting_body
            )
            assert pending_conflict.status_code == 409
            assert pending_conflict.json()["status"] == "FAILED"
            assert pending_conflict.json()["error_code"] == "IDEMPOTENCY_CONFLICT"

            entries = await consumer.read_new(block_ms=1)
            assert len(entries) == 1
            message_ids = [entries[0][0]]
            persisted = await consumer.process_message(*entries[0])
            assert persisted.status == "CONFIRMED"

            confirmed = await client.get(
                f"/api/v1/booking-requests/{request_id}",
                headers=headers,
            )
            assert confirmed.status_code == 200
            confirmed_payload = confirmed.json()
            assert confirmed_payload["status"] == "CONFIRMED"
            assert confirmed_payload["booking"]["items"][0]["slot_id"] == slot_ids[0]

            confirmed_inventory = await client.get(
                f"/api/v1/venues/1/inventory?service_date={service_date}",
                headers=headers,
            )
            assert confirmed_inventory.status_code == 200
            assert confirmed_inventory.json()["courts"][0]["slots"][0]["status"] == "BOOKED"

            replay = await client.post(
                "/api/v1/booking-requests",
                headers=headers,
                json=body,
            )
            assert replay.status_code == 200
            assert replay.json()["status"] == "CONFIRMED"
            assert replay.json()["replayed"] is True

            confirmed_conflict = await client.post(
                "/api/v1/booking-requests", headers=headers, json=conflicting_body
            )
            assert confirmed_conflict.status_code == 409
            assert confirmed_conflict.json()["status"] == "FAILED"
            assert confirmed_conflict.json()["error_code"] == "IDEMPOTENCY_CONFLICT"
            assert confirmed_conflict.json()["booking"] is None
            other_user_conflict = await client.post(
                "/api/v1/booking-requests",
                headers={"Authorization": f"Bearer {other_access_token}"},
                json=body,
            )
            assert other_user_conflict.status_code == 409
            assert other_user_conflict.json()["error_code"] == "IDEMPOTENCY_CONFLICT"
            assert other_user_conflict.json()["booking"] is None
            assert await redis.xlen(keyspace.stream) >= 1
    finally:
        app.dependency_overrides.pop(get_session, None)
        if message_ids:
            await redis.xdel(keyspace.stream, *message_ids)
        try:
            await redis.xgroup_destroy(keyspace.stream, group)
        except Exception:
            pass
        redis_keys = [
            keyspace.slots(batch_id),
            keyspace.periods(batch_id),
            keyspace.inventory_marker(batch_id),
            keyspace.usage(batch_id, user_id),
            keyspace.user_periods(batch_id, user_id),
            keyspace.request(request_id),
        ]
        if batch_id:
            await redis.delete(*redis_keys)
            async with sessions() as cleanup, cleanup.begin():
                booking_ids = list(
                    (
                        await cleanup.scalars(
                            select(Booking.id).where(Booking.request_id == request_id)
                        )
                    ).all()
                )
                if booking_ids:
                    await cleanup.execute(
                        delete(BookingStatusLog).where(
                            BookingStatusLog.booking_id.in_(booking_ids)
                        )
                    )
                    await cleanup.execute(
                        delete(BookingItem).where(BookingItem.booking_id.in_(booking_ids))
                    )
                await cleanup.execute(
                    delete(BookingRequest).where(BookingRequest.request_id == request_id)
                )
                await cleanup.execute(
                    update(BookingSlot)
                    .where(BookingSlot.id.in_(slot_ids))
                    .values(status=BookingSlotStatus.AVAILABLE, current_booking_id=None)
                )
                if booking_ids:
                    await cleanup.execute(delete(Booking).where(Booking.id.in_(booking_ids)))
                await cleanup.execute(
                    delete(UserDailyUsage).where(UserDailyUsage.user_id == user_id)
                )
                await cleanup.execute(
                    delete(BookingSlot).where(BookingSlot.release_batch_id == batch_id)
                )
                await cleanup.execute(delete(ReleaseBatch).where(ReleaseBatch.id == batch_id))
                await cleanup.execute(delete(User).where(User.id == user_id))
                await cleanup.execute(delete(User).where(User.id == other_user_id))
        await redis.aclose()
        await engine.dispose()
