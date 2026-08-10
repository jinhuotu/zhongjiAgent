"""alert_rules + biz_reports

Revision ID: 0020_alerts_biz_reports
Revises: 0019_mysql_comments
Create Date: 2026-08-08
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0020_alerts_biz_reports"
down_revision: Union[str, None] = "0019_mysql_comments"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "alert_rules",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False, comment="规则名称"),
        sa.Column("category", sa.String(length=32), nullable=False, comment="类型"),
        sa.Column("expression", sa.String(length=512), nullable=False, comment="表达式"),
        sa.Column("threshold", sa.String(length=64), nullable=True, comment="阈值"),
        sa.Column("channels", sa.String(length=128), nullable=True, comment="通知渠道"),
        sa.Column("devices", sa.String(length=128), nullable=True, comment="生效设备"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1"), comment="是否启用"),
        sa.Column("remark", sa.Text(), nullable=True, comment="备注"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="创建时间",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="更新时间",
        ),
        sa.PrimaryKeyConstraint("id"),
        comment="告警中心规则",
    )
    op.create_index("ix_alert_rules_public_id", "alert_rules", ["public_id"], unique=True)

    op.create_table(
        "biz_reports",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False, comment="报表名称"),
        sa.Column("report_type", sa.String(length=32), nullable=False, comment="类型"),
        sa.Column("period", sa.String(length=64), nullable=False, comment="统计周期"),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="generating",
            comment="状态",
        ),
        sa.Column("content", sa.Text(), nullable=True, comment="报表正文"),
        sa.Column(
            "char_count", sa.Integer(), nullable=False, server_default="0", comment="字符数"
        ),
        sa.Column("size_label", sa.String(length=32), nullable=True, comment="展示用大小"),
        sa.Column("created_by", sa.BigInteger(), nullable=True, comment="创建人用户ID"),
        sa.Column("created_by_name", sa.String(length=64), nullable=True, comment="创建人显示名"),
        sa.Column("template_key", sa.String(length=64), nullable=True, comment="模板键"),
        sa.Column("error_msg", sa.String(length=512), nullable=True, comment="失败原因"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="创建时间",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="更新时间",
        ),
        sa.PrimaryKeyConstraint("id"),
        comment="业务统计报表",
    )
    op.create_index("ix_biz_reports_public_id", "biz_reports", ["public_id"], unique=True)
    op.create_index("ix_biz_reports_status", "biz_reports", ["status"], unique=False)
    op.create_index("ix_biz_reports_report_type", "biz_reports", ["report_type"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_biz_reports_report_type", table_name="biz_reports")
    op.drop_index("ix_biz_reports_status", table_name="biz_reports")
    op.drop_index("ix_biz_reports_public_id", table_name="biz_reports")
    op.drop_table("biz_reports")
    op.drop_index("ix_alert_rules_public_id", table_name="alert_rules")
    op.drop_table("alert_rules")
