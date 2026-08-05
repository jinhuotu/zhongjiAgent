"""knowledge bases and document base_id

Revision ID: 0003_knowledge_bases
Revises: 0002_knowledge_documents
Create Date: 2026-07-31
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_knowledge_bases"
down_revision: Union[str, None] = "0002_knowledge_documents"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "knowledge_bases",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.String(length=512), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
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
    op.create_index("ix_knowledge_bases_public_id", "knowledge_bases", ["public_id"])

    # seed default base for existing documents
    op.execute(
        sa.text(
            "INSERT INTO knowledge_bases (public_id, name, description, status, created_by) "
            "VALUES ('defaultkb0001', '默认知识库', '系统迁移自动创建，可改名或新建其他知识库', 'active', NULL)"
        )
    )

    op.add_column(
        "knowledge_documents",
        sa.Column("base_id", sa.BigInteger(), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE knowledge_documents SET base_id = ("
            "SELECT id FROM ("
            "SELECT id FROM knowledge_bases WHERE public_id = 'defaultkb0001' LIMIT 1"
            ") AS _kb_default"
            ") WHERE base_id IS NULL"
        )
    )
    op.alter_column(
        "knowledge_documents",
        "base_id",
        existing_type=sa.BigInteger(),
        nullable=False,
    )
    op.create_index("ix_knowledge_documents_base_id", "knowledge_documents", ["base_id"])
    op.create_foreign_key(
        "fk_knowledge_documents_base_id",
        "knowledge_documents",
        "knowledge_bases",
        ["base_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint("fk_knowledge_documents_base_id", "knowledge_documents", type_="foreignkey")
    op.drop_index("ix_knowledge_documents_base_id", table_name="knowledge_documents")
    op.drop_column("knowledge_documents", "base_id")
    op.drop_index("ix_knowledge_bases_public_id", table_name="knowledge_bases")
    op.drop_table("knowledge_bases")
