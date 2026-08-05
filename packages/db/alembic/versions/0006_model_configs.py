"""model configs for LLM / Embedding

Revision ID: 0006_model_configs
Revises: 0005_chat_sessions
Create Date: 2026-07-31
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_model_configs"
down_revision: Union[str, None] = "0005_chat_sessions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "model_configs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("api_base", sa.String(length=512), nullable=False),
        sa.Column("api_key", sa.String(length=512), nullable=False),
        sa.Column("model_name", sa.String(length=128), nullable=False),
        sa.Column("temperature", sa.Float(), nullable=True),
        sa.Column("timeout_seconds", sa.Float(), nullable=False, server_default="120"),
        sa.Column("embedding_dim", sa.Integer(), nullable=True),
        sa.Column("remark", sa.String(length=512), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("scope_fast", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("scope_deep", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("scope_embedding", sa.Boolean(), nullable=False, server_default=sa.text("0")),
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
    op.create_index("ix_model_configs_public_id", "model_configs", ["public_id"])
    op.create_index("ix_model_configs_kind", "model_configs", ["kind"])


def downgrade() -> None:
    op.drop_index("ix_model_configs_kind", table_name="model_configs")
    op.drop_index("ix_model_configs_public_id", table_name="model_configs")
    op.drop_table("model_configs")
