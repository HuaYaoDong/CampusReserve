from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from redis.asyncio import Redis
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import campus_reserve.models  # noqa: F401
from campus_reserve.bookings.models import (
    Booking,
    BookingItem,
    BookingRequest,
    BookingStatusLog,
)
from campus_reserve.bookings.services.redis_reservation import (
    RedisAtomicReservationService,
    RedisInventorySlot,
    RedisReservationCommand,
    RedisReservationKeyspace,
)
from campus_reserve.bookings.services.stream_booking import (
    BookingEventProcessor,
    BookingPersistenceResult,
    BookingPersistenceStatus,
    BookingReservationEvent,
    BookingStreamConsumer,
)
from campus_reserve.inventory.models import BookingSlot, ReleaseBatch
from campus_reserve.shared.config import get_settings
from campus_reserve.shared.enums import (
    BookingRequestStatus,
    BookingSlotStatus,
    ReleaseBatchStatus,
    UserRole,
    UserStatus,
)
from campus_reserve.users.models import User, UserDailyUsage


async def cleanup_redis(redis: Redis, keyspace: RedisReservationKeyspace) -> None:
    keys = [key async for key in redis.scan_iter(match=f"{keyspace.prefix}:*")]
    if keys:
        await redis.delete(*keys)


@pytest.mark.integration
async def test_worker_persists_event_and_recovers_the_commit_before_ack_window() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.postgres_dsn, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    token = uuid4().hex
    keyspace = RedisReservationKeyspace(f"campus-reserve:{{worker-test-{token}}}")
    group = f"worker-test-{token}"
    service_date = date(2080, 1, 2)
    request_id = uuid4()
    user_id = 0
    batch_id = 0
    slot_ids: list[int] = []

    try:
        async with sessions() as setup, setup.begin():
            user = User(
                username=f"worker-test-{token}",
                password_hash="test-only",
                student_no=f"W{token[:20]}",
                display_name="Worker 测试学生",
                role=UserRole.STUDENT,
                status=UserStatus.ACTIVE,
            )
            setup.add(user)
            await setup.flush()
            user_id = user.id
            batch = ReleaseBatch(
                venue_id=1,
                service_date=service_date,
                release_at=datetime.now(UTC) - timedelta(minutes=1),
                status=ReleaseBatchStatus.OPEN,
                slot_count=2,
                rule_snapshot={"max_slots_per_user_per_day": 2},
                opened_at=datetime.now(UTC),
            )
            setup.add(batch)
            await setup.flush()
            batch_id = batch.id
            slots = [
                BookingSlot(
                    court_id=1,
                    release_batch_id=batch.id,
                    service_date=service_date,
                    start_at=datetime(2080, 1, 2, hour, tzinfo=UTC),
                    end_at=datetime(2080, 1, 2, hour, tzinfo=UTC) + timedelta(hours=1),
                    status=BookingSlotStatus.AVAILABLE,
                )
                for hour in (8, 10)
            ]
            setup.add_all(slots)
            await setup.flush()
            slot_ids = [slot.id for slot in slots]

        reservation = RedisAtomicReservationService(redis, keyspace=keyspace, ttl_seconds=60)
        await reservation.initialize_inventory(
            batch_id,
            service_date,
            [
                RedisInventorySlot(slot_ids[0], "08:00-09:00"),
                RedisInventorySlot(slot_ids[1], "10:00-11:00"),
            ],
        )
        processor = BookingEventProcessor(sessions)
        first_consumer = BookingStreamConsumer(
            redis,
            processor,
            stream=keyspace.stream,
            dead_letter_stream=f"{keyspace.stream}:dead-letter",
            group=group,
            consumer_name="worker-a",
            keyspace=keyspace,
            result_ttl_seconds=60,
        )
        await first_consumer.ensure_group()
        reserve_result = await reservation.reserve(
            RedisReservationCommand(
                request_id=request_id,
                user_id=user_id,
                batch_id=batch_id,
                service_date=service_date,
                slot_ids=tuple(slot_ids),
            )
        )
        assert reserve_result.reserved is True
        entries = await first_consumer.read_new(block_ms=1)
        assert len(entries) == 1
        message_id, payload = entries[0]

        # Simulate a crash after the database commit but before XACK.
        first_persist = await processor.process(payload)
        assert first_persist.status is BookingPersistenceStatus.CONFIRMED
        second_consumer = BookingStreamConsumer(
            redis,
            processor,
            stream=keyspace.stream,
            dead_letter_stream=f"{keyspace.stream}:dead-letter",
            group=group,
            consumer_name="worker-b",
            keyspace=keyspace,
            result_ttl_seconds=60,
        )
        claimed = await second_consumer.claim_stale(min_idle_ms=0)
        assert claimed[0][0] == message_id
        replay = await second_consumer.process_message(*claimed[0])

        assert replay.status is BookingPersistenceStatus.CONFIRMED
        assert replay.replayed is True
        request_state = await redis.hgetall(keyspace.request(request_id))
        assert request_state["persistence_status"] == "CONFIRMED"
        assert request_state["persistence_replayed"] == "1"
        assert request_state["booking_id"]
        assert (await redis.xpending(keyspace.stream, group))["pending"] == 0

        async with sessions() as verify:
            assert (
                await verify.scalar(
                    select(func.count())
                    .select_from(Booking)
                    .where(Booking.request_id == request_id)
                )
                == 1
            )
            assert (
                await verify.scalar(
                    select(func.count())
                    .select_from(BookingItem)
                    .where(BookingItem.user_id == user_id)
                )
                == 2
            )
            request = await verify.get(BookingRequest, request_id)
            usage = await verify.get(UserDailyUsage, (user_id, service_date))
            assert request is not None
            assert request.status is BookingRequestStatus.CONFIRMED
            assert usage is not None
            assert usage.used_slot_count == 2
    finally:
        await cleanup_redis(redis, keyspace)
        if batch_id:
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
                if slot_ids:
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
        await redis.aclose()
        await engine.dispose()


