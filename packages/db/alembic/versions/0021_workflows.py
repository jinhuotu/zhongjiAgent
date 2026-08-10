"""workflows + versions + runs + steps

Revision ID: 0021_workflows
Revises: 0020_alerts_biz_reports
Create Date: 2026-08-08
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0021_workflows"
down_revision: Union[str, None] = "0020_alerts_biz_reports"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "workflows",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.String(length=32), nullable=False, comment="对外ID"),
        sa.Column("name", sa.String(length=128), nullable=False, comment="工作流名称"),
        sa.Column("remark", sa.Text(), nullable=True, comment="备注"),
        sa.Column(
            "domain",
            sa.String(length=32),
            nullable=False,
            server_default="ai",
            comment="业务域，默认 ai",
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("1"),
            comment="是否启用",
        ),
        sa.Column("created_by", sa.BigInteger(), nullable=True, comment="创建人用户ID"),
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
        comment="工作流定义",
    )
    op.create_index("ix_workflows_public_id", "workflows", ["public_id"], unique=True)

    op.create_table(
        "workflow_versions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.String(length=32), nullable=False, comment="对外ID"),
        sa.Column("workflow_id", sa.BigInteger(), nullable=False, comment="所属工作流ID"),
        sa.Column("version", sa.Integer(), nullable=False, comment="版本号"),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="draft",
            comment="状态：draft/published",
        ),
        sa.Column(
            "graph_json",
            mysql.JSON(),
            nullable=True,
            comment="流程图 JSON（nodes/edges）",
        ),
        sa.Column("changelog", sa.Text(), nullable=True, comment="变更说明"),
        sa.Column("created_by", sa.BigInteger(), nullable=True, comment="创建人用户ID"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="创建时间",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_id"], ["workflows.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        comment="工作流版本",
    )
    op.create_index(
        "ix_workflow_versions_public_id", "workflow_versions", ["public_id"], unique=True
    )
    op.create_index(
        "ix_workflow_versions_workflow_id", "workflow_versions", ["workflow_id"], unique=False
    )

    op.create_table(
        "workflow_runs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.String(length=32), nullable=False, comment="对外ID"),
        sa.Column("workflow_id", sa.BigInteger(), nullable=False, comment="所属工作流ID"),
        sa.Column("version_id", sa.BigInteger(), nullable=False, comment="使用的版本ID"),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="pending",
            comment="状态：pending/running/done/failed",
        ),
        sa.Column(
            "trigger", sa.String(length=32), nullable=True, comment="触发方式：trial/api 等"
        ),
        sa.Column("input_json", mysql.JSON(), nullable=True, comment="输入 JSON"),
        sa.Column("output_json", mysql.JSON(), nullable=True, comment="输出 JSON"),
        sa.Column("error_msg", sa.String(length=512), nullable=True, comment="失败原因"),
        sa.Column("created_by", sa.BigInteger(), nullable=True, comment="创建人用户ID"),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), nullable=True, comment="开始时间"
        ),
        sa.Column(
            "finished_at", sa.DateTime(timezone=True), nullable=True, comment="结束时间"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="创建时间",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_id"], ["workflows.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["version_id"], ["workflow_versions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        comment="工作流运行记录",
    )
    op.create_index(
        "ix_workflow_runs_public_id", "workflow_runs", ["public_id"], unique=True
    )
    op.create_index(
        "ix_workflow_runs_workflow_id", "workflow_runs", ["workflow_id"], unique=False
    )
    op.create_index(
        "ix_workflow_runs_version_id", "workflow_runs", ["version_id"], unique=False
    )

    op.create_table(
        "workflow_run_steps",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False, comment="所属运行ID"),
        sa.Column("node_id", sa.String(length=64), nullable=False, comment="节点ID"),
        sa.Column(
            "node_type",
            sa.String(length=32),
            nullable=False,
            comment="节点类型：start/end/knowledge/llm/agent/mcp",
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="pending",
            comment="状态：pending/running/done/failed",
        ),
        sa.Column("detail_json", mysql.JSON(), nullable=True, comment="步骤详情 JSON"),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), nullable=True, comment="开始时间"
        ),
        sa.Column(
            "finished_at", sa.DateTime(timezone=True), nullable=True, comment="结束时间"
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["workflow_runs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        comment="工作流运行步骤",
    )
    op.create_index(
        "ix_workflow_run_steps_run_id", "workflow_run_steps", ["run_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_workflow_run_steps_run_id", table_name="workflow_run_steps")
    op.drop_table("workflow_run_steps")
    op.drop_index("ix_workflow_runs_version_id", table_name="workflow_runs")
    op.drop_index("ix_workflow_runs_workflow_id", table_name="workflow_runs")
    op.drop_index("ix_workflow_runs_public_id", table_name="workflow_runs")
    op.drop_table("workflow_runs")
    op.drop_index("ix_workflow_versions_workflow_id", table_name="workflow_versions")
    op.drop_index("ix_workflow_versions_public_id", table_name="workflow_versions")
    op.drop_table("workflow_versions")
    op.drop_index("ix_workflows_public_id", table_name="workflows")
    op.drop_table("workflows")
