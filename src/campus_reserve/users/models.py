from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from campus_reserve.shared.database import Base, TimestampMixin
from campus_reserve.shared.enums import UserRole, UserStatus, enum_type

if TYPE_CHECKING:
    from campus_reserve.bookings.models import Booking, BookingItem, BookingRequest


class User(TimestampMixin, Base):
    __tablename__ = "app_user"
    __table_args__ = (
        UniqueConstraint("username", name="uq_app_user_username"),
        UniqueConstraint("student_no", name="uq_app_user_student_no"),
        CheckConstraint(
            "role <> 'STUDENT' OR student_no IS NOT NULL",
            name="student_requires_student_no",
        ),
        CheckConstraint(
            "role IN ('STUDENT', 'VENUE_ADMIN', 'PLATFORM_ADMIN')", name="user_role"
        ),
        CheckConstraint("status IN ('ACTIVE', 'DISABLED', 'GRADUATED')", name="user_status"),
        Index("ix_app_user_status", "status"),
        Index("uq_app_user_username_lower", text("lower(username)"), unique=True),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    student_no: Mapped[str | None] = mapped_column(String(32))
    display_name: Mapped[str] = mapped_column(String(64), nullable=False)
    department: Mapped[str | None] = mapped_column(String(128))
    role: Mapped[UserRole] = mapped_column(
        enum_type(UserRole, 24), default=UserRole.STUDENT, nullable=False
    )
    status: Mapped[UserStatus] = mapped_column(
        enum_type(UserStatus, 16), default=UserStatus.ACTIVE, nullable=False
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    daily_usages: Mapped[list["UserDailyUsage"]] = relationship(back_populates="user")
    bookings: Mapped[list["Booking"]] = relationship(back_populates="user")
    booking_requests: Mapped[list["BookingRequest"]] = relationship(back_populates="user")
    booking_items: Mapped[list["BookingItem"]] = relationship(back_populates="user")


class UserDailyUsage(TimestampMixin, Base):
    __tablename__ = "user_daily_usage"
    __table_args__ = (
        CheckConstraint("used_slot_count BETWEEN 0 AND 2", name="slot_count_between_0_and_2"),
        CheckConstraint("version >= 0", name="version_non_negative"),
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="RESTRICT"), primary_key=True
    )
    service_date: Mapped[date] = mapped_column(Date, primary_key=True)
    used_slot_count: Mapped[int] = mapped_column(SmallInteger, default=0, nullable=False)
    version: Mapped[int] = mapped_column(default=0, nullable=False)

    user: Mapped[User] = relationship(back_populates="daily_usages")
