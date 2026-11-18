"""保存窑次手动选型、情景试算与 AI 看板快照。"""

import sqlalchemy as sa
from alembic import op

revision = "0031_kiln_analysis_runs"
down_revision = "0030_kiln_cycles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kiln_analysis_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.String(32), nullable=False, unique=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("cycle_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_kiln_analysis_runs_owner_cycle", "kiln_analysis_runs", ["user_id", "cycle_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_kiln_analysis_runs_owner_cycle", table_name="kiln_analysis_runs")
    op.drop_table("kiln_analysis_runs")
