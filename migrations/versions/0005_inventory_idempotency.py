"""Guarantee one inventory release batch per venue and service date.

Revision ID: 0005_inventory_idempotency
Revises: 0004_demo_accounts
"""

from alembic import op

revision = "0005_inventory_idempotency"
down_revision = "0004_demo_accounts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "uq_release_batch_venue_date_time",
        "release_batch",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_release_batch_venue_date",
        "release_batch",
        ["venue_id", "service_date"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_release_batch_venue_date",
        "release_batch",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_release_batch_venue_date_time",
        "release_batch",
        ["venue_id", "service_date", "release_at"],
    )
