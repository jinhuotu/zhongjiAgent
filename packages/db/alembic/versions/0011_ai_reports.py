"""ai_reports

Revision ID: 0011_ai_reports
Revises: 0010_furnaces_process_samples
Create Date: 2026-08-03
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011_ai_reports"
down_revision: Union[str, None] = "0010_furnaces_process_samples"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ai_reports",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("report_type", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("kiln_code", sa.String(length=32), nullable=False),
        sa.Column("kiln_name", sa.String(length=128), nullable=True),
        sa.Column("mode", sa.String(length=16), nullable=False, server_default="deep"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="generating"),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("char_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("refs", sa.JSON(), nullable=True),
        sa.Column("context_summary", sa.Text(), nullable=True),
        sa.Column("error_msg", sa.String(length=512), nullable=True),
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
        sa.UniqueConstraint("public_id"),
    )
    op.create_index("ix_ai_reports_public_id", "ai_reports", ["public_id"])
    op.create_index("ix_ai_reports_user_id", "ai_reports", ["user_id"])
    op.create_index("ix_ai_reports_report_type", "ai_reports", ["report_type"])
    op.create_index("ix_ai_reports_kiln_code", "ai_reports", ["kiln_code"])


def downgrade() -> None:
    op.drop_index("ix_ai_reports_kiln_code", table_name="ai_reports")
    op.drop_index("ix_ai_reports_report_type", table_name="ai_reports")
    op.drop_index("ix_ai_reports_user_id", table_name="ai_reports")
    op.drop_index("ix_ai_reports_public_id", table_name="ai_reports")
    op.drop_table("ai_reports")
