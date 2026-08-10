"""ai_reports 绑定已发布工作流

Revision ID: 0022_ai_report_workflow
Revises: 0021_workflows
Create Date: 2026-08-08
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0022_ai_report_workflow"
down_revision: Union[str, None] = "0021_workflows"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ai_reports",
        sa.Column(
            "workflow_public_id",
            sa.String(length=32),
            nullable=True,
            comment="绑定的已发布工作流ID",
        ),
    )
    op.add_column(
        "ai_reports",
        sa.Column(
            "workflow_run_id",
            sa.String(length=32),
            nullable=True,
            comment="本次生成对应的工作流运行ID",
        ),
    )
    op.create_index(
        "ix_ai_reports_workflow_public_id",
        "ai_reports",
        ["workflow_public_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_ai_reports_workflow_public_id", table_name="ai_reports")
    op.drop_column("ai_reports", "workflow_run_id")
    op.drop_column("ai_reports", "workflow_public_id")
