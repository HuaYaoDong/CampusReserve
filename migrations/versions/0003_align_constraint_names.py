"""Give the booking request one-to-one constraint a stable explicit name.

Revision ID: 0003_constraint_names
Revises: 0002_core_models
"""

from alembic import op

revision = "0003_constraint_names"
down_revision = "0002_core_models"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE booking_request RENAME CONSTRAINT "
        "booking_request_booking_id_key TO uq_booking_request_booking_id"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE booking_request RENAME CONSTRAINT "
        "uq_booking_request_booking_id TO booking_request_booking_id_key"
    )
