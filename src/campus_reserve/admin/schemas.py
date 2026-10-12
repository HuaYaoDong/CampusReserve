from datetime import date, datetime, time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from campus_reserve.shared.enums import (
    BookingStatus,
    CalendarExceptionType,
    CourtStatus,
    ReleaseBatchStatus,
    TemplateStatus,
    VenueStatus,
)


class VenuePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    address: str | None = Field(default=None, max_length=255)
    notice: str | None = None
    status: VenueStatus | None = None


class CourtPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    status: CourtStatus | None = None
    facilities: dict[str, Any] | None = None


class TemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    weekday_mask: int = Field(default=127, ge=1, le=127)
    valid_from: date
    valid_to: date | None = None
    status: TemplateStatus = TemplateStatus.DRAFT

    @model_validator(mode="after")
    def valid_dates(self):
        if self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValueError("结束日期不能早于开始日期")
        return self


class TemplatePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    weekday_mask: int | None = Field(default=None, ge=1, le=127)
    valid_from: date | None = None
    valid_to: date | None = None
    status: TemplateStatus | None = None


class ExceptionUpsert(BaseModel):
    exception_date: date
    court_id: int | None = None
    type: CalendarExceptionType
    custom_open_time: time | None = None
    custom_close_time: time | None = None
    reason: str = Field(min_length=1, max_length=255)

    @model_validator(mode="after")
    def valid_hours(self):
        if self.type is CalendarExceptionType.CUSTOM_HOURS:
            if (
                self.custom_open_time is None
                or self.custom_close_time is None
                or self.custom_open_time >= self.custom_close_time
            ):
                raise ValueError("自定义开放时间必须填写有效的起止时间")
        elif self.custom_open_time is not None or self.custom_close_time is not None:
            raise ValueError("关闭日期不能填写开放时间")
        return self


class GenerationRequest(BaseModel):
    service_date: date
    preview_only: bool = True


class CheckInRequest(BaseModel):
    note: str | None = Field(default=None, max_length=255)


class VenueView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    address: str
    notice: str | None
    status: VenueStatus


class CourtView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    status: CourtStatus
    facilities: dict[str, Any]


class TemplateView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    weekday_mask: int
    open_time: time
    close_time: time
    slot_minutes: int
    advance_days: int
    release_time: time
    valid_from: date
    valid_to: date | None
    status: TemplateStatus


class ExceptionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    exception_date: date
    court_id: int | None
    type: CalendarExceptionType
    custom_open_time: time | None
    custom_close_time: time | None
    reason: str


class BatchView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    service_date: date
    release_at: datetime
    status: ReleaseBatchStatus
    slot_count: int


class AdminOverview(BaseModel):
    venue: VenueView
    courts: list[CourtView]
    templates: list[TemplateView]
    exceptions: list[ExceptionView]
    batches: list[BatchView]


class GenerationPreview(BaseModel):
    service_date: date
    release_at: datetime
    slot_count: int
    court_count: int
    existing_batch_id: int | None
    batch: BatchView | None = None


class AdminBookingView(BaseModel):
    id: int
    booking_no: str
    username: str
    display_name: str
    status: BookingStatus
    service_date: date
    courts: str
    start_at: datetime
    end_at: datetime
    checked_in_at: datetime | None


class CheckInView(BaseModel):
    booking_id: int
    booking_no: str
    booking_status: BookingStatus
    checked_in_at: datetime
    verifier_id: int
    replayed: bool


class AuditView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    actor_id: int | None
    action: str
    resource_type: str
    resource_id: str
    before_data: dict[str, Any] | None
    after_data: dict[str, Any] | None
    created_at: datetime


class AdminMessage(BaseModel):
    message: str
