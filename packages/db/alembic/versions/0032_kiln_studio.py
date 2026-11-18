"""Isolated, versioned kiln modelling assets."""

import sqlalchemy as sa
from alembic import op

revision = "0032_kiln_studio"
down_revision = "0031_kiln_analysis_runs"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "kiln_studio_assets",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.String(32), unique=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("title", sa.String(180), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_kiln_studio_owner_project", "kiln_studio_assets", ["user_id", "project_id", "kind"]
    )


def downgrade():
    op.drop_table("kiln_studio_assets")