@pytest.mark.integration
async def test_worker_moves_invalid_event_to_dead_letter_and_acknowledges_it() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.postgres_dsn, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    token = uuid4().hex
    keyspace = RedisReservationKeyspace(f"campus-reserve:{{worker-poison-{token}}}")
    group = f"worker-poison-{token}"
    dead_letter = f"{keyspace.stream}:dead-letter"
    consumer = BookingStreamConsumer(
        redis,
        BookingEventProcessor(sessions),
        stream=keyspace.stream,
        dead_letter_stream=dead_letter,
        group=group,
        consumer_name="worker-test",
        keyspace=keyspace,
        result_ttl_seconds=60,
    )

    try:
        await consumer.ensure_group()
        await redis.xadd(keyspace.stream, {"request_id": str(UUID(int=0))})
        entries = await consumer.read_new(block_ms=1)
        result = await consumer.process_message(*entries[0])

        assert result.status is BookingPersistenceStatus.DEAD_LETTERED
        assert await redis.xlen(dead_letter) == 1
        assert (await redis.xpending(keyspace.stream, group))["pending"] == 0
    finally:
        await cleanup_redis(redis, keyspace)
        await redis.aclose()
        await engine.dispose()


@pytest.mark.integration
async def test_worker_compensates_redis_reservation_after_permanent_database_failure() -> None:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    token = uuid4().hex
    keyspace = RedisReservationKeyspace(f"campus-reserve:{{worker-compensate-{token}}}")
    group = f"worker-compensate-{token}"
    request_id = uuid4()
    service_date = date(2080, 1, 2)

    class PermanentlyFailingProcessor:
        async def process(self, payload: dict[str, str]) -> BookingPersistenceResult:
            command = BookingReservationEvent.from_payload(payload).command
            return BookingPersistenceResult(
                request_id=command.request_id,
                status=BookingPersistenceStatus.FAILED,
                replayed=False,
                command=command,
                error_code="SLOT_NOT_AVAILABLE",
            )

    try:
        reservations = RedisAtomicReservationService(redis, keyspace=keyspace, ttl_seconds=60)
        await reservations.initialize_inventory(
            100,
            service_date,
            [RedisInventorySlot(1, "08:00-09:00")],
        )
        consumer = BookingStreamConsumer(
            redis,
            PermanentlyFailingProcessor(),
            stream=keyspace.stream,
            dead_letter_stream=f"{keyspace.stream}:dead-letter",
            group=group,
            consumer_name="worker-test",
            keyspace=keyspace,
            result_ttl_seconds=60,
        )
        await consumer.ensure_group()
        reserved = await reservations.reserve(
            RedisReservationCommand(
                request_id=request_id,
                user_id=1,
                batch_id=100,
                service_date=service_date,
                slot_ids=(1,),
            )
        )
        assert reserved.reserved is True
        entries = await consumer.read_new(block_ms=1)
        result = await consumer.process_message(*entries[0])

        assert result.status is BookingPersistenceStatus.FAILED
        assert await redis.hget(keyspace.slots(100), "1") == "AVAILABLE"
        assert await redis.get(keyspace.usage(100, 1)) is None
        assert await redis.scard(keyspace.user_periods(100, 1)) == 0
        request_state = await redis.hgetall(keyspace.request(request_id))
        assert request_state["persistence_status"] == "FAILED"
        assert request_state["compensation_status"] == "RELEASED"
        assert request_state["persistence_error_code"] == "SLOT_NOT_AVAILABLE"
        assert (await redis.xpending(keyspace.stream, group))["pending"] == 0
    finally:
        await cleanup_redis(redis, keyspace)
        await redis.aclose()


