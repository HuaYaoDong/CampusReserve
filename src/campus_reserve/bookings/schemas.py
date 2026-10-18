"""预约提交与结果查询的 API 输入输出模型。"""

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, Field

from campus_reserve.shared.enums import BookingRequestStatus, BookingStatus


class CreateBookingRequest(BaseModel):
    request_id: UUID
    release_batch_id: int = Field(gt=0)
    service_date: date
    slot_ids: list[int] = Field(min_length=1, max_length=2)


class BookingItemResponse(BaseModel):
    slot_id: int
    court_id: int
    court_code: str
    court_name: str
    service_date: date
    start_at: datetime
    end_at: datetime


class BookingResponse(BaseModel):
    id: int
    request_id: UUID
    booking_no: str
    status: BookingStatus
    confirmed_at: datetime | None
    created_at: datetime
    items: list[BookingItemResponse]


class BookingRequestResponse(BaseModel):
    request_id: UUID
    status: BookingRequestStatus
    error_code: str | None
    error_message: str | None
    replayed: bool = False
    booking: BookingResponse | None
