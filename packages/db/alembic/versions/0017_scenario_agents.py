"""scenario_agents table for scenario agent configs

Revision ID: 0017_scenario_agents
Revises: 0016_prompts
Create Date: 2026-08-07
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0017_scenario_agents"
down_revision: Union[str, None] = "0016_prompts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "scenario_agents",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("remark", sa.String(length=512), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("prompt_public_id", sa.String(length=32), nullable=True),
        sa.Column("knowledge_base_ids", mysql.JSON(), nullable=True),
        sa.Column("mode", sa.String(length=16), nullable=False, server_default="fast"),
        sa.Column("mcp_tool_ids", mysql.JSON(), nullable=True),
        sa.Column("tools_enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
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
    op.create_index("ix_scenario_agents_public_id", "scenario_agents", ["public_id"])
    op.create_index(
        "ix_scenario_agents_prompt_public_id",
        "scenario_agents",
        ["prompt_public_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_scenario_agents_prompt_public_id", table_name="scenario_agents")
    op.drop_index("ix_scenario_agents_public_id", table_name="scenario_agents")
    op.drop_table("scenario_agents")
