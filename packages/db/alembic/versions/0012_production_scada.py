"""production scada tables

Revision ID: 0012_production_scada
Revises: 0011_ai_reports
Create Date: 2026-08-04
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012_production_scada"
down_revision: Union[str, None] = "0011_ai_reports"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "prod_systems",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("description", sa.String(length=512), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("meta", sa.JSON(), nullable=True),
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
    op.create_index("ix_prod_systems_code", "prod_systems", ["code"])

    op.create_table(
        "prod_tags",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("system_code", sa.String(length=32), nullable=False),
        sa.Column("tag_code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("unit", sa.String(length=32), nullable=True),
        sa.Column("group_name", sa.String(length=64), nullable=True),
        sa.Column("data_type", sa.String(length=16), nullable=False, server_default="number"),
        sa.Column("writable", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("alarm_lo", sa.Float(), nullable=True),
        sa.Column("alarm_hi", sa.Float(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("system_code", "tag_code", name="uq_prod_tags_system_tag"),
    )
    op.create_index("ix_prod_tags_system_code", "prod_tags", ["system_code"])

    op.create_table(
        "prod_samples",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("system_code", sa.String(length=32), nullable=False),
        sa.Column("tag_code", sa.String(length=64), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=False), nullable=False),
        sa.Column("value_num", sa.Float(), nullable=True),
        sa.Column("value_text", sa.String(length=256), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("system_code", "tag_code", "ts", name="uq_prod_samples_sys_tag_ts"),
    )
    op.create_index(
        "ix_prod_samples_sys_tag_ts",
        "prod_samples",
        ["system_code", "tag_code", "ts"],
    )

    op.create_table(
        "prod_alarms",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.String(length=32), nullable=False),
        sa.Column("system_code", sa.String(length=32), nullable=False),
        sa.Column("tag_code", sa.String(length=64), nullable=True),
        sa.Column("level", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("message", sa.String(length=512), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("raised_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acked_by", sa.BigInteger(), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id"),
    )
    op.create_index("ix_prod_alarms_public_id", "prod_alarms", ["public_id"])
    op.create_index("ix_prod_alarms_system_code", "prod_alarms", ["system_code"])

    op.create_table(
        "prod_commands",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.String(length=32), nullable=False),
        sa.Column("system_code", sa.String(length=32), nullable=False),
        sa.Column("tag_code", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False, server_default="write"),
        sa.Column("target_value", sa.Float(), nullable=True),
        sa.Column("target_text", sa.String(length=256), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("executor", sa.String(length=32), nullable=False, server_default="simulate"),
        sa.Column("result_msg", sa.String(length=512), nullable=True),
        sa.Column("requested_by", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id"),
    )
    op.create_index("ix_prod_commands_public_id", "prod_commands", ["public_id"])
    op.create_index("ix_prod_commands_system_code", "prod_commands", ["system_code"])


def downgrade() -> None:
    op.drop_index("ix_prod_commands_system_code", table_name="prod_commands")
    op.drop_index("ix_prod_commands_public_id", table_name="prod_commands")
    op.drop_table("prod_commands")
    op.drop_index("ix_prod_alarms_system_code", table_name="prod_alarms")
    op.drop_index("ix_prod_alarms_public_id", table_name="prod_alarms")
    op.drop_table("prod_alarms")
    op.drop_index("ix_prod_samples_sys_tag_ts", table_name="prod_samples")
    op.drop_table("prod_samples")
    op.drop_index("ix_prod_tags_system_code", table_name="prod_tags")
    op.drop_table("prod_tags")
    op.drop_index("ix_prod_systems_code", table_name="prod_systems")
    op.drop_table("prod_systems")
