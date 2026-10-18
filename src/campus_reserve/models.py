"""Import every ORM model so Alembic sees one complete metadata graph.

集中导入 ORM 模型，让 Alembic 能看到完整元数据关系。"""

from campus_reserve.admin.models import AuditLog
from campus_reserve.bookings.models import Booking, BookingItem, BookingRequest, BookingStatusLog
from campus_reserve.checkins.models import CheckIn
from campus_reserve.inventory.models import BookingSlot, ReleaseBatch
from campus_reserve.users.models import User, UserDailyUsage
from campus_reserve.venues.models import CalendarException, Court, OpeningTemplate, Venue

__all__ = [
    "AuditLog",
    "Booking",
    "BookingItem",
    "BookingRequest",
    "BookingSlot",
    "BookingStatusLog",
    "CalendarException",
    "CheckIn",
    "Court",
    "OpeningTemplate",
    "ReleaseBatch",
    "User",
    "UserDailyUsage",
    "Venue",
]
