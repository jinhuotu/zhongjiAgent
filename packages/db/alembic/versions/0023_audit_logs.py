"""login_logs + operation_logs（滚动保留 7 天）

Revision ID: 0023_audit_logs
Revises: 0022_ai_report_workflow
Create Date: 2026-08-14
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0023_audit_logs"
down_revision: Union[str, None] = "0022_ai_report_workflow"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "login_logs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False, comment="尝试登录的用户名"),
        sa.Column("user_id", sa.BigInteger(), nullable=True, comment="对应用户ID（未知则为空）"),
        sa.Column(
            "success",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
            comment="是否成功",
        ),
        sa.Column(
            "reason",
            sa.String(length=64),
            nullable=False,
            server_default="",
            comment="结果原因：ok/invalid_credentials/inactive",
        ),
        sa.Column(
            "ip",
            sa.String(length=64),
            nullable=False,
            server_default="",
            comment="客户端IP",
        ),
        sa.Column(
            "user_agent",
            sa.String(length=512),
            nullable=False,
            server_default="",
            comment="User-Agent",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="创建时间",
        ),
        sa.PrimaryKeyConstraint("id"),
        comment="登录日志",
    )
    op.create_index("ix_login_logs_username", "login_logs", ["username"])
    op.create_index("ix_login_logs_success", "login_logs", ["success"])
    op.create_index("ix_login_logs_created_at", "login_logs", ["created_at"])

    op.create_table(
        "operation_logs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "module",
            sa.String(length=32),
            nullable=False,
            comment="模块：users/roles/hot_configs/mcp/models",
        ),
        sa.Column(
            "action",
            sa.String(length=32),
            nullable=False,
            server_default="",
            comment="动作：create/update/delete/...",
        ),
        sa.Column("method", sa.String(length=16), nullable=False, comment="HTTP方法"),
        sa.Column("path", sa.String(length=512), nullable=False, comment="请求路径"),
        sa.Column("resource_id", sa.String(length=128), nullable=True, comment="资源ID"),
        sa.Column("operator_id", sa.BigInteger(), nullable=True, comment="操作人用户ID"),
        sa.Column("operator_username", sa.String(length=64), nullable=True, comment="操作人用户名"),
        sa.Column(
            "success",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("1"),
            comment="是否成功",
        ),
        sa.Column(
            "status_code",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="HTTP状态码",
        ),
        sa.Column(
            "ip",
            sa.String(length=64),
            nullable=False,
            server_default="",
            comment="客户端IP",
        ),
        sa.Column(
            "user_agent",
            sa.String(length=512),
            nullable=False,
            server_default="",
            comment="User-Agent",
        ),
        sa.Column("detail", sa.String(length=512), nullable=True, comment="摘要（已脱敏）"),
        sa.Column("error_msg", sa.String(length=512), nullable=True, comment="失败原因"),
        sa.Column(
            "duration_ms",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="耗时毫秒",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="创建时间",
        ),
        sa.PrimaryKeyConstraint("id"),
        comment="操作日志",
    )
    op.create_index("ix_operation_logs_module", "operation_logs", ["module"])
    op.create_index("ix_operation_logs_operator_username", "operation_logs", ["operator_username"])
    op.create_index("ix_operation_logs_success", "operation_logs", ["success"])
    op.create_index("ix_operation_logs_created_at", "operation_logs", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_operation_logs_created_at", table_name="operation_logs")
    op.drop_index("ix_operation_logs_success", table_name="operation_logs")
    op.drop_index("ix_operation_logs_operator_username", table_name="operation_logs")
    op.drop_index("ix_operation_logs_module", table_name="operation_logs")
    op.drop_table("operation_logs")
    op.drop_index("ix_login_logs_created_at", table_name="login_logs")
    op.drop_index("ix_login_logs_success", table_name="login_logs")
    op.drop_index("ix_login_logs_username", table_name="login_logs")
    op.drop_table("login_logs")
