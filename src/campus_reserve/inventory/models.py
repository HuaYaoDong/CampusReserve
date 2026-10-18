"""放号批次与最小场地时段库存实体。"""

from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from campus_reserve.shared.database import Base, TimestampMixin
from campus_reserve.shared.enums import BookingSlotStatus, ReleaseBatchStatus, enum_type

if TYPE_CHECKING:
    from campus_reserve.bookings.models import Booking, BookingItem, BookingRequest
    from campus_reserve.venues.models import Court, Venue


# 一个场馆在一个使用日期的一次放号；保存规则快照与操作状态。
class ReleaseBatch(TimestampMixin, Base):
    __tablename__ = "release_batch"
    __table_args__ = (
        UniqueConstraint(
            "venue_id", "service_date", name="uq_release_batch_venue_date"
        ),
        CheckConstraint("slot_count >= 0", name="slot_count_non_negative"),
        CheckConstraint("closed_at IS NULL OR opened_at IS NOT NULL", name="close_requires_open"),
        CheckConstraint(
            "closed_at IS NULL OR opened_at IS NULL OR opened_at <= closed_at",
            name="open_before_close",
        ),
        CheckConstraint(
            "status IN ('DRAFT', 'READY', 'OPEN', 'PAUSED', 'CLOSED')", name="status"
        ),
        Index("ix_release_batch_status_release_at", "status", "release_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    venue_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("venue.id", ondelete="RESTRICT"), nullable=False
    )
    service_date: Mapped[date] = mapped_column(Date, nullable=False)
    release_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[ReleaseBatchStatus] = mapped_column(
        enum_type(ReleaseBatchStatus, 16),
        default=ReleaseBatchStatus.DRAFT,
        nullable=False,
    )
    slot_count: Mapped[int] = mapped_column(default=0, nullable=False)
    rule_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    venue: Mapped["Venue"] = relationship(back_populates="release_batches")
    booking_slots: Mapped[list["BookingSlot"]] = relationship(back_populates="release_batch")
    booking_requests: Mapped[list["BookingRequest"]] = relationship(back_populates="release_batch")


# 最小可占用库存单元：场地加使用日期加固定时间窗。
class BookingSlot(TimestampMixin, Base):
    __tablename__ = "booking_slot"
    __table_args__ = (
        UniqueConstraint("court_id", "start_at", "end_at", name="uq_booking_slot_court_period"),
        CheckConstraint("start_at < end_at", name="start_before_end"),
        CheckConstraint(
            "end_at - start_at = INTERVAL '60 minutes'", name="duration_exactly_60_minutes"
        ),
        CheckConstraint(
            "(status = 'BOOKED') = (current_booking_id IS NOT NULL)",
            name="booked_requires_booking",
        ),
        CheckConstraint(
            "status = 'BLOCKED' OR block_reason IS NULL", name="block_reason_only_when_blocked"
        ),
        CheckConstraint("version >= 0", name="version_non_negative"),
        CheckConstraint(
            "status IN ('UNRELEASED', 'AVAILABLE', 'BOOKED', 'BLOCKED')", name="status"
        ),
        Index("ix_booking_slot_date_court_status", "service_date", "court_id", "status"),
        Index("ix_booking_slot_batch_status", "release_batch_id", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    court_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("court.id", ondelete="RESTRICT"), nullable=False
    )
    release_batch_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("release_batch.id", ondelete="RESTRICT"), nullable=False
    )
    service_date: Mapped[date] = mapped_column(Date, nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[BookingSlotStatus] = mapped_column(
        enum_type(BookingSlotStatus, 16),
        default=BookingSlotStatus.UNRELEASED,
        nullable=False,
    )
    current_booking_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("booking.id", ondelete="RESTRICT")
    )
    block_reason: Mapped[str | None] = mapped_column(String(255))
    version: Mapped[int] = mapped_column(default=0, nullable=False)

    court: Mapped["Court"] = relationship(back_populates="booking_slots")
    release_batch: Mapped[ReleaseBatch] = relationship(back_populates="booking_slots")
    current_booking: Mapped["Booking | None"] = relationship(
        back_populates="allocated_slots", foreign_keys=[current_booking_id]
    )
    booking_item: Mapped["BookingItem | None"] = relationship(back_populates="slot", uselist=False)