@pytest.mark.integration
async def test_invalid_reserved_event_releases_claim_and_records_failed_result() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.postgres_dsn, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    keyspace = RedisReservationKeyspace(f"campus-reserve:{{poison-claim-{uuid4().hex}}}")
    service_date = date(2080, 1, 2)
    request_id = uuid4()
    group = f"poison-claim-{uuid4().hex}"
    user_id = 0
    batch_id = 0
    try:
        async with sessions() as setup, setup.begin():
            user = User(
                username=f"poison-claim-{uuid4().hex}",
                password_hash="test-only",
                student_no=f"P{uuid4().hex[:20]}",
                display_name="异常事件测试学生",
                role=UserRole.STUDENT,
                status=UserStatus.ACTIVE,
            )
            setup.add(user)
            await setup.flush()
            user_id = user.id
            batch = ReleaseBatch(
                venue_id=1,
                service_date=service_date,
                release_at=datetime.now(UTC) - timedelta(minutes=1),
                status=ReleaseBatchStatus.OPEN,
                slot_count=1,
                rule_snapshot={},
                opened_at=datetime.now(UTC),
            )
            setup.add(batch)
            await setup.flush()
            batch_id = batch.id
        reservations = RedisAtomicReservationService(redis, keyspace=keyspace, ttl_seconds=60)
        await reservations.initialize_inventory(
            batch_id, service_date, [RedisInventorySlot(1, "08:00-09:00")]
        )
        consumer = BookingStreamConsumer(
            redis,
            BookingEventProcessor(sessions),
            stream=keyspace.stream,
            dead_letter_stream=f"{keyspace.stream}:dead-letter",
            group=group,
            consumer_name="worker-test",
            keyspace=keyspace,
            result_ttl_seconds=60,
        )
        await consumer.ensure_group()
        await reservations.reserve(
            RedisReservationCommand(request_id, user_id, batch_id, service_date, (1,))
        )
        message_id, payload = (await consumer.read_new(block_ms=1))[0]
        result = await consumer.process_message(message_id, {**payload, "fingerprint": "invalid"})

        assert result.status is BookingPersistenceStatus.FAILED
        assert result.error_code == "INVALID_EVENT_PAYLOAD"
        assert await redis.hget(keyspace.slots(batch_id), "1") == "AVAILABLE"
        assert await redis.get(keyspace.usage(batch_id, user_id)) is None
        assert (await redis.hgetall(keyspace.request(request_id)))["persistence_status"] == "FAILED"
        async with sessions() as verify:
            failed_request = await verify.get(BookingRequest, request_id)
            assert failed_request is not None
            assert failed_request.status is BookingRequestStatus.FAILED
            assert failed_request.error_code == "INVALID_EVENT_PAYLOAD"
        late_replay = await BookingEventProcessor(sessions).process(payload)
        assert late_replay.status is BookingPersistenceStatus.FAILED
        assert late_replay.replayed is True
        assert await redis.xlen(f"{keyspace.stream}:dead-letter") == 1
        assert (await redis.xpending(keyspace.stream, group))["pending"] == 0
    finally:
        await cleanup_redis(redis, keyspace)
        if batch_id:
            async with sessions() as cleanup, cleanup.begin():
                await cleanup.execute(
                    delete(BookingRequest).where(BookingRequest.request_id == request_id)
                )
                await cleanup.execute(delete(ReleaseBatch).where(ReleaseBatch.id == batch_id))
                await cleanup.execute(delete(User).where(User.id == user_id))
        await redis.aclose()
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.parametrize(
    ("database_status", "expected_slot"),
    [
        (BookingPersistenceStatus.FAILED, "AVAILABLE"),
        (BookingPersistenceStatus.CONFIRMED, "RESERVED"),
    ],
)
async def test_worker_reconciles_after_retry_limit(
    database_status: BookingPersistenceStatus, expected_slot: str
) -> None:
    redis = Redis.from_url(get_settings().redis_url, decode_responses=True)
    keyspace = RedisReservationKeyspace(f"campus-reserve:{{retry-limit-{uuid4().hex}}}")
    service_date = date(2080, 1, 2)
    request_id = uuid4()
    group = f"retry-limit-{uuid4().hex}"

    class FailingProcessor:
        async def process(self, payload: dict[str, str]) -> BookingPersistenceResult:
            raise RuntimeError("temporary failure")

        async def reconcile(
            self, command: RedisReservationCommand, error_code: str
        ) -> BookingPersistenceResult:
            return BookingPersistenceResult(
                request_id=command.request_id,
                status=database_status,
                replayed=False,
                command=command,
                error_code=(
                    error_code if database_status is BookingPersistenceStatus.FAILED else None
                ),
                booking_id=123 if database_status is BookingPersistenceStatus.CONFIRMED else None,
            )

    try:
        reservations = RedisAtomicReservationService(redis, keyspace=keyspace, ttl_seconds=60)
        await reservations.initialize_inventory(
            100, service_date, [RedisInventorySlot(1, "08:00-09:00")]
        )
        consumer = BookingStreamConsumer(
            redis,
            FailingProcessor(),
            stream=keyspace.stream,
            dead_letter_stream=f"{keyspace.stream}:dead-letter",
            group=group,
            consumer_name="worker-test",
            keyspace=keyspace,
            result_ttl_seconds=60,
            max_deliveries=2,
        )
        await consumer.ensure_group()
        await reservations.reserve(
            RedisReservationCommand(request_id, 1, 100, service_date, (1,))
        )
        message_id, payload = (await consumer.read_new(block_ms=1))[0]
        with pytest.raises(RuntimeError, match="temporary failure"):
            await consumer.process_message(message_id, payload)
        assert (await redis.xpending(keyspace.stream, group))["pending"] == 1

        claimed = await consumer.claim_stale(min_idle_ms=0)
        result = await consumer.process_message(*claimed[0])
        assert result.status is database_status
        slot_state = await redis.hget(keyspace.slots(100), "1")
        assert slot_state.startswith(expected_slot)
        request_state = await redis.hgetall(keyspace.request(request_id))
        assert request_state["persistence_status"] == database_status.value
        if database_status is BookingPersistenceStatus.FAILED:
            assert result.error_code == "PERSISTENCE_RETRY_EXHAUSTED"
        else:
            assert request_state["booking_id"] == "123"
        assert await redis.xlen(f"{keyspace.stream}:dead-letter") == 1
        assert (await redis.xpending(keyspace.stream, group))["pending"] == 0
    finally:
        await cleanup_redis(redis, keyspace)
        await redis.aclose()


