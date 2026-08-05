"""furnaces + kiln_process_samples

Revision ID: 0010_furnaces_process_samples
Revises: 0009_hot_configs
Create Date: 2026-08-01
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010_furnaces_process_samples"
down_revision: Union[str, None] = "0009_hot_configs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "furnaces",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("kiln_no", sa.String(length=32), nullable=True),
        sa.Column("type", sa.String(length=64), nullable=True),
        sa.Column("workshop", sa.String(length=128), nullable=True),
        sa.Column("capacity", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="idle"),
        sa.Column("remark", sa.String(length=512), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
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
        sa.UniqueConstraint("code"),
    )
    op.create_index("ix_furnaces_code", "furnaces", ["code"])

    op.create_table(
        "kiln_process_samples",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("kiln_code", sa.String(length=32), nullable=False),
        sa.Column("ts", sa.DateTime(), nullable=False),
        sa.Column("temp_sp", sa.Float(), nullable=True),
        sa.Column("afr_sp", sa.Float(), nullable=True),
        sa.Column("z1_temp", sa.Float(), nullable=True),
        sa.Column("z1_gas_flow", sa.Float(), nullable=True),
        sa.Column("z1_air_flow", sa.Float(), nullable=True),
        sa.Column("z1_air_valve", sa.Float(), nullable=True),
        sa.Column("z2_temp", sa.Float(), nullable=True),
        sa.Column("z2_gas_flow", sa.Float(), nullable=True),
        sa.Column("z2_air_flow", sa.Float(), nullable=True),
        sa.Column("z2_air_valve", sa.Float(), nullable=True),
        sa.Column("z3_temp", sa.Float(), nullable=True),
        sa.Column("z3_gas_flow", sa.Float(), nullable=True),
        sa.Column("z3_air_flow", sa.Float(), nullable=True),
        sa.Column("z3_air_valve", sa.Float(), nullable=True),
        sa.Column("z4_temp", sa.Float(), nullable=True),
        sa.Column("z4_gas_flow", sa.Float(), nullable=True),
        sa.Column("z4_air_flow", sa.Float(), nullable=True),
        sa.Column("z4_air_valve", sa.Float(), nullable=True),
        sa.Column("z5_temp", sa.Float(), nullable=True),
        sa.Column("z5_gas_flow", sa.Float(), nullable=True),
        sa.Column("z5_air_flow", sa.Float(), nullable=True),
        sa.Column("z5_air_valve", sa.Float(), nullable=True),
        sa.Column("z6_temp", sa.Float(), nullable=True),
        sa.Column("z6_gas_flow", sa.Float(), nullable=True),
        sa.Column("z6_air_flow", sa.Float(), nullable=True),
        sa.Column("z6_air_valve", sa.Float(), nullable=True),
        sa.Column("furnace_p_sp", sa.Float(), nullable=True),
        sa.Column("furnace_p_meas", sa.Float(), nullable=True),
        sa.Column("furnace_p_out", sa.Float(), nullable=True),
        sa.Column("gas_p_sp", sa.Float(), nullable=True),
        sa.Column("gas_p_meas", sa.Float(), nullable=True),
        sa.Column("gas_p_out", sa.Float(), nullable=True),
        sa.Column("air_p_sp", sa.Float(), nullable=True),
        sa.Column("air_p_meas", sa.Float(), nullable=True),
        sa.Column("air_p_out", sa.Float(), nullable=True),
        sa.Column("gas_flow_instant", sa.Float(), nullable=True),
        sa.Column("gas_flow_total", sa.Float(), nullable=True),
        sa.Column("air_flow_instant", sa.Float(), nullable=True),
        sa.Column("air_flow_total", sa.Float(), nullable=True),
        sa.Column("o2_sp", sa.Float(), nullable=True),
        sa.Column("o2_meas", sa.Float(), nullable=True),
        sa.Column("source_file", sa.String(length=255), nullable=True),
        sa.Column("batch_no", sa.String(length=16), nullable=True),
        sa.Column("zone_count", sa.Integer(), nullable=True),
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
        sa.UniqueConstraint("kiln_code", "ts", name="uq_kiln_process_samples_kiln_ts"),
    )
    op.create_index("ix_kiln_process_samples_kiln_code", "kiln_process_samples", ["kiln_code"])
    op.create_index("ix_kiln_process_samples_ts", "kiln_process_samples", ["ts"])


def downgrade() -> None:
    op.drop_index("ix_kiln_process_samples_ts", table_name="kiln_process_samples")
    op.drop_index("ix_kiln_process_samples_kiln_code", table_name="kiln_process_samples")
    op.drop_table("kiln_process_samples")
    op.drop_index("ix_furnaces_code", table_name="furnaces")
    op.drop_table("furnaces")
