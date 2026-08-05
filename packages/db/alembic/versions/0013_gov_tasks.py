"""gov_tasks table

Revision ID: 0013_gov_tasks
Revises: 0012_production_scada
Create Date: 2026-08-04
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013_gov_tasks"
down_revision: Union[str, None] = "0012_production_scada"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "gov_tasks",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.String(length=512), nullable=True),
        sa.Column("owner", sa.String(length=64), nullable=False, server_default="管理员"),
        sa.Column("source_type", sa.String(length=16), nullable=False, server_default="excel"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="draft"),
        sa.Column("excel_preview", sa.JSON(), nullable=True),
        sa.Column("search_text", sa.Text(), nullable=True),
        sa.Column("file_key", sa.String(length=512), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_gov_tasks_public_id", "gov_tasks", ["public_id"], unique=True)
    op.create_index("ix_gov_tasks_status", "gov_tasks", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_gov_tasks_status", table_name="gov_tasks")
    op.drop_index("ix_gov_tasks_public_id", table_name="gov_tasks")
    op.drop_table("gov_tasks")
