"""add preview_url / file_key; widen uploader

Revision ID: 0004_kb_doc_preview
Revises: 0003_knowledge_bases
Create Date: 2026-07-31
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_kb_doc_preview"
down_revision: Union[str, None] = "0003_knowledge_bases"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "knowledge_documents",
        sa.Column("file_key", sa.String(length=512), nullable=True),
    )
    op.add_column(
        "knowledge_documents",
        sa.Column("preview_url", sa.String(length=2048), nullable=True),
    )
    # backfill file_key from storage_path when present
    op.execute(
        sa.text(
            "UPDATE knowledge_documents SET file_key = storage_path "
            "WHERE file_key IS NULL AND storage_path IS NOT NULL"
        )
    )
    op.alter_column(
        "knowledge_documents",
        "uploader",
        existing_type=sa.String(length=64),
        type_=sa.String(length=128),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "knowledge_documents",
        "uploader",
        existing_type=sa.String(length=128),
        type_=sa.String(length=64),
        existing_nullable=True,
    )
    op.drop_column("knowledge_documents", "preview_url")
    op.drop_column("knowledge_documents", "file_key")
