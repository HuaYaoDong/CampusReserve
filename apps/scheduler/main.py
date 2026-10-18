"""周期任务入口：补齐今天/明天库存、同步 Redis，并归档已结束预约。"""

import asyncio
import os
import signal
import socket
from collections import defaultdict
from datetime import date, datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

import campus_reserve.models  # noqa: F401
from campus_reserve.admin.services import archive_completed_bookings
from campus_reserve.bookings.services.redis_reservation import (
    RedisAtomicReservationService,
    RedisConfirmedUsage,
    RedisInventorySlot,
    RedisReservationKeyspace,
    RedisSlotState,
)
from campus_reserve.inventory.infrastructure.sqlalchemy_repository import (
    SqlAlchemyInventoryRepository,
)
from campus_reserve.inventory.services.inventory import (
    InventoryConfigurationError,
    InventoryService,
)
from campus_reserve.shared.config import get_settings
from campus_reserve.shared.database import async_session_factory, close_database
from campus_reserve.shared.enums import BookingSlotStatus, ReleaseBatchStatus
from campus_reserve.shared.logging import configure_logging, get_logger
from campus_reserve.shared.redis import close_redis, get_redis

settings = get_settings()
configure_logging(settings.log_level)
logger = get_logger(__name__)

# 写入心跳，30 秒过期，保证调度器存活时 Redis 中有心跳键。
async def write_heartbeat() -> None:
    instance = os.getenv("SCHEDULER_INSTANCE", socket.gethostname())
    await get_redis().setex(f"campus-reserve:scheduler:{instance}:heartbeat", 30, "ok")


# 同时检查今天和明天，保证当天剩余时段与次日放号都能被补齐。
async def synchronize_inventory() -> None:
    now = datetime.now(ZoneInfo(settings.timezone))
    for service_date in (now.date(), None):
        await _synchronize_inventory_for_date(now, service_date)


# 数据库批次是事实来源；同步到 Redis 时必须保持可重试。
async def _synchronize_inventory_for_date(now: datetime, service_date: date | None) -> None:
    try:
        async with async_session_factory() as session, session.begin():
            service = InventoryService(SqlAlchemyInventoryRepository(session))
            result = await service.synchronize_target(now, service_date=service_date)
            view = await service.get_inventory(result.venue_id, result.service_date)
            confirmed_rows = await service.repository.list_batch_confirmed_usage(result.batch_id)
        redis_inventory = None
        if result.status is ReleaseBatchStatus.OPEN:
            redis_service = RedisAtomicReservationService(
                get_redis(),
                keyspace=RedisReservationKeyspace(),
                ttl_seconds=settings.booking_result_ttl_seconds,
            )
            redis_inventory = await redis_service.initialize_inventory(
                result.batch_id,
                result.service_date,
                [
                    RedisInventorySlot(
                        slot_id=slot.id,
                        period_token=f"{slot.start_at.isoformat()}|{slot.end_at.isoformat()}",
                        state=(
                            RedisSlotState.AVAILABLE
                            if slot.status is BookingSlotStatus.AVAILABLE and slot.start_at > now
                            else RedisSlotState.UNAVAILABLE
                        ),
                    )
                    for slot, _ in view.slot_rows
                ],
                _confirmed_usages(confirmed_rows),
            )
            await get_redis().hsetnx(
                RedisReservationKeyspace().periods(result.batch_id), "__batch_status", "OPEN"
            )
        logger.info(
            "inventory_synchronized",
            extra={
                "venue_id": result.venue_id,
                "service_date": result.service_date.isoformat(),
                "batch_id": result.batch_id,
                "slot_count": result.slot_count,
                "status": result.status.value,
                "generated": result.generated,
                "released": result.released,
                "redis_inventory": redis_inventory.code.value if redis_inventory else "SKIPPED",
                "redis_replayed": redis_inventory.replayed if redis_inventory else False,
            },
        )
    except InventoryConfigurationError as exc:
        logger.warning("inventory_configuration_missing", extra={"detail": str(exc)})
    except Exception:
        logger.exception("inventory_synchronization_failed")


# 根据时段结束时间归档预约，不改变已记录的签到事实。
async def complete_finished_bookings() -> None:
    try:
        async with async_session_factory() as session, session.begin():
            completed = await archive_completed_bookings(session, datetime.now(ZoneInfo("UTC")))
        if completed:
            logger.info("bookings_completed", extra={"count": completed})
    except Exception:
        logger.exception("booking_completion_failed")


def _confirmed_usages(
    rows: list[tuple[int, datetime, datetime]],
) -> list[RedisConfirmedUsage]:
    periods_by_user: dict[int, list[str]] = defaultdict(list)
    for user_id, start_at, end_at in rows:
        periods_by_user[user_id].append(f"{start_at.isoformat()}|{end_at.isoformat()}")
    return [
        RedisConfirmedUsage(user_id=user_id, period_tokens=tuple(periods))
        for user_id, periods in periods_by_user.items()
    ]


async def run() -> None:
    await write_heartbeat()
    await synchronize_inventory()
    await complete_finished_bookings()
    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    scheduler.add_job(
        write_heartbeat,
        trigger="interval",
        seconds=10,
        id="scheduler-heartbeat",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        synchronize_inventory,
        trigger="interval",
        seconds=30,
        id="inventory-synchronization",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        complete_finished_bookings,
        trigger="interval",
        seconds=60,
        id="booking-completion",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    logger.info("scheduler_started", extra={"timezone": settings.timezone})

    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopped.set)
    await stopped.wait()
    scheduler.shutdown(wait=False)
    await close_redis()
    await close_database()
    logger.info("scheduler_stopped")


if __name__ == "__main__":
    asyncio.run(run())
