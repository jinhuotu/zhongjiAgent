"""chat_sessions.knowledge_base_ids — remember last selected KBs per session

Revision ID: 0014_chat_session_kb_ids
Revises: 0013_gov_tasks
Create Date: 2026-08-05
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0014_chat_session_kb_ids"
down_revision: Union[str, None] = "0013_gov_tasks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "chat_sessions",
        sa.Column("knowledge_base_ids", mysql.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("chat_sessions", "knowledge_base_ids")
