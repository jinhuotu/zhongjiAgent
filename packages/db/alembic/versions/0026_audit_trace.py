"""知识库变更审计、数据查询日志、问答会话软删除。

Revision ID: 0026_audit_trace
Revises: 0025_role_menus_kb_acl
Create Date: 2026-09-21
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0026_audit_trace"
down_revision: Union[str, None] = "0025_role_menus_kb_acl"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "chat_sessions",
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="用户删除时间；非空则对用户隐藏，审计仍可查",
        ),
    )
    op.create_index("ix_chat_sessions_deleted_at", "chat_sessions", ["deleted_at"])

    op.create_table(
        "knowledge_audits",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False, comment="变更动作"),
        sa.Column("base_id", sa.String(length=32), nullable=True, comment="知识库对外ID"),
        sa.Column("doc_id", sa.String(length=32), nullable=True, comment="文档对外ID"),
        sa.Column("operator_id", sa.BigInteger(), nullable=True, comment="操作人用户ID"),
        sa.Column("operator_username", sa.String(length=64), nullable=True, comment="操作人用户名"),
        sa.Column("before_json", sa.Text(), nullable=True, comment="变更前摘要JSON"),
        sa.Column("after_json", sa.Text(), nullable=True, comment="变更后摘要JSON"),
        sa.Column("ip", sa.String(length=64), nullable=False, server_default="", comment="客户端IP"),
        sa.Column(
            "request_id",
            sa.String(length=64),
            nullable=False,
            server_default="",
            comment="请求ID",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="创建时间",
        ),
        sa.PrimaryKeyConstraint("id"),
        comment="知识库变更审计",
    )
    op.create_index("ix_knowledge_audits_action", "knowledge_audits", ["action"])
    op.create_index("ix_knowledge_audits_base_id", "knowledge_audits", ["base_id"])
    op.create_index("ix_knowledge_audits_operator_username", "knowledge_audits", ["operator_username"])
    op.create_index("ix_knowledge_audits_created_at", "knowledge_audits", ["created_at"])

    op.create_table(
        "data_query_logs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False, comment="来源：knowledge/mcp"),
        sa.Column(
            "query_text",
            sa.String(length=512),
            nullable=False,
            server_default="",
            comment="查询摘要",
        ),
        sa.Column("tool_name", sa.String(length=128), nullable=True, comment="工具名"),
        sa.Column("params_json", sa.Text(), nullable=True, comment="参数摘要JSON"),
        sa.Column("result_summary", sa.String(length=512), nullable=True, comment="结果摘要"),
        sa.Column(
            "success",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("1"),
            comment="是否成功",
        ),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0", comment="耗时毫秒"),
        sa.Column("operator_id", sa.BigInteger(), nullable=True, comment="查询人用户ID"),
        sa.Column("operator_username", sa.String(length=64), nullable=True, comment="查询人用户名"),
        sa.Column("session_id", sa.String(length=32), nullable=True, comment="关联问答会话对外ID"),
        sa.Column("ip", sa.String(length=64), nullable=False, server_default="", comment="客户端IP"),
        sa.Column(
            "request_id",
            sa.String(length=64),
            nullable=False,
            server_default="",
            comment="请求ID",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="创建时间",
        ),
        sa.PrimaryKeyConstraint("id"),
        comment="数据查询日志",
    )
    op.create_index("ix_data_query_logs_source", "data_query_logs", ["source"])
    op.create_index("ix_data_query_logs_success", "data_query_logs", ["success"])
    op.create_index(
        "ix_data_query_logs_operator_username", "data_query_logs", ["operator_username"]
    )
    op.create_index("ix_data_query_logs_session_id", "data_query_logs", ["session_id"])
    op.create_index("ix_data_query_logs_created_at", "data_query_logs", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_data_query_logs_created_at", table_name="data_query_logs")
    op.drop_index("ix_data_query_logs_session_id", table_name="data_query_logs")
    op.drop_index("ix_data_query_logs_operator_username", table_name="data_query_logs")
    op.drop_index("ix_data_query_logs_success", table_name="data_query_logs")
    op.drop_index("ix_data_query_logs_source", table_name="data_query_logs")
    op.drop_table("data_query_logs")

    op.drop_index("ix_knowledge_audits_created_at", table_name="knowledge_audits")
    op.drop_index("ix_knowledge_audits_operator_username", table_name="knowledge_audits")
    op.drop_index("ix_knowledge_audits_base_id", table_name="knowledge_audits")
    op.drop_index("ix_knowledge_audits_action", table_name="knowledge_audits")
    op.drop_table("knowledge_audits")

    op.drop_index("ix_chat_sessions_deleted_at", table_name="chat_sessions")
    op.drop_column("chat_sessions", "deleted_at")
