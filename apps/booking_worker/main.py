import asyncio
import os
import socket

import campus_reserve.models  # noqa: F401
from campus_reserve.bookings.services.redis_reservation import RedisReservationKeyspace
from campus_reserve.bookings.services.stream_booking import (
    BookingEventProcessor,
    BookingStreamConsumer,
)
from campus_reserve.shared.config import get_settings
from campus_reserve.shared.database import async_session_factory, close_database
from campus_reserve.shared.logging import configure_logging, get_logger
from campus_reserve.shared.redis import close_redis, get_redis

settings = get_settings()
configure_logging(settings.log_level)
logger = get_logger(__name__)

async def run() -> None:
    consumer_name = os.getenv("WORKER_CONSUMER_NAME", socket.gethostname())
    redis = get_redis()
    consumer = BookingStreamConsumer(
        redis,
        BookingEventProcessor(async_session_factory),
        stream=settings.booking_stream,
        dead_letter_stream=settings.booking_dead_letter_stream,
        group=settings.booking_consumer_group,
        consumer_name=consumer_name,
        keyspace=RedisReservationKeyspace(),
        result_ttl_seconds=settings.booking_result_ttl_seconds,
    )
    await consumer.ensure_group()
    logger.info(
        "worker_started",
        extra={"consumer_name": consumer_name, "stream": settings.booking_stream},
    )

    try:
        while True:
            await redis.setex(f"campus-reserve:worker:{consumer_name}:heartbeat", 30, "ok")
            entries = await consumer.claim_stale(
                min_idle_ms=settings.booking_claim_idle_ms,
                count=10,
            )
            if not entries:
                entries = await consumer.read_new(count=10, block_ms=5_000)
            for message_id, payload in entries:
                try:
                    result = await consumer.process_message(message_id, payload)
                    logger.info(
                        "worker_message_processed",
                        extra={
                            "message_id": message_id,
                            "request_id": str(result.request_id),
                            "persistence_status": result.status.value,
                            "replayed": result.replayed,
                        },
                    )
                except Exception:
                    # Leave transient failures pending so another worker can claim them.
                    logger.exception(
                        "worker_message_failed",
                        extra={"message_id": message_id},
                    )
    finally:
        await close_redis()
        await close_database()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("worker_stopped")
