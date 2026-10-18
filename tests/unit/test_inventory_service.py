"""时段窗口计算的单元测试。"""

from datetime import date, time

from campus_reserve.inventory.services.inventory import build_slot_windows


def test_build_slot_windows_creates_fourteen_hourly_periods() -> None:
    windows = build_slot_windows(
        date(2026, 8, 31),
        time(8, 0),
        time(22, 0),
        60,
        "Asia/Shanghai",
    )

    assert len(windows) == 14
    assert windows[0][0].isoformat() == "2026-08-31T08:00:00+08:00"
    assert windows[0][1].isoformat() == "2026-08-31T09:00:00+08:00"
    assert windows[-1][0].isoformat() == "2026-08-31T21:00:00+08:00"
    assert windows[-1][1].isoformat() == "2026-08-31T22:00:00+08:00"
