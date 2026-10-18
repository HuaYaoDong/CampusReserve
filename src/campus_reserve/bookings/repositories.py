"""预约仓储协议，定义同步落库服务需要的数据操作。"""

from datetime import date
from typing import Protocol
from uuid import UUID

from campus_reserve.bookings.models import (
    Booking,
    BookingItem,
    BookingRequest,
    BookingStatusLog,
)
from campus_reserve.inventory.models import BookingSlot
from campus_reserve.users.models import UserDailyUsage


class BookingRepository(Protocol):
    # 按请求编号加锁，保证重复请求按顺序处理。
    async def acquire_request_lock(self, request_id: UUID) -> None: ...

    # 按学生和日期加锁，保护当天预约额度。
    async def acquire_user_date_lock(self, user_id: int, service_date: date) -> None: ...

    # 查询请求及其关联预约，返回不存在时的空值。
    async def get_request(self, request_id: UUID) -> BookingRequest | None: ...

    # 读取指定库存时段；可要求锁定行。
    async def get_slots(self, slot_ids: list[int], *, for_update: bool) -> list[BookingSlot]: ...

    # 读取学生当天的额度记录。
    async def get_daily_usage(
        self, user_id: int, service_date: date
    ) -> UserDailyUsage | None: ...

    # 列出学生指定日期已预约的场地明细。
    async def list_user_items(self, user_id: int, service_date: date) -> list[BookingItem]: ...

    # 将预约请求写入当前事务。
    async def add_request(self, request: BookingRequest) -> None: ...

    # 将预约主记录写入当前事务。
    async def add_booking(self, booking: Booking) -> None: ...

    # 将预约的多个场地明细写入当前事务。
    async def add_items(self, items: list[BookingItem]) -> None: ...

    # 将学生当天额度记录写入当前事务。
    async def add_daily_usage(self, usage: UserDailyUsage) -> None: ...

    # 将预约状态变更写入当前事务。
    async def add_status_log(self, log: BookingStatusLog) -> None: ...

    # 把待写入变更发送到数据库但不提交事务。
    async def flush(self) -> None: ...

    # 列出学生的所有预约。
    async def list_user_bookings(self, user_id: int) -> list[Booking]: ...

    # 按学生与预约主键读取本人预约。
    async def get_user_booking(self, user_id: int, booking_id: int) -> Booking | None: ...
