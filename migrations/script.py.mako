"""${message}

新建迁移时先检查 upgrade 和 downgrade 的可逆性，再核对数据库约束名称。

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    # 正向变更：由 Alembic 生成初稿后再补业务数据迁移。
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    # 回滚必须与正向迁移对称，不能仅删除表结构而遗留数据依赖。
    ${downgrades if downgrades else "pass"}

