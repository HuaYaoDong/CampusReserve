from datetime import date, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, ExcludeConstraint
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from campus_reserve.shared.database import Base, TimestampMixin
from campus_reserve.shared.enums import (
    BookingRequestStatus,
    BookingStatus,
    OperatorType,
    enum_type,
)

if TYPE_CHECKING:
    from campus_reserve.checkins.models import CheckIn
    from campus_reserve.inventory.models import BookingSlot, ReleaseBatch
    from campus_reserve.users.models import User
    from campus_reserve.venues.models import Venue


class Booking(TimestampMixin, Base):
    __tablename__ = "booking"
    __table_args__ = (
        UniqueConstraint("request_id", name="uq_booking_request_id"),
        UniqueConstraint("booking_no", name="uq_booking_booking_no"),
        CheckConstraint(
            "(status IN ('CONFIRMED', 'CHECKED_IN', 'COMPLETED')) "
            "= (confirmed_at IS NOT NULL)",
            name="confirmed_status_has_timestamp",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'CONFIRMED', 'CHECKED_IN', 'COMPLETED', 'FAILED')",
            name="status",
        ),
        Index("ix_booking_user_status_created", "user_id", "status", text("created_at DESC")),
        Index("ix_booking_venue_status_created", "venue_id", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    request_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    booking_no: Mapped[str] = mapped_column(String(32), nullable=False)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )
    venue_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("venue.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[BookingStatus] = mapped_column(
        enum_type(BookingStatus, 24), default=BookingStatus.PENDING, nullable=False
    )
    rule_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped["User"] = relationship(back_populates="bookings")
    venue: Mapped["Venue"] = relationship(back_populates="bookings")
    request: Mapped["BookingRequest | None"] = relationship(back_populates="booking", uselist=False)
    items: Mapped[list["BookingItem"]] = relationship(back_populates="booking")
    status_logs: Mapped[list["BookingStatusLog"]] = relationship(back_populates="booking")
    check_in: Mapped["CheckIn | None"] = relationship(back_populates="booking", uselist=False)
    allocated_slots: Mapped[list["BookingSlot"]] = relationship(
        back_populates="current_booking", foreign_keys="BookingSlot.current_booking_id"
    )


class BookingRequest(TimestampMixin, Base):
    __tablename__ = "booking_request"
    __table_args__ = (
        CheckConstraint("cardinality(slot_ids) BETWEEN 1 AND 2", name="slot_count_between_1_and_2"),
        CheckConstraint(
            "(status = 'CONFIRMED') = (booking_id IS NOT NULL)",
            name="confirmed_requires_booking",
        ),
        CheckConstraint(
            "(status = 'FAILED') = (error_code IS NOT NULL)", name="failed_requires_error"
        ),
        CheckConstraint(
            "finished_at IS NULL OR accepted_at <= finished_at", name="accepted_before_finished"
        ),
        CheckConstraint("status IN ('PROCESSING', 'CONFIRMED', 'FAILED')", name="status"),
        UniqueConstraint("booking_id", name="uq_booking_request_booking_id"),
        Index("ix_booking_request_user_created", "user_id", "created_at"),
        Index("ix_booking_request_status_accepted", "status", "accepted_at"),
    )

    request_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )
    release_batch_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("release_batch.id", ondelete="RESTRICT"), nullable=False
    )
    slot_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), nullable=False)
    status: Mapped[BookingRequestStatus] = mapped_column(
        enum_type(BookingRequestStatus, 16),
        default=BookingRequestStatus.PROCESSING,
        nullable=False,
    )
    booking_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("booking.id", ondelete="RESTRICT")
    )
    error_code: Mapped[str | None] = mapped_column(String(64))
    accepted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped["User"] = relationship(back_populates="booking_requests")
    release_batch: Mapped["ReleaseBatch"] = relationship(back_populates="booking_requests")
    booking: Mapped[Booking | None] = relationship(back_populates="request")


class BookingItem(TimestampMixin, Base):
    __tablename__ = "booking_item"
    __table_args__ = (
        UniqueConstraint("booking_id", "slot_id", name="uq_booking_item_booking_slot"),
        UniqueConstraint("slot_id", name="uq_booking_item_slot"),
        CheckConstraint("start_at < end_at", name="start_before_end"),
        CheckConstraint(
            "end_at - start_at = INTERVAL '60 minutes'", name="duration_exactly_60_minutes"
        ),
        ExcludeConstraint(
            ("user_id", "="),
            (func.tstzrange(text("start_at"), text("end_at"), "[)"), "&&"),
            name="booking_item_user_time_excl",
            using="gist",
        ),
        Index("ix_booking_item_user_date", "user_id", "service_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    booking_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("booking.id", ondelete="RESTRICT"), nullable=False
    )
    slot_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("booking_slot.id", ondelete="RESTRICT"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )
    service_date: Mapped[date] = mapped_column(Date, nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    booking: Mapped[Booking] = relationship(back_populates="items")
    slot: Mapped["BookingSlot"] = relationship(back_populates="booking_item")
    user: Mapped["User"] = relationship(back_populates="booking_items")


class BookingStatusLog(TimestampMixin, Base):
    __tablename__ = "booking_status_log"
    __table_args__ = (
        CheckConstraint(
            "from_status IS NULL OR from_status IN "
            "('PENDING', 'CONFIRMED', 'CHECKED_IN', 'COMPLETED', 'FAILED')",
            name="from",
        ),
        CheckConstraint(
            "to_status IN ('PENDING', 'CONFIRMED', 'CHECKED_IN', 'COMPLETED', 'FAILED')",
            name="to",
        ),
        CheckConstraint("operator_type IN ('ADMIN', 'SYSTEM')", name="operator"),
        Index("ix_booking_status_log_booking_created", "booking_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    booking_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("booking.id", ondelete="RESTRICT"), nullable=False
    )
    from_status: Mapped[BookingStatus | None] = mapped_column(
        enum_type(BookingStatus, 24)
    )
    to_status: Mapped[BookingStatus] = mapped_column(
        enum_type(BookingStatus, 24), nullable=False
    )
    operator_type: Mapped[OperatorType] = mapped_column(
        enum_type(OperatorType, 16), nullable=False
    )
    operator_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="RESTRICT")
    )
    reason: Mapped[str | None] = mapped_column(String(255))
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)

    booking: Mapped[Booking] = relationship(back_populates="status_logs")
