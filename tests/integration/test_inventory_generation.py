"""库存生成测试：重复执行幂等，放号与 Redis 状态同步。"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from campus_reserve.inventory.infrastructure.sqlalchemy_repository import (
    SqlAlchemyInventoryRepository,
)
from campus_reserve.inventory.models import BookingSlot, ReleaseBatch
from campus_reserve.inventory.services.inventory import InventoryService
from campus_reserve.shared.config import get_settings
from campus_reserve.shared.enums import BookingSlotStatus, ReleaseBatchStatus


@pytest.mark.integration
async def test_inventory_generation_is_idempotent_and_releases_atomically() -> None:
    engine = create_async_engine(get_settings().postgres_dsn, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    timezone = ZoneInfo("Asia/Shanghai")

    async with sessions() as session:
        transaction = await session.begin()
        try:
            service = InventoryService(SqlAlchemyInventoryRepository(session))
            first = await service.synchronize_target(datetime(2040, 1, 1, 7, 0, tzinfo=timezone))
            second = await service.synchronize_target(datetime(2040, 1, 1, 7, 30, tzinfo=timezone))

            assert first.generated is True
            assert first.service_date == date(2040, 1, 2)
            assert first.status is ReleaseBatchStatus.READY
            assert first.slot_count == 210
            assert second.generated is False
            assert second.batch_id == first.batch_id
            assert second.slot_count == 210

            batch_count = await session.scalar(
                select(func.count()).select_from(ReleaseBatch).where(
                    ReleaseBatch.service_date == first.service_date
                )
            )
            slot_count = await session.scalar(
                select(func.count()).select_from(BookingSlot).where(
                    BookingSlot.release_batch_id == first.batch_id
                )
            )
            assert batch_count == 1
            assert slot_count == 210

            released = await service.synchronize_target(
                datetime(2040, 1, 1, 8, 0, tzinfo=timezone)
            )
            assert released.released is True
            assert released.status is ReleaseBatchStatus.OPEN
            available_count = await session.scalar(
                select(func.count()).select_from(BookingSlot).where(
                    BookingSlot.release_batch_id == first.batch_id,
                    BookingSlot.status == BookingSlotStatus.AVAILABLE,
                )
            )
            assert available_count == 210

            today = await service.synchronize_target(
                datetime(2040, 1, 1, 13, 0, tzinfo=timezone),
                service_date=date(2040, 1, 1),
            )
            assert today.generated is True
            assert today.service_date == date(2040, 1, 1)
            assert today.status is ReleaseBatchStatus.OPEN
            assert today.slot_count == 210
        finally:
            await transaction.rollback()

    await engine.dispose()
