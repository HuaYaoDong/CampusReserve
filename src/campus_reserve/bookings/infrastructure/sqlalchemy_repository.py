"""预约 SQLAlchemy 仓储：加锁读取、持久化预约并查询结果。"""

from datetime import date
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from campus_reserve.bookings.models import (
    Booking,
    BookingItem,
    BookingRequest,
    BookingStatusLog,
)
from campus_reserve.inventory.models import BookingSlot
from campus_reserve.users.models import UserDailyUsage


class SqlAlchemyBookingRepository:
    def __init__(self, session: AsyncSession) -> None:
        """保存当前事务的异步数据库会话，供所有仓储方法共用。"""
        self.session = session

    async def acquire_request_lock(self, request_id: UUID) -> None:
        """按请求 UUID 获取事务级锁，防止相同请求同时落库。"""
        await self._advisory_lock(f"booking-request:{request_id}")

    async def acquire_user_date_lock(self, user_id: int, service_date: date) -> None:
        """按学生和使用日期加锁，串行检查当天预约额度。"""
        await self._advisory_lock(f"booking-usage:{user_id}:{service_date.isoformat()}")

    async def _advisory_lock(self, lock_name: str) -> None:
        """通过 PostgreSQL 事务级 advisory lock 获取指定业务键的锁。"""
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_name, 0))"),
            {"lock_name": lock_name},
        )

    async def get_request(self, request_id: UUID) -> BookingRequest | None:
        """查询请求及其预约、场地明细，用于幂等判断和结果查询。"""
        return await self.session.scalar(
            select(BookingRequest)
            .options(
                selectinload(BookingRequest.booking)
                .selectinload(Booking.items)
                .joinedload(BookingItem.slot)
                .joinedload(BookingSlot.court)
            )
            .where(BookingRequest.request_id == request_id)
            .execution_options(populate_existing=True)
        )

    async def get_slots(self, slot_ids: list[int], *, for_update: bool) -> list[BookingSlot]:
        """按时段 ID 读取库存；for_update 为真时锁住库存行。"""
        statement = (
            select(BookingSlot)
            .options(
                joinedload(BookingSlot.release_batch),
                joinedload(BookingSlot.court),
            )
            .where(BookingSlot.id.in_(slot_ids))
            .order_by(BookingSlot.id)
        )
        if for_update:
            statement = statement.with_for_update(of=BookingSlot).execution_options(
                populate_existing=True
            )
        return list((await self.session.scalars(statement)).all())

    async def get_daily_usage(
        self, user_id: int, service_date: date
    ) -> UserDailyUsage | None:
        """锁定并读取学生在指定使用日期的已用额度。"""
        return await self.session.scalar(
            select(UserDailyUsage)
            .where(
                UserDailyUsage.user_id == user_id,
                UserDailyUsage.service_date == service_date,
            )
            .with_for_update()
        )

    async def list_user_items(self, user_id: int, service_date: date) -> list[BookingItem]:
        """列出学生在指定使用日期已占用的预约明细。"""
        return list(
            (
                await self.session.scalars(
                    select(BookingItem).where(
                        BookingItem.user_id == user_id,
                        BookingItem.service_date == service_date,
                    )
                )
            ).all()
        )

    async def add_request(self, request: BookingRequest) -> None:
        """加入预约请求并立即 flush，使后续查询可见其数据库状态。"""
        self.session.add(request)
        await self.session.flush()

    async def add_booking(self, booking: Booking) -> None:
        """加入预约主记录并 flush，以取得其数据库主键。"""
        self.session.add(booking)
        await self.session.flush()

    async def add_items(self, items: list[BookingItem]) -> None:
        """把一笔预约所选的场地时段明细加入当前事务。"""
        self.session.add_all(items)

    async def add_daily_usage(self, usage: UserDailyUsage) -> None:
        """把学生当天额度记录加入当前事务。"""
        self.session.add(usage)

    async def add_status_log(self, log: BookingStatusLog) -> None:
        """把预约状态变更记录加入当前事务。"""
        self.session.add(log)

    async def flush(self) -> None:
        """将待写入的 ORM 变更发送到数据库，但不提交事务。"""
        await self.session.flush()

    async def list_user_bookings(self, user_id: int) -> list[Booking]:
        """按创建时间倒序查询学生预约，并预加载场地明细。"""
        return list(
            (
                await self.session.scalars(
                    select(Booking)
                    .options(
                        selectinload(Booking.items)
                        .joinedload(BookingItem.slot)
                        .joinedload(BookingSlot.court)
                    )
                    .where(Booking.user_id == user_id)
                    .order_by(Booking.created_at.desc())
                )
            ).all()
        )

    async def get_user_booking(self, user_id: int, booking_id: int) -> Booking | None:
        """按学生和预约 ID 查询详情，避免读取其他学生的预约。"""
        return await self.session.scalar(
            select(Booking)
            .options(
                selectinload(Booking.items)
                .joinedload(BookingItem.slot)
                .joinedload(BookingSlot.court)
            )
            .where(Booking.id == booking_id, Booking.user_id == user_id)
        )
