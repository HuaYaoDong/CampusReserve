"""ORM 模型导入及基础元数据单元测试。"""

import campus_reserve.models  # noqa: F401
from campus_reserve.shared.database import Base


def test_core_tables_are_registered() -> None:
    assert set(Base.metadata.tables) == {
        "app_user",
        "audit_log",
        "booking",
        "booking_item",
        "booking_request",
        "booking_slot",
        "booking_status_log",
        "calendar_exception",
        "check_in",
        "court",
        "opening_template",
        "release_batch",
        "user_daily_usage",
        "venue",
    }


def test_booking_item_has_database_safety_constraints() -> None:
    table = Base.metadata.tables["booking_item"]
    constraint_names = {constraint.name for constraint in table.constraints}

    assert "uq_booking_item_slot" in constraint_names
    assert "booking_item_user_time_excl" not in constraint_names


def test_daily_usage_has_two_slot_limit() -> None:
    table = Base.metadata.tables["user_daily_usage"]
    check_sql = " ".join(
        str(constraint.sqltext)
        for constraint in table.constraints
        if hasattr(constraint, "sqltext")
    )

    assert "used_slot_count BETWEEN 0 AND 2" in check_sql
