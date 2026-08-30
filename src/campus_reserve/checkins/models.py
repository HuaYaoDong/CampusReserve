from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from campus_reserve.shared.database import Base, TimestampMixin
from campus_reserve.shared.enums import CheckInMethod, enum_type

if TYPE_CHECKING:
    from campus_reserve.bookings.models import Booking
    from campus_reserve.users.models import User


class CheckIn(TimestampMixin, Base):
    __tablename__ = "check_in"
    __table_args__ = (
        UniqueConstraint("booking_id", name="uq_check_in_booking"),
        CheckConstraint("method IN ('ADMIN')", name="method"),
        Index("ix_check_in_user_checked_at", "user_id", "checked_in_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    booking_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("booking.id", ondelete="RESTRICT"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )
    method: Mapped[CheckInMethod] = mapped_column(
        enum_type(CheckInMethod, 16), default=CheckInMethod.ADMIN, nullable=False
    )
    checked_in_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    verifier_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )
    note: Mapped[str | None] = mapped_column(String(255))

    booking: Mapped["Booking"] = relationship(back_populates="check_in")
    user: Mapped["User"] = relationship(foreign_keys=[user_id])
    verifier: Mapped["User"] = relationship(foreign_keys=[verifier_id])
