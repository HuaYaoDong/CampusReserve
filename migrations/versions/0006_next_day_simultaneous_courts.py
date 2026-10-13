"""Open tomorrow's inventory and allow one student to reserve parallel courts.

Revision ID: 0006_next_day_parallel
Revises: 0005_inventory_idempotency
"""

from alembic import op

revision = "0006_next_day_parallel"
down_revision = "0005_inventory_idempotency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE booking_item DROP CONSTRAINT booking_item_user_time_excl")
    op.execute(
        "ALTER TABLE opening_template DROP CONSTRAINT ck_opening_template_mvp_advance_days_2"
    )
    op.execute("ALTER TABLE opening_template ALTER COLUMN advance_days SET DEFAULT 1")
    op.execute("UPDATE opening_template SET advance_days = 1")
    op.execute(
        "ALTER TABLE opening_template ADD CONSTRAINT "
        "ck_opening_template_mvp_advance_days_1 CHECK (advance_days = 1)"
    )
    op.execute(
        "UPDATE opening_template SET rule_json = rule_json || "
        "'{\"allow_simultaneous_courts\": true}'::jsonb"
    )
    op.execute(
        "UPDATE release_batch SET rule_snapshot = rule_snapshot || "
        "'{\"allow_simultaneous_courts\": true}'::jsonb "
        "WHERE service_date >= CURRENT_DATE"
    )
    op.execute(
        "UPDATE venue SET notice = '每天 08:00—22:00 开放，每天 08:00 放出明天场次。' "
        "WHERE id = 1 AND notice = "
        "'每天 08:00—22:00 开放，每天 08:00 放出未来第 2 天场次。'"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE booking_item ADD CONSTRAINT booking_item_user_time_excl "
        "EXCLUDE USING gist (user_id WITH =, tstzrange(start_at, end_at, '[)') WITH &&)"
    )
    op.execute(
        "ALTER TABLE opening_template DROP CONSTRAINT ck_opening_template_mvp_advance_days_1"
    )
    op.execute("ALTER TABLE opening_template ALTER COLUMN advance_days SET DEFAULT 2")
    op.execute("UPDATE opening_template SET advance_days = 2")
    op.execute(
        "ALTER TABLE opening_template ADD CONSTRAINT "
        "ck_opening_template_mvp_advance_days_2 CHECK (advance_days = 2)"
    )
    op.execute(
        "UPDATE opening_template SET rule_json = rule_json - 'allow_simultaneous_courts'"
    )
    op.execute(
        "UPDATE release_batch SET rule_snapshot = "
        "rule_snapshot - 'allow_simultaneous_courts' "
        "WHERE service_date >= CURRENT_DATE"
    )
    op.execute(
        "UPDATE venue SET notice = "
        "'每天 08:00—22:00 开放，每天 08:00 放出未来第 2 天场次。' "
        "WHERE id = 1 AND notice = '每天 08:00—22:00 开放，每天 08:00 放出明天场次。'"
    )
