import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import ResponseError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from campus_reserve.bookings.infrastructure.sqlalchemy_repository import (
    SqlAlchemyBookingRepository,
)
from campus_reserve.bookings.services.redis_reservation import (
    RedisAtomicReservationService,
    RedisReservationCommand,
    RedisReservationKeyspace,
    reservation_fingerprint,
)
from campus_reserve.bookings.services.sync_booking import BookingRuleError, SyncBookingService


class BookingEventPayloadError(ValueError):
    pass


class BookingPersistenceStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    DEAD_LETTERED = "DEAD_LETTERED"


@dataclass(frozen=True, slots=True)
class BookingReservationEvent:
    command: RedisReservationCommand
    fingerprint: str

    @classmethod
    def from_payload(cls, payload: dict[str, str]) -> "BookingReservationEvent":
        try:
            slot_ids = tuple(int(value) for value in payload["slot_ids"].split(","))
            command = RedisReservationCommand(
                request_id=UUID(payload["request_id"]),
                user_id=int(payload["user_id"]),
                batch_id=int(payload["batch_id"]),
                service_date=date.fromisoformat(payload["service_date"]),
                slot_ids=slot_ids,
            )
            fingerprint = payload["fingerprint"]
        except (KeyError, TypeError, ValueError) as exc:
            raise BookingEventPayloadError("预约事件字段缺失或格式错误") from exc
        if command.user_id < 1 or command.batch_id < 1 or not slot_ids:
            raise BookingEventPayloadError("预约事件标识或时段列表无效")
        if reservation_fingerprint(command) != fingerprint:
            raise BookingEventPayloadError("预约事件指纹不匹配")
        return cls(command=command, fingerprint=fingerprint)


@dataclass(frozen=True, slots=True)
class BookingPersistenceResult:
    request_id: UUID
    status: BookingPersistenceStatus
    replayed: bool
    command: RedisReservationCommand | None = None
    booking_id: int | None = None
    booking_no: str | None = None
    error_code: str | None = None


class BookingEventProcessor:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def process(self, payload: dict[str, str]) -> BookingPersistenceResult:
        event = BookingReservationEvent.from_payload(payload)
        command = event.command
        async with self.sessions() as session, session.begin():
            service = SyncBookingService(SqlAlchemyBookingRepository(session))
            try:
                submission = await service.submit(
                    command.user_id,
                    command.request_id,
                    list(command.slot_ids),
                )
            except BookingRuleError as exc:
                return BookingPersistenceResult(
                    request_id=command.request_id,
                    status=BookingPersistenceStatus.FAILED,
                    replayed=exc.replayed,
                    command=command,
                    error_code=exc.code,
                )

            request = submission.request
            booking = request.booking
            if request.release_batch_id != command.batch_id:
                raise BookingEventPayloadError("预约事件批次与数据库库存不一致")
            if booking is None or any(
                item.service_date != command.service_date for item in booking.items
            ):
                raise BookingEventPayloadError("预约事件日期与数据库库存不一致")
            return BookingPersistenceResult(
                request_id=command.request_id,
                status=BookingPersistenceStatus.CONFIRMED,
                replayed=submission.replayed,
                command=command,
                booking_id=booking.id,
                booking_no=booking.booking_no,
            )


class BookingEventHandler(Protocol):
    async def process(self, payload: dict[str, str]) -> BookingPersistenceResult: ...


class BookingStreamConsumer:
    def __init__(
        self,
        redis: Redis,
        processor: BookingEventHandler,
        *,
        stream: str,
        dead_letter_stream: str,
        group: str,
        consumer_name: str,
        keyspace: RedisReservationKeyspace | None = None,
        result_ttl_seconds: int = 7 * 24 * 60 * 60,
    ) -> None:
        self.redis = redis
        self.processor = processor
        self.stream = stream
        self.dead_letter_stream = dead_letter_stream
        self.group = group
        self.consumer_name = consumer_name
        self.keyspace = keyspace or RedisReservationKeyspace()
        self.result_ttl_seconds = result_ttl_seconds
        self.reservations = RedisAtomicReservationService(
            redis,
            keyspace=self.keyspace,
            ttl_seconds=result_ttl_seconds,
        )

    async def ensure_group(self) -> None:
        try:
            await self.redis.xgroup_create(
                name=self.stream,
                groupname=self.group,
                id="0",
                mkstream=True,
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def read_new(self, *, count: int = 10, block_ms: int = 5_000):
        messages = await self.redis.xreadgroup(
            groupname=self.group,
            consumername=self.consumer_name,
            streams={self.stream: ">"},
            count=count,
            block=block_ms,
        )
        return [entry for _, entries in messages for entry in entries]

    async def claim_stale(self, *, min_idle_ms: int, count: int = 10):
        response = await self.redis.xautoclaim(
            name=self.stream,
            groupname=self.group,
            consumername=self.consumer_name,
            min_idle_time=min_idle_ms,
            start_id="0-0",
            count=count,
        )
        return response[1]

    async def process_message(
        self,
        message_id: str,
        payload: dict[str, str],
    ) -> BookingPersistenceResult:
        try:
            result = await self.processor.process(payload)
        except BookingEventPayloadError as exc:
            await self._dead_letter(message_id, payload, str(exc))
            return BookingPersistenceResult(
                request_id=_safe_request_id(payload),
                status=BookingPersistenceStatus.DEAD_LETTERED,
                replayed=False,
                error_code="INVALID_EVENT_PAYLOAD",
            )

        if result.status is BookingPersistenceStatus.FAILED and result.command is not None:
            await self.reservations.compensate(
                result.command,
                result.error_code or "DATABASE_PERSISTENCE_FAILED",
            )

        request_key = self.keyspace.request(result.request_id)
        mapping = {
            "persistence_status": result.status.value,
            "persistence_replayed": "1" if result.replayed else "0",
            "persistence_error_code": result.error_code or "",
            "booking_id": str(result.booking_id or ""),
            "booking_no": result.booking_no or "",
            "persisted_at": datetime.now(UTC).isoformat(),
        }
        async with self.redis.pipeline(transaction=True) as pipeline:
            pipeline.hset(request_key, mapping=mapping)
            pipeline.expire(request_key, self.result_ttl_seconds)
            pipeline.xack(self.stream, self.group, message_id)
            await pipeline.execute()
        return result

    async def _dead_letter(
        self,
        message_id: str,
        payload: dict[str, str],
        error: str,
    ) -> None:
        async with self.redis.pipeline(transaction=True) as pipeline:
            pipeline.xadd(
                self.dead_letter_stream,
                {
                    "source_message_id": message_id,
                    "error": error,
                    "payload": json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    "failed_at": datetime.now(UTC).isoformat(),
                },
            )
            pipeline.xack(self.stream, self.group, message_id)
            await pipeline.execute()


def _safe_request_id(payload: dict[str, str]) -> UUID:
    try:
        return UUID(payload.get("request_id", ""))
    except (TypeError, ValueError):
        return UUID(int=0)
