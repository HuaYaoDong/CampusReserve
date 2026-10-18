"""跨模块共用的身份、批次、库存、预约和签到状态枚举。"""

from enum import StrEnum
from typing import Any

from sqlalchemy import String
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.types import TypeDecorator


class StringEnum(TypeDecorator[StrEnum]):
    """Persist a StrEnum as VARCHAR while returning enum members to Python."""

    impl = String
    cache_ok = True

    def __init__(self, enum_class: type[StrEnum], length: int) -> None:
        self.enum_class = enum_class
        super().__init__(length=length)

    def process_bind_param(self, value: StrEnum | str | None, dialect: Dialect) -> str | None:
        del dialect
        if value is None:
            return None
        return self.enum_class(value).value

    def process_result_value(self, value: Any, dialect: Dialect) -> StrEnum | None:
        del dialect
        return None if value is None else self.enum_class(value)


def enum_type(enum_class: type[StrEnum], length: int) -> StringEnum:
    return StringEnum(enum_class, length)


class UserRole(StrEnum):
    STUDENT = "STUDENT"
    VENUE_ADMIN = "VENUE_ADMIN"
    PLATFORM_ADMIN = "PLATFORM_ADMIN"


class UserStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
    GRADUATED = "GRADUATED"


class VenueStatus(StrEnum):
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"
    MAINTENANCE = "MAINTENANCE"


class CourtStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
    MAINTENANCE = "MAINTENANCE"


class SportType(StrEnum):
    BADMINTON = "BADMINTON"


class TemplateStatus(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class CalendarExceptionType(StrEnum):
    CLOSED = "CLOSED"
    CUSTOM_HOURS = "CUSTOM_HOURS"


class ReleaseBatchStatus(StrEnum):
    DRAFT = "DRAFT"
    READY = "READY"
    OPEN = "OPEN"
    PAUSED = "PAUSED"
    CLOSED = "CLOSED"


class BookingSlotStatus(StrEnum):
    UNRELEASED = "UNRELEASED"
    AVAILABLE = "AVAILABLE"
    BOOKED = "BOOKED"
    BLOCKED = "BLOCKED"


class BookingStatus(StrEnum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    CHECKED_IN = "CHECKED_IN"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class BookingRequestStatus(StrEnum):
    PROCESSING = "PROCESSING"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"


class OperatorType(StrEnum):
    ADMIN = "ADMIN"
    SYSTEM = "SYSTEM"


class CheckInMethod(StrEnum):
    ADMIN = "ADMIN"
