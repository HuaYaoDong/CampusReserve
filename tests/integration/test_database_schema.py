"""数据库约束测试：种子数据、每日额度和同场地防超卖。"""

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from campus_reserve.shared.config import get_settings


@pytest.fixture
async def database_engine() -> AsyncEngine:
    test_engine = create_async_engine(get_settings().postgres_dsn, poolclass=NullPool)
    yield test_engine
    await test_engine.dispose()


@pytest.mark.integration
async def test_fixed_mvp_resource_seed(database_engine: AsyncEngine) -> None:
    async with database_engine.connect() as connection:
        venue_count = await connection.scalar(text("SELECT count(*) FROM venue"))
        court_count = await connection.scalar(text("SELECT count(*) FROM court"))
        template = (
            await connection.execute(
                text(
                    "SELECT open_time, close_time, slot_minutes, advance_days, release_time "
                    "FROM opening_template WHERE status = 'ACTIVE'"
                )
            )
        ).one()

    assert venue_count == 1
    assert court_count == 15
    assert str(template.open_time) == "08:00:00"
    assert str(template.close_time) == "22:00:00"
    assert template.slot_minutes == 60
    assert template.advance_days == 1
    assert str(template.release_time) == "08:00:00"


@pytest.mark.integration
async def test_critical_database_constraints_exist(database_engine: AsyncEngine) -> None:
    async with database_engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT conname, contype::text FROM pg_constraint "
                "WHERE conname = ANY(CAST(:names AS text[]))"
            ),
            {
                "names": [
                    "uq_booking_item_slot",
                    "ck_user_daily_usage_slot_count_between_0_and_2",
                    "ck_booking_slot_booked_requires_booking",
                ]
            },
        )
        constraints = dict(rows.all())

    assert constraints == {
        "ck_booking_slot_booked_requires_booking": "c",
        "ck_user_daily_usage_slot_count_between_0_and_2": "c",
        "uq_booking_item_slot": "u",
    }


@pytest.mark.integration
async def test_database_rejects_more_than_two_daily_slots(database_engine: AsyncEngine) -> None:
    username_suffix = uuid4().hex

    async with database_engine.connect() as connection:
        transaction = await connection.begin()
        user_id = await connection.scalar(
            text(
                "INSERT INTO app_user "
                "(username, password_hash, student_no, display_name, role, status) "
                "VALUES (:username, 'test-only', :student_no, '约束测试学生', 'STUDENT', 'ACTIVE') "
                "RETURNING id"
            ),
            {"username": f"constraint-{username_suffix}", "student_no": username_suffix[:32]},
        )

        with pytest.raises(IntegrityError):
            await connection.execute(
                text(
                    "INSERT INTO user_daily_usage (user_id, service_date, used_slot_count) "
                    "VALUES (:user_id, CURRENT_DATE + 2, 3)"
                ),
                {"user_id": user_id},
            )

        await transaction.rollback()


@pytest.mark.integration
async def test_database_rejects_slot_oversell_and_allows_parallel_courts(
    database_engine: AsyncEngine,
) -> None:
    suffix = uuid4().hex
    request_ids = [uuid4(), uuid4(), uuid4()]

    async with database_engine.connect() as connection:
        transaction = await connection.begin()
        users = (
            await connection.execute(
                text(
                    "INSERT INTO app_user "
                    "(username, password_hash, student_no, display_name, role, status) VALUES "
                    "(:username1, 'test-only', :student1, '学生一', 'STUDENT', 'ACTIVE'), "
                    "(:username2, 'test-only', :student2, '学生二', 'STUDENT', 'ACTIVE') "
                    "RETURNING id"
                ),
                {
                    "username1": f"oversell-a-{suffix}",
                    "username2": f"oversell-b-{suffix}",
                    "student1": f"A{suffix[:31]}",
                    "student2": f"B{suffix[:31]}",
                },
            )
        ).scalars().all()
        batch_id = await connection.scalar(
            text(
                "INSERT INTO release_batch "
                "(venue_id, service_date, release_at, status, slot_count) "
                "VALUES (1, CURRENT_DATE + 3650, now(), 'OPEN', 2) RETURNING id"
            )
        )
        slots = (
            await connection.execute(
                text(
                    "INSERT INTO booking_slot "
                    "(court_id, release_batch_id, service_date, start_at, end_at, status) "
                    "SELECT court_id, :batch_id, CURRENT_DATE + 3650, start_at, "
                    "start_at + INTERVAL '60 minutes', 'AVAILABLE' "
                    "FROM (VALUES "
                    "(1, ((CURRENT_DATE + 3650)::timestamp + TIME '08:00') "
                    "AT TIME ZONE 'Asia/Shanghai'), "
                    "(2, ((CURRENT_DATE + 3650)::timestamp + TIME '08:00') "
                    "AT TIME ZONE 'Asia/Shanghai')) AS source(court_id, start_at) "
                    "RETURNING id, start_at, end_at"
                ),
                {"batch_id": batch_id},
            )
        ).all()
        booking_ids = (
            await connection.execute(
                text(
                    "INSERT INTO booking "
                    "(request_id, booking_no, user_id, venue_id, status, confirmed_at) VALUES "
                    "(:request1, :number1, :user1, 1, 'CONFIRMED', now()), "
                    "(:request2, :number2, :user2, 1, 'CONFIRMED', now()), "
                    "(:request3, :number3, :user1, 1, 'CONFIRMED', now()) "
                    "RETURNING id"
                ),
                {
                    "request1": request_ids[0],
                    "request2": request_ids[1],
                    "request3": request_ids[2],
                    "number1": f"TEST-{suffix[:20]}-1",
                    "number2": f"TEST-{suffix[:20]}-2",
                    "number3": f"TEST-{suffix[:20]}-3",
                    "user1": users[0],
                    "user2": users[1],
                },
            )
        ).scalars().all()
        await connection.execute(
            text(
                "INSERT INTO booking_item "
                "(booking_id, slot_id, user_id, service_date, start_at, end_at) "
                "VALUES (:booking_id, :slot_id, :user_id, CURRENT_DATE + 3650, "
                ":start_at, :end_at)"
            ),
            {
                "booking_id": booking_ids[0],
                "slot_id": slots[0].id,
                "user_id": users[0],
                "start_at": slots[0].start_at,
                "end_at": slots[0].end_at,
            },
        )

        oversell_savepoint = await connection.begin_nested()
        with pytest.raises(IntegrityError):
            await connection.execute(
                text(
                    "INSERT INTO booking_item "
                    "(booking_id, slot_id, user_id, service_date, start_at, end_at) "
                    "VALUES (:booking_id, :slot_id, :user_id, CURRENT_DATE + 3650, "
                    ":start_at, :end_at)"
                ),
                {
                    "booking_id": booking_ids[1],
                    "slot_id": slots[0].id,
                    "user_id": users[1],
                    "start_at": slots[0].start_at,
                    "end_at": slots[0].end_at,
                },
            )
        await oversell_savepoint.rollback()

        await connection.execute(
                text(
                    "INSERT INTO booking_item "
                    "(booking_id, slot_id, user_id, service_date, start_at, end_at) "
                    "VALUES (:booking_id, :slot_id, :user_id, CURRENT_DATE + 3650, "
                    ":start_at, :end_at)"
                ),
                {
                    "booking_id": booking_ids[2],
                    "slot_id": slots[1].id,
                    "user_id": users[0],
                    "start_at": slots[1].start_at,
                    "end_at": slots[1].end_at,
                },
            )
        await transaction.rollback()
