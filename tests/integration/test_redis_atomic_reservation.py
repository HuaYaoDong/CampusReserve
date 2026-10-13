import asyncio
from collections import Counter
from contextlib import asynccontextmanager
from datetime import date
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from campus_reserve.bookings.services.redis_reservation import (
    RedisAtomicReservationService,
    RedisConfirmedUsage,
    RedisInventoryCode,
    RedisInventorySlot,
    RedisReservationCode,
    RedisReservationCommand,
    RedisReservationKeyspace,
    RedisSlotState,
)
from campus_reserve.shared.config import get_settings


@asynccontextmanager
async def isolated_service(
    *,
    ttl_seconds: int = 60,
    max_daily_slots: int = 2,
):
    redis = Redis.from_url(get_settings().redis_url, decode_responses=True)
    token = uuid4().hex
    keyspace = RedisReservationKeyspace(f"campus-reserve:{{booking-test-{token}}}")
    service = RedisAtomicReservationService(
        redis,
        keyspace=keyspace,
        ttl_seconds=ttl_seconds,
        max_daily_slots=max_daily_slots,
    )
    try:
        yield redis, service, keyspace
    finally:
        keys = [key async for key in redis.scan_iter(match=f"{keyspace.prefix}:*")]
        if keys:
            await redis.delete(*keys)
        await redis.aclose()


def command(
    *,
    request_id=None,
    user_id: int = 1,
    batch_id: int = 100,
    slot_ids: tuple[int, ...],
) -> RedisReservationCommand:
    return RedisReservationCommand(
        request_id=request_id or uuid4(),
        user_id=user_id,
        batch_id=batch_id,
        service_date=date(2030, 1, 2),
        slot_ids=slot_ids,
    )


@pytest.mark.integration
async def test_inventory_initialization_is_idempotent_and_has_ttl() -> None:
    async with isolated_service() as (redis, service, keys):
        slots = [
            RedisInventorySlot(1, "08:00-09:00"),
            RedisInventorySlot(2, "09:00-10:00"),
        ]
        first = await service.initialize_inventory(100, date(2030, 1, 2), slots)
        replay = await service.initialize_inventory(
            100, date(2030, 1, 2), list(reversed(slots))
        )
        conflict = await service.initialize_inventory(
            100,
            date(2030, 1, 2),
            [RedisInventorySlot(1, "08:00-09:00")],
        )

        assert first.code is RedisInventoryCode.INITIALIZED
        assert first.replayed is False
        assert first.slot_count == 2
        assert replay.code is RedisInventoryCode.INITIALIZED
        assert replay.replayed is True
        assert conflict.code is RedisInventoryCode.INVENTORY_CONFLICT
        assert 0 < await redis.ttl(keys.slots(100)) <= 60
        assert 0 < await redis.ttl(keys.periods(100)) <= 60


@pytest.mark.integration
async def test_atomic_reservation_is_idempotent_and_emits_one_event() -> None:
    async with isolated_service() as (redis, service, keys):
        await service.initialize_inventory(
            100,
            date(2030, 1, 2),
            [
                RedisInventorySlot(1, "08:00-09:00"),
                RedisInventorySlot(2, "10:00-11:00"),
                RedisInventorySlot(3, "12:00-13:00"),
            ],
        )
        request_id = uuid4()

        first = await service.reserve(command(request_id=request_id, slot_ids=(1, 2)))
        replay = await service.reserve(command(request_id=request_id, slot_ids=(2, 1)))

        assert first.code is RedisReservationCode.RESERVED
        assert first.reserved is True
        assert first.replayed is False
        assert replay.code is RedisReservationCode.RESERVED
        assert replay.replayed is True
        assert replay.stream_id == first.stream_id
        assert await redis.xlen(keys.stream) == 1
        assert await redis.get(keys.usage(100, 1)) == "2"
        assert await redis.scard(keys.user_periods(100, 1)) == 2
        states = await redis.hmget(keys.slots(100), "1", "2", "3")
        assert states[0].startswith(f"RESERVED:{request_id}:")
        assert states[1].startswith(f"RESERVED:{request_id}:")
        assert states[2] == "AVAILABLE"
        assert 0 < await redis.ttl(keys.request(request_id)) <= 60