@pytest.mark.integration
async def test_worker_keeps_claim_when_database_outcome_cannot_be_checked() -> None:
    redis = Redis.from_url(get_settings().redis_url, decode_responses=True)
    keyspace = RedisReservationKeyspace(f"campus-reserve:{{uncertain-{uuid4().hex}}}")
    group = f"uncertain-{uuid4().hex}"
    request_id = uuid4()
    service_date = date(2080, 1, 2)

    class UnavailableProcessor:
        async def process(self, payload: dict[str, str]) -> BookingPersistenceResult:
            raise RuntimeError("database unavailable")

        async def reconcile(
            self, command: RedisReservationCommand, error_code: str
        ) -> BookingPersistenceResult:
            raise RuntimeError("database unavailable")

    try:
        reservations = RedisAtomicReservationService(redis, keyspace=keyspace, ttl_seconds=60)
        await reservations.initialize_inventory(
            100, service_date, [RedisInventorySlot(1, "08:00-09:00")]
        )
        consumer = BookingStreamConsumer(
            redis,
            UnavailableProcessor(),
            stream=keyspace.stream,
            dead_letter_stream=f"{keyspace.stream}:dead-letter",
            group=group,
            consumer_name="worker-test",
            keyspace=keyspace,
            result_ttl_seconds=60,
            max_deliveries=1,
        )
        await consumer.ensure_group()
        await reservations.reserve(
            RedisReservationCommand(request_id, 1, 100, service_date, (1,))
        )
        message_id, payload = (await consumer.read_new(block_ms=1))[0]
        with pytest.raises(RuntimeError, match="database unavailable"):
            await consumer.process_message(message_id, payload)
        assert (await redis.hget(keyspace.slots(100), "1")).startswith("RESERVED:")
        assert (await redis.hgetall(keyspace.request(request_id))).get("persistence_status") is None
        assert await redis.xlen(f"{keyspace.stream}:dead-letter") == 0
        assert (await redis.xpending(keyspace.stream, group))["pending"] == 1
    finally:
        await cleanup_redis(redis, keyspace)
        await redis.aclose()
