"""chat_messages archive / tool / token fields

Revision ID: 0008_chat_message_archive_fields
Revises: 0007_user_profile_fields
Create Date: 2026-08-01
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0008_chat_message_archive_fields"
down_revision: Union[str, None] = "0007_user_profile_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "chat_messages",
        sa.Column("stream_msg_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "chat_messages",
        sa.Column("model_name", sa.String(length=128), nullable=True),
    )
    op.add_column("chat_messages", sa.Column("prompt_tokens", sa.Integer(), nullable=True))
    op.add_column(
        "chat_messages",
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
    )
    op.add_column("chat_messages", sa.Column("total_tokens", sa.Integer(), nullable=True))
    op.add_column(
        "chat_messages",
        sa.Column("tool_name", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "chat_messages",
        sa.Column("tool_input", mysql.JSON(), nullable=True),
    )
    op.add_column(
        "chat_messages",
        sa.Column("tool_output", mysql.JSON(), nullable=True),
    )
    op.add_column("chat_messages", sa.Column("tool_error", sa.Text(), nullable=True))
    op.add_column(
        "chat_messages",
        sa.Column("tool_duration_ms", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_chat_messages_stream_msg_id",
        "chat_messages",
        ["stream_msg_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_chat_messages_stream_msg_id", table_name="chat_messages")
    op.drop_column("chat_messages", "tool_duration_ms")
    op.drop_column("chat_messages", "tool_error")
    op.drop_column("chat_messages", "tool_output")
    op.drop_column("chat_messages", "tool_input")
    op.drop_column("chat_messages", "tool_name")
    op.drop_column("chat_messages", "total_tokens")
    op.drop_column("chat_messages", "completion_tokens")
    op.drop_column("chat_messages", "prompt_tokens")
    op.drop_column("chat_messages", "model_name")
    op.drop_column("chat_messages", "stream_msg_id")
