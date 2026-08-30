from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from campus_reserve.shared.database import Base, TimestampMixin
from campus_reserve.shared.enums import UserRole, enum_type


class AuditLog(TimestampMixin, Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        CheckConstraint(
            "actor_role IN ('STUDENT', 'VENUE_ADMIN', 'PLATFORM_ADMIN')", name="actor_role"
        ),
        Index("ix_audit_log_actor_created", "actor_id", "created_at"),
        Index("ix_audit_log_resource", "resource_type", "resource_id"),
        Index("ix_audit_log_trace_id", "trace_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    actor_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="RESTRICT")
    )
    actor_role: Mapped[UserRole] = mapped_column(
        enum_type(UserRole, 24), nullable=False
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(64), nullable=False)
    before_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    ip: Mapped[str | None] = mapped_column(INET)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
