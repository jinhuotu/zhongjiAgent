"""model_configs.model_type: 文本对话 / 多模态视觉 / 多模态音频 / 文本向量

Revision ID: 0024_model_config_model_type
Revises: 0023_audit_logs
Create Date: 2026-08-15
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0024_model_config_model_type"
down_revision: Union[str, None] = "0023_audit_logs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "model_configs",
        sa.Column(
            "model_type",
            sa.String(length=32),
            nullable=False,
            server_default="text_chat",
            comment="模型类型：文本对话/多模态视觉/多模态音频/文本向量",
        ),
    )
    op.create_index("ix_model_configs_model_type", "model_configs", ["model_type"])
    op.execute(
        sa.text(
            "UPDATE model_configs SET model_type = 'text_embedding' WHERE kind = 'embedding'"
        )
    )


def downgrade() -> None:
    op.drop_index("ix_model_configs_model_type", table_name="model_configs")
    op.drop_column("model_configs", "model_type")