@pytest.mark.integration
async def test_daily_limit_and_failed_request_replay() -> None:
    async with isolated_service() as (redis, service, keys):
        await service.initialize_inventory(
            100,
            date(2030, 1, 2),
            [RedisInventorySlot(slot_id, f"{slot_id:02}:00") for slot_id in range(1, 5)],
        )
        await service.reserve(command(slot_ids=(1,)))
        await service.reserve(command(slot_ids=(2,)))
        rejected_id = uuid4()

        rejected = await service.reserve(command(request_id=rejected_id, slot_ids=(3,)))
        replay = await service.reserve(command(request_id=rejected_id, slot_ids=(3,)))
        conflict = await service.reserve(command(request_id=rejected_id, slot_ids=(4,)))

        assert rejected.code is RedisReservationCode.DAILY_LIMIT_EXCEEDED
        assert rejected.replayed is False
        assert replay.code is RedisReservationCode.DAILY_LIMIT_EXCEEDED
        assert replay.replayed is True
        assert conflict.code is RedisReservationCode.IDEMPOTENCY_CONFLICT
        assert conflict.replayed is True
        assert await redis.get(keys.usage(100, 1)) == "2"
        assert await redis.xlen(keys.stream) == 2
        assert await redis.hget(keys.slots(100), "3") == "AVAILABLE"


@pytest.mark.integration
async def test_confirmed_database_usage_is_counted_with_pending_reservations() -> None:
    async with isolated_service() as (redis, service, keys):
        await service.initialize_inventory(
            100,
            date(2030, 1, 2),
            [
                RedisInventorySlot(1, "08:00-09:00", RedisSlotState.UNAVAILABLE),
                RedisInventorySlot(2, "10:00-11:00"),
                RedisInventorySlot(3, "12:00-13:00"),
                RedisInventorySlot(4, "08:00-09:00"),
            ],
            [RedisConfirmedUsage(user_id=1, period_tokens=("08:00-09:00",))],
        )

        parallel_to_confirmed = await service.reserve(command(slot_ids=(4,)))
        over_limit = await service.reserve(command(slot_ids=(3,)))

        assert parallel_to_confirmed.code is RedisReservationCode.RESERVED
        assert parallel_to_confirmed.used_slot_count == 2
        assert over_limit.code is RedisReservationCode.DAILY_LIMIT_EXCEEDED
        assert await redis.get(keys.usage(100, 1)) == "2"
        assert (await redis.hget(keys.slots(100), "4")).startswith("RESERVED:")


@pytest.mark.integration
async def test_same_period_can_be_reserved_on_two_courts_by_one_user() -> None:
    async with isolated_service() as (redis, service, keys):
        await service.initialize_inventory(
            100,
            date(2030, 1, 2),
            [
                RedisInventorySlot(1, "08:00-09:00"),
                RedisInventorySlot(2, "08:00-09:00"),
                RedisInventorySlot(3, "09:00-10:00"),
                RedisInventorySlot(4, "09:00-10:00"),
            ],
        )

        same_request = await service.reserve(command(slot_ids=(1, 2)))
        first = await service.reserve(command(user_id=2, slot_ids=(3,)))
        later_parallel = await service.reserve(command(user_id=2, slot_ids=(4,)))

        assert same_request.code is RedisReservationCode.RESERVED
        assert same_request.used_slot_count == 2
        assert first.code is RedisReservationCode.RESERVED
        assert later_parallel.code is RedisReservationCode.RESERVED
        assert later_parallel.used_slot_count == 2
        assert await redis.xlen(keys.stream) == 3


@pytest.mark.integration
async def test_concurrent_requests_never_oversell_one_slot() -> None:
    async with isolated_service() as (redis, service, keys):
        await service.initialize_inventory(
            100,
            date(2030, 1, 2),
            [RedisInventorySlot(1, "08:00-09:00")],
        )

        results = await asyncio.gather(
            *(
                service.reserve(command(user_id=user_id, slot_ids=(1,)))
                for user_id in range(1, 101)
            )
        )
        counts = Counter(result.code for result in results)

        assert counts[RedisReservationCode.RESERVED] == 1
        assert counts[RedisReservationCode.SLOT_NOT_AVAILABLE] == 99
        assert await redis.xlen(keys.stream) == 1
        assert (await redis.hget(keys.slots(100), "1")).startswith("RESERVED:")


@pytest.mark.integration
async def test_concurrent_requests_enforce_one_users_daily_limit() -> None:
    async with isolated_service() as (redis, service, keys):
        await service.initialize_inventory(
            100,
            date(2030, 1, 2),
            [RedisInventorySlot(slot_id, f"period-{slot_id}") for slot_id in range(1, 101)],
        )

        results = await asyncio.gather(
            *(service.reserve(command(slot_ids=(slot_id,))) for slot_id in range(1, 101))
        )
        counts = Counter(result.code for result in results)

        assert counts[RedisReservationCode.RESERVED] == 2
        assert counts[RedisReservationCode.DAILY_LIMIT_EXCEEDED] == 98
        assert await redis.get(keys.usage(100, 1)) == "2"
        assert await redis.xlen(keys.stream) == 2
