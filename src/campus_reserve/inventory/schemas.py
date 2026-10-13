from datetime import date, datetime, time
from typing import Literal

from pydantic import BaseModel

from campus_reserve.shared.enums import (
    BookingSlotStatus,
    CourtStatus,
    ReleaseBatchStatus,
    VenueStatus,
)


class CourtSummary(BaseModel):
    id: int
    code: str
    name: str
    status: CourtStatus


class VenueSummary(BaseModel):
    id: int
    name: str
    address: str
    timezone: str
    status: VenueStatus
    notice: str | None
    open_time: time
    close_time: time
    slot_minutes: int
    advance_days: int
    release_time: time
    target_service_date: date
    courts: list[CourtSummary]


class InventorySlotResponse(BaseModel):
    id: int
    start_at: datetime
    end_at: datetime
    status: BookingSlotStatus | Literal["PROCESSING"]


class CourtInventoryResponse(CourtSummary):
    slots: list[InventorySlotResponse]


class InventoryResponse(BaseModel):
    venue_id: int
    venue_name: str
    release_batch_id: int
    service_date: date
    release_at: datetime
    batch_status: ReleaseBatchStatus
    is_released: bool
    slot_count: int
    available_count: int
    courts: list[CourtInventoryResponse]
