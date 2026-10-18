"""管理集成测试：验证权限、批次状态、例外日、签到和审计。"""

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import campus_reserve.models  # noqa: F401
from apps.api.main import app
from campus_reserve.admin.controllers.http import get_admin_session
from campus_reserve.admin.models import AuditLog
from campus_reserve.auth.security import create_access_token
from campus_reserve.bookings.models import Booking, BookingItem, BookingStatusLog
from campus_reserve.bookings.services.redis_reservation import (
    RedisAtomicReservationService,
    RedisInventorySlot,
    RedisReservationKeyspace,
)
from campus_reserve.checkins.models import CheckIn
from campus_reserve.inventory.models import BookingSlot, ReleaseBatch
from campus_reserve.shared.config import get_settings
from campus_reserve.shared.enums import BookingSlotStatus, BookingStatus, ReleaseBatchStatus
from campus_reserve.shared.redis import close_redis, get_redis
from campus_reserve.users.models import User
from campus_reserve.venues.models import CalendarException, OpeningTemplate


@pytest.mark.integration
async def test_admin_batch_gate_rbac_check_in_and_audit() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.postgres_dsn, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    async def override_admin_session():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_admin_session] = override_admin_session
    app.dependency_overrides[get_redis] = lambda: redis
    keys = RedisReservationKeyspace()
    service_date = date(2090, 4, 3)
    batch_id = slot_id = booking_id = 0
    try:
        async with sessions() as session:
            admin = await session.scalar(select(User).where(User.username == "venue_admin"))
            platform = await session.scalar(select(User).where(User.username == "platform_admin"))
            student = await session.scalar(select(User).where(User.username == "student001"))
            assert admin and platform and student
            admin_token, _ = create_access_token(admin)
            platform_token, _ = create_access_token(platform)
            student_token, _ = create_access_token(student)
            student_id = student.id

        async with sessions() as session, session.begin():
            batch = ReleaseBatch(
                venue_id=1,
                service_date=service_date,
                release_at=datetime.now(UTC) - timedelta(minutes=1),
                status=ReleaseBatchStatus.OPEN,
                slot_count=2,
                rule_snapshot={},
                opened_at=datetime.now(UTC),
            )
            session.add(batch)
            await session.flush()
            batch_id = batch.id
            free_slot = BookingSlot(
                court_id=1, release_batch_id=batch_id, service_date=service_date,
                start_at=datetime(2090, 4, 3, 8, tzinfo=UTC),
                end_at=datetime(2090, 4, 3, 9, tzinfo=UTC),
                status=BookingSlotStatus.AVAILABLE,
            )
            booked_slot = BookingSlot(
                court_id=1, release_batch_id=batch_id, service_date=service_date,
                start_at=datetime(2090, 4, 3, 9, tzinfo=UTC),
                end_at=datetime(2090, 4, 3, 10, tzinfo=UTC),
                status=BookingSlotStatus.AVAILABLE,
            )
            session.add_all([free_slot, booked_slot])
            await session.flush()
            slot_id = free_slot.id
            booking = Booking(
                request_id=uuid4(), booking_no=f"ADMIN-{uuid4().hex[:12]}",
                user_id=student_id, venue_id=1, status=BookingStatus.CONFIRMED,
                confirmed_at=datetime.now(UTC), rule_snapshot={},
            )
            session.add(booking)
            await session.flush()
            booking_id = booking.id
            booked_slot.status = BookingSlotStatus.BOOKED
            booked_slot.current_booking_id = booking_id
            session.add(BookingItem(
                booking_id=booking_id, slot_id=booked_slot.id, user_id=student_id,
                service_date=service_date, start_at=booked_slot.start_at,
                end_at=booked_slot.end_at,
            ))

        await RedisAtomicReservationService(redis).initialize_inventory(
            batch_id, service_date,
            [
                RedisInventorySlot(slot_id, "2090-04-03T08:00:00+00:00|2090-04-03T09:00:00+00:00"),
                RedisInventorySlot(
                    booked_slot.id, "2090-04-03T09:00:00+00:00|2090-04-03T10:00:00+00:00"
                ),
            ],
        )
        headers = {"Authorization": f"Bearer {admin_token}"}
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            student_response = await client.get(
                "/api/v1/admin/overview",
                headers={"Authorization": f"Bearer {student_token}"},
            )
            assert student_response.status_code == 403
            platform_only = await client.post(
                "/api/v1/admin/opening-templates", headers=headers,
                json={"name": "unauthorized", "valid_from": "2090-04-01"},
            )
            assert platform_only.status_code == 403
            assert (await client.get(
                "/api/v1/admin/overview",
                headers={"Authorization": f"Bearer {platform_token}"},
            )).status_code == 200

            paused = await client.post(
                f"/api/v1/admin/release-batches/{batch_id}/pause", headers=headers,
            )
            assert paused.status_code == 200, paused.text
            assert paused.json()["status"] == "PAUSED"
            rejected = await client.post(
                "/api/v1/booking-requests",
                headers={"Authorization": f"Bearer {student_token}"},
                json={
                    "request_id": str(uuid4()), "release_batch_id": batch_id,
                    "service_date": service_date.isoformat(), "slot_ids": [slot_id],
                },
            )
            assert rejected.status_code == 409, rejected.text
            assert rejected.json()["error_code"] == "BATCH_NOT_OPEN"

            resumed = await client.post(
                f"/api/v1/admin/release-batches/{batch_id}/resume", headers=headers,
            )
            assert resumed.status_code == 200, resumed.text
            assert resumed.json()["status"] == "OPEN"
            assert await redis.hget(keys.periods(batch_id), "__batch_status") == "OPEN"

            searched = await client.get(
                "/api/v1/admin/bookings", headers=headers,
                params={"booking_no": "ADMIN-", "service_date": service_date.isoformat()},
            )
            assert searched.status_code == 200, searched.text
            assert any(row["id"] == booking_id for row in searched.json())
            checked = await client.post(
                f"/api/v1/admin/bookings/{booking_id}/check-in", headers=headers,
                json={"note": "现场核验"},
            )
            assert checked.status_code == 200, checked.text
            assert checked.json()["booking_status"] == "CHECKED_IN"
            replay = await client.post(
                f"/api/v1/admin/bookings/{booking_id}/check-in", headers=headers,
                json={},
            )
            assert replay.status_code == 200, replay.text
            assert replay.json()["replayed"] is True

            closed = await client.post(
                f"/api/v1/admin/release-batches/{batch_id}/close", headers=headers,
            )
            assert closed.status_code == 200, closed.text
            assert closed.json()["status"] == "CLOSED"
            assert await redis.hget(keys.periods(batch_id), "__batch_status") == "CLOSED"
            logs = await client.get("/api/v1/admin/audit-logs", headers=headers)
            assert logs.status_code == 200, logs.text
            assert any(row["action"] == "BOOKING_CHECKED_IN" for row in logs.json())

        async with sessions() as session:
            free = await session.get(BookingSlot, slot_id)
            assert free and free.status is BookingSlotStatus.BLOCKED
    finally:
        app.dependency_overrides.pop(get_admin_session, None)
        app.dependency_overrides.pop(get_redis, None)
        if batch_id:
            await redis.delete(
                keys.inventory_marker(batch_id), keys.periods(batch_id), keys.slots(batch_id),
            )
        async with sessions() as cleanup, cleanup.begin():
            if booking_id:
                await cleanup.execute(delete(CheckIn).where(CheckIn.booking_id == booking_id))
                await cleanup.execute(
                    delete(BookingStatusLog).where(BookingStatusLog.booking_id == booking_id)
                )
                await cleanup.execute(
                    delete(BookingItem).where(BookingItem.booking_id == booking_id)
                )
            if batch_id:
                await cleanup.execute(
                    delete(AuditLog).where(
                        AuditLog.resource_type == "release_batch",
                        AuditLog.resource_id == str(batch_id),
                    )
                )
                if booking_id:
                    await cleanup.execute(
                        delete(AuditLog).where(
                            AuditLog.resource_type == "booking",
                            AuditLog.resource_id == str(booking_id),
                        )
                    )
                await cleanup.execute(
                    delete(BookingSlot).where(BookingSlot.release_batch_id == batch_id)
                )
            if booking_id:
                await cleanup.execute(delete(Booking).where(Booking.id == booking_id))
            if batch_id:
                await cleanup.execute(delete(ReleaseBatch).where(ReleaseBatch.id == batch_id))
        await redis.aclose()
        await engine.dispose()
        await close_redis()


