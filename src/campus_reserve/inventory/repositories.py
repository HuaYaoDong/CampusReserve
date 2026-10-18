"""库存仓储协议和查询行结构。"""

from datetime import date, datetime
from typing import Protocol

from campus_reserve.inventory.models import BookingSlot, ReleaseBatch
from campus_reserve.venues.models import CalendarException, Court, OpeningTemplate, Venue

InventoryConfiguration = tuple[
    Venue,
    OpeningTemplate,
    list[Court],
    list[CalendarException],
]
InventorySlotRow = tuple[BookingSlot, Court]
ConfirmedUsageRow = tuple[int, datetime, datetime]
VenueConfiguration = tuple[Venue, OpeningTemplate, list[Court]]


class InventoryRepository(Protocol):
    async def get_active_configuration(
        self, service_date: date
    ) -> InventoryConfiguration | None: ...

    async def acquire_generation_lock(self, venue_id: int, service_date: date) -> None: ...

    async def get_batch(self, venue_id: int, service_date: date) -> ReleaseBatch | None: ...

    async def add_batch(self, batch: ReleaseBatch) -> None: ...

    async def add_slots(self, slots: list[BookingSlot]) -> None: ...

    async def release_batch(self, batch: ReleaseBatch, opened_at: datetime) -> None: ...

    async def list_venue_configurations(self) -> list[VenueConfiguration]: ...

    async def list_batch_slots(self, batch_id: int) -> list[InventorySlotRow]: ...

    async def list_batch_confirmed_usage(
        self, batch_id: int
    ) -> list[ConfirmedUsageRow]: ...
