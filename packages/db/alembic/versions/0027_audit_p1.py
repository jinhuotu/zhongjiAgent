"""operation_logs.request_id + 合规审计保留字段说明。

Revision ID: 0027_audit_p1
Revises: 0026_audit_trace
Create Date: 2026-09-21
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0027_audit_p1"
down_revision: Union[str, None] = "0026_audit_trace"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "operation_logs",
        sa.Column(
            "request_id",
            sa.String(length=64),
            nullable=False,
            server_default="",
            comment="请求ID（关联追溯）",
        ),
    )
    op.create_index("ix_operation_logs_request_id", "operation_logs", ["request_id"])


def downgrade() -> None:
    op.drop_index("ix_operation_logs_request_id", table_name="operation_logs")
    op.drop_column("operation_logs", "request_id")