@pytest.mark.integration
async def test_admin_template_exception_and_generation_lifecycle() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.postgres_dsn, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)

    async def override_admin_session():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_admin_session] = override_admin_session
    app.dependency_overrides[get_redis] = lambda: redis
    service_date = datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=24)
    exception_date = service_date + timedelta(days=1)
    batch_id = template_id = exception_id = 0
    try:
        async with sessions() as session:
            admin = await session.scalar(select(User).where(User.username == "venue_admin"))
            platform = await session.scalar(select(User).where(User.username == "platform_admin"))
            assert admin and platform
            admin_token, _ = create_access_token(admin)
            platform_token, _ = create_access_token(platform)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            platform_headers = {"Authorization": f"Bearer {platform_token}"}
            admin_headers = {"Authorization": f"Bearer {admin_token}"}
            created_template = await client.post(
                "/api/v1/admin/opening-templates", headers=platform_headers,
                json={
                    "name": f"integration-{uuid4().hex[:12]}",
                    "valid_from": service_date.isoformat(),
                    "status": "DRAFT",
                },
            )
            assert created_template.status_code == 200, created_template.text
            template_id = created_template.json()["id"]
            patched_template = await client.patch(
                f"/api/v1/admin/opening-templates/{template_id}",
                headers=platform_headers, json={"weekday_mask": 31},
            )
            assert patched_template.status_code == 200, patched_template.text
            assert patched_template.json()["weekday_mask"] == 31

            exception = await client.put(
                "/api/v1/admin/calendar-exceptions", headers=admin_headers,
                json={
                    "exception_date": exception_date.isoformat(),
                    "court_id": 1, "type": "CLOSED", "reason": "集成测试",
                },
            )
            assert exception.status_code == 200, exception.text
            exception_id = exception.json()["id"]

            preview = await client.post(
                "/api/v1/admin/slot-generation-jobs", headers=admin_headers,
                json={"service_date": service_date.isoformat(), "preview_only": True},
            )
            assert preview.status_code == 200, preview.text
            assert preview.json()["slot_count"] == 210
            assert preview.json()["existing_batch_id"] is None
            generated = await client.post(
                "/api/v1/admin/slot-generation-jobs", headers=admin_headers,
                json={"service_date": service_date.isoformat(), "preview_only": False},
            )
            assert generated.status_code == 200, generated.text
            batch_id = generated.json()["existing_batch_id"]
            assert generated.json()["batch"]["status"] == "DRAFT"
            replay = await client.post(
                "/api/v1/admin/slot-generation-jobs", headers=admin_headers,
                json={"service_date": service_date.isoformat(), "preview_only": False},
            )
            assert replay.status_code == 200, replay.text
            assert replay.json()["existing_batch_id"] == batch_id
            published = await client.post(
                f"/api/v1/admin/release-batches/{batch_id}/publish",
                headers=admin_headers,
            )
            assert published.status_code == 200, published.text
            assert published.json()["status"] == "READY"
            premature = await client.post(
                f"/api/v1/admin/release-batches/{batch_id}/open",
                headers=admin_headers,
            )
            assert premature.status_code == 409, premature.text
            assert premature.json()["detail"]["code"] == "RELEASE_NOT_DUE"
            closed = await client.post(
                f"/api/v1/admin/release-batches/{batch_id}/close",
                headers=admin_headers,
            )
            assert closed.status_code == 200, closed.text
            assert closed.json()["status"] == "CLOSED"

        async with sessions() as session:
            blocked = list((await session.scalars(
                select(BookingSlot).where(BookingSlot.release_batch_id == batch_id)
            )).all())
            assert len(blocked) == 210
            assert all(slot.status is BookingSlotStatus.BLOCKED for slot in blocked)
    finally:
        app.dependency_overrides.pop(get_admin_session, None)
        app.dependency_overrides.pop(get_redis, None)
        async with sessions() as cleanup, cleanup.begin():
            if batch_id:
                await cleanup.execute(delete(AuditLog).where(
                    AuditLog.resource_type == "release_batch",
                    AuditLog.resource_id == str(batch_id),
                ))
                await cleanup.execute(delete(BookingSlot).where(
                    BookingSlot.release_batch_id == batch_id
                ))
                await cleanup.execute(delete(ReleaseBatch).where(ReleaseBatch.id == batch_id))
            if exception_id:
                await cleanup.execute(delete(AuditLog).where(
                    AuditLog.resource_type == "calendar_exception",
                    AuditLog.resource_id == str(exception_id),
                ))
                await cleanup.execute(delete(CalendarException).where(
                    CalendarException.id == exception_id
                ))
            if template_id:
                await cleanup.execute(delete(AuditLog).where(
                    AuditLog.resource_type == "opening_template",
                    AuditLog.resource_id == str(template_id),
                ))
                await cleanup.execute(delete(OpeningTemplate).where(
                    OpeningTemplate.id == template_id
                ))
        await engine.dispose()
        await redis.aclose()
