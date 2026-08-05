"""hot configs + audits

Revision ID: 0009_hot_configs
Revises: 0008_chat_message_archive_fields
Create Date: 2026-08-01
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009_hot_configs"
down_revision: Union[str, None] = "0008_chat_message_archive_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "hot_configs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("config_key", sa.String(length=128), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("description", sa.String(length=512), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
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
        sa.UniqueConstraint("config_key"),
    )
    op.create_index("ix_hot_configs_config_key", "hot_configs", ["config_key"])

    op.create_table(
        "hot_config_audits",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("config_key", sa.String(length=128), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("action", sa.String(length=32), nullable=False, server_default="update"),
        sa.Column("operator_id", sa.BigInteger(), nullable=True),
        sa.Column("remark", sa.String(length=512), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_hot_config_audits_config_key", "hot_config_audits", ["config_key"])


def downgrade() -> None:
    op.drop_index("ix_hot_config_audits_config_key", table_name="hot_config_audits")
    op.drop_table("hot_config_audits")
    op.drop_index("ix_hot_configs_config_key", table_name="hot_configs")
    op.drop_table("hot_configs")
