"""kiln_cycles：窑次文件与曲线/装载匹配参数

Revision ID: 0030_kiln_cycles
Revises: 0029_glass_defect_inspection
Create Date: 2026-11-13
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0030_kiln_cycles"
down_revision: Union[str, None] = "0029_glass_defect_inspection"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "kiln_cycles",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("kiln_code", sa.String(length=32), nullable=False, comment="窑炉编码"),
        sa.Column("batch_no", sa.String(length=16), nullable=False, server_default="", comment="窑次号"),
        sa.Column("source_file", sa.String(length=255), nullable=False, comment="来源文件名"),
        sa.Column("start_ts", sa.DateTime(), nullable=True, comment="窑次开始"),
        sa.Column("end_ts", sa.DateTime(), nullable=True, comment="窑次结束"),
        sa.Column("sample_count", sa.Integer(), nullable=False, server_default="0", comment="工况行数"),
        sa.Column("curve_version", sa.String(length=64), nullable=True, comment="曲线版本"),
        sa.Column("load_kg", sa.Float(), nullable=True, comment="装载量 kg"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment="创建时间",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment="更新时间",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("kiln_code", "source_file", "batch_no", name="uq_kiln_cycles_identity"),
        comment="窑次文件与匹配参数",
    )
    op.create_index("ix_kiln_cycles_kiln_code", "kiln_cycles", ["kiln_code"])


def downgrade() -> None:
    op.drop_index("ix_kiln_cycles_kiln_code", table_name="kiln_cycles")
    op.drop_table("kiln_cycles")
