"""mcp servers + tools cache

Revision ID: 0015_mcp_servers
Revises: 0014_chat_session_kb_ids
Create Date: 2026-08-06
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0015_mcp_servers"
down_revision: Union[str, None] = "0014_chat_session_kb_ids"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "mcp_servers",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("transport", sa.String(length=32), nullable=False),
        sa.Column("url", sa.String(length=1024), nullable=True),
        sa.Column("command", sa.String(length=512), nullable=True),
        sa.Column("args", mysql.JSON(), nullable=True),
        sa.Column("env", mysql.JSON(), nullable=True),
        sa.Column("headers", mysql.JSON(), nullable=True),
        sa.Column("timeout_seconds", sa.Float(), nullable=False, server_default="60"),
        sa.Column("remark", sa.String(length=512), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tools_cached_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.UniqueConstraint("public_id"),
    )
    op.create_index("ix_mcp_servers_public_id", "mcp_servers", ["public_id"])
    op.create_index("ix_mcp_servers_transport", "mcp_servers", ["transport"])

    op.create_table(
        "mcp_tools",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.String(length=32), nullable=False),
        sa.Column("server_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("input_schema", mysql.JSON(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
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
        sa.ForeignKeyConstraint(["server_id"], ["mcp_servers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id"),
    )
    op.create_index("ix_mcp_tools_public_id", "mcp_tools", ["public_id"])
    op.create_index("ix_mcp_tools_server_id", "mcp_tools", ["server_id"])
    op.create_index("ix_mcp_tools_name", "mcp_tools", ["name"])


def downgrade() -> None:
    op.drop_index("ix_mcp_tools_name", table_name="mcp_tools")
    op.drop_index("ix_mcp_tools_server_id", table_name="mcp_tools")
    op.drop_index("ix_mcp_tools_public_id", table_name="mcp_tools")
    op.drop_table("mcp_tools")
    op.drop_index("ix_mcp_servers_transport", table_name="mcp_servers")
    op.drop_index("ix_mcp_servers_public_id", table_name="mcp_servers")
    op.drop_table("mcp_servers")
