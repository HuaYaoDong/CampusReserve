"""Seed hashed demonstration accounts for mock login.

Revision ID: 0004_demo_accounts
Revises: 0003_constraint_names
"""

from alembic import op

revision = "0004_demo_accounts"
down_revision = "0003_constraint_names"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE UNIQUE INDEX uq_app_user_username_lower ON app_user (lower(username))")
    op.execute(
        """
        INSERT INTO app_user
            (username, password_hash, student_no, display_name, department, role, status)
        VALUES
            (
                'student001',
                '$argon2id$v=19$m=65536,t=3,p=4$ZsghkEC/sljcgoGkhtMlXQ$lWZlCnrd8Xts2APLz2PiMzf+3FJFycSrxRMvMWIOinw',
                '20260001', '演示学生一', '计算机学院', 'STUDENT', 'ACTIVE'
            ),
            (
                'student002',
                '$argon2id$v=19$m=65536,t=3,p=4$Oaff00FB+7o25rQC37V/IA$EKx8nisfuFKA7xigJAOASB+moqAJy9XBqzr1rh6Oq48',
                '20260002', '演示学生二', '软件学院', 'STUDENT', 'ACTIVE'
            ),
            (
                'venue_admin',
                '$argon2id$v=19$m=65536,t=3,p=4$TuQkBMPHL6vjJza9JY5E0A$MxnPBjq0Y99y3ryn9SY6F8rjjT7f3PkRF30+izTAaJQ',
                NULL, '场馆管理员', '体育场馆中心', 'VENUE_ADMIN', 'ACTIVE'
            ),
            (
                'platform_admin',
                '$argon2id$v=19$m=65536,t=3,p=4$TuQkBMPHL6vjJza9JY5E0A$MxnPBjq0Y99y3ryn9SY6F8rjjT7f3PkRF30+izTAaJQ',
                NULL, '平台管理员', '信息化中心', 'PLATFORM_ADMIN', 'ACTIVE'
            )
        """
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM app_user WHERE username IN "
        "('student001', 'student002', 'venue_admin', 'platform_admin')"
    )
    op.execute("DROP INDEX IF EXISTS uq_app_user_username_lower")
