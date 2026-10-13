from datetime import date, time
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from campus_reserve.shared.database import Base, TimestampMixin
from campus_reserve.shared.enums import (
    CalendarExceptionType,
    CourtStatus,
    SportType,
    TemplateStatus,
    VenueStatus,
    enum_type,
)

if TYPE_CHECKING:
    from campus_reserve.bookings.models import Booking
    from campus_reserve.inventory.models import BookingSlot, ReleaseBatch


class Venue(TimestampMixin, Base):
    __tablename__ = "venue"
    __table_args__ = (
        UniqueConstraint("campus_code", name="uq_venue_campus_code"),
        CheckConstraint("status IN ('ACTIVE', 'CLOSED', 'MAINTENANCE')", name="venue_status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    campus_code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    address: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Shanghai")
    status: Mapped[VenueStatus] = mapped_column(
        enum_type(VenueStatus, 16), default=VenueStatus.ACTIVE, nullable=False
    )
    notice: Mapped[str | None] = mapped_column(Text)

    courts: Mapped[list["Court"]] = relationship(back_populates="venue")
    opening_templates: Mapped[list["OpeningTemplate"]] = relationship(back_populates="venue")
    release_batches: Mapped[list["ReleaseBatch"]] = relationship(back_populates="venue")
    bookings: Mapped[list["Booking"]] = relationship(back_populates="venue")


class Court(TimestampMixin, Base):
    __tablename__ = "court"
    __table_args__ = (
        UniqueConstraint("venue_id", "code", name="uq_court_venue_code"),
        CheckConstraint("sport_type IN ('BADMINTON')", name="sport_type"),
        CheckConstraint(
            "status IN ('ACTIVE', 'DISABLED', 'MAINTENANCE')", name="court_status"
        ),
        Index("ix_court_venue_status", "venue_id", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    venue_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("venue.id", ondelete="RESTRICT"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    sport_type: Mapped[SportType] = mapped_column(
        enum_type(SportType, 32), default=SportType.BADMINTON, nullable=False
    )
    status: Mapped[CourtStatus] = mapped_column(
        enum_type(CourtStatus, 16), default=CourtStatus.ACTIVE, nullable=False
    )
    details: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict, nullable=False)

    venue: Mapped[Venue] = relationship(back_populates="courts")
    booking_slots: Mapped[list["BookingSlot"]] = relationship(back_populates="court")


class OpeningTemplate(TimestampMixin, Base):
    __tablename__ = "opening_template"
    __table_args__ = (
        CheckConstraint("weekday_mask BETWEEN 1 AND 127", name="weekday_mask_valid"),
        CheckConstraint("open_time = TIME '08:00:00'", name="mvp_open_time_0800"),
        CheckConstraint("close_time = TIME '22:00:00'", name="mvp_close_time_2200"),
        CheckConstraint("slot_minutes = 60", name="mvp_slot_minutes_60"),
        CheckConstraint("advance_days = 1", name="mvp_advance_days_1"),
        CheckConstraint("release_time = TIME '08:00:00'", name="mvp_release_time_0800"),
        CheckConstraint("valid_to IS NULL OR valid_from <= valid_to", name="valid_date_range"),
        CheckConstraint("status IN ('DRAFT', 'ACTIVE', 'ARCHIVED')", name="template_status"),
        UniqueConstraint("venue_id", "name", name="uq_opening_template_venue_name"),
        Index(
            "uq_opening_template_active_per_venue",
            "venue_id",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    venue_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("venue.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    weekday_mask: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=127)
    open_time: Mapped[time] = mapped_column(Time, nullable=False, default=time(8, 0))
    close_time: Mapped[time] = mapped_column(Time, nullable=False, default=time(22, 0))
    slot_minutes: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=60)
    advance_days: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    release_time: Mapped[time] = mapped_column(Time, nullable=False, default=time(8, 0))
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date | None] = mapped_column(Date)
    rule_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    status: Mapped[TemplateStatus] = mapped_column(
        enum_type(TemplateStatus, 16),
        default=TemplateStatus.ACTIVE,
        nullable=False,
    )

    venue: Mapped[Venue] = relationship(back_populates="opening_templates")


class CalendarException(TimestampMixin, Base):
    __tablename__ = "calendar_exception"
    __table_args__ = (
        CheckConstraint(
            "(type = 'CLOSED' AND custom_open_time IS NULL AND custom_close_time IS NULL) "
            "OR (type = 'CUSTOM_HOURS' AND custom_open_time IS NOT NULL "
            "AND custom_close_time IS NOT NULL AND custom_open_time < custom_close_time)",
            name="exception_hours_match_type",
        ),
        CheckConstraint("type IN ('CLOSED', 'CUSTOM_HOURS')", name="type"),
        UniqueConstraint(
            "venue_id",
            "court_id",
            "exception_date",
            name="uq_calendar_exception_scope_date",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_calendar_exception_venue_date", "venue_id", "exception_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    venue_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("venue.id", ondelete="RESTRICT"), nullable=False
    )
    court_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("court.id", ondelete="RESTRICT")
    )
    exception_date: Mapped[date] = mapped_column(Date, nullable=False)
    type: Mapped[CalendarExceptionType] = mapped_column(
        enum_type(CalendarExceptionType, 16), nullable=False
    )
    custom_open_time: Mapped[time | None] = mapped_column(Time)
    custom_close_time: Mapped[time | None] = mapped_column(Time)
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
