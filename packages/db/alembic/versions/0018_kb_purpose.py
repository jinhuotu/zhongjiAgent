"""knowledge_bases.purpose: rag vs asset

Revision ID: 0018_kb_purpose
Revises: 0017_scenario_agents
Create Date: 2026-08-07
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018_kb_purpose"
down_revision: Union[str, None] = "0017_scenario_agents"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "knowledge_bases",
        sa.Column(
            "purpose",
            sa.String(length=16),
            nullable=False,
            server_default="rag",
        ),
    )
    op.create_index("ix_knowledge_bases_purpose", "knowledge_bases", ["purpose"])

    # 旧版固定业务四库 → purpose=asset
    op.execute(
        sa.text(
            "UPDATE knowledge_bases SET purpose = 'asset' WHERE public_id IN "
            "('assetdefect01', 'assetops000001', 'assetenergy001', 'assetcarbon001') "
            "OR name IN ('缺陷库', '运维库', '能耗库', '碳排放库') "
            "OR description LIKE '%业务资料库%'"
        )
    )


def downgrade() -> None:
    op.drop_index("ix_knowledge_bases_purpose", table_name="knowledge_bases")
    op.drop_column("knowledge_bases", "purpose")
