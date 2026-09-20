"""角色菜单白名单 + 知识库 ACL。

Revision ID: 0025_role_menus_kb_acl
Revises: 0024_model_config_model_type
Create Date: 2026-09-20
"""

from __future__ import annotations

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0025_role_menus_kb_acl"
down_revision: Union[str, None] = "0024_model_config_model_type"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OPERATOR = [
    "/",
    "/realtime",
    "/furnaces",
    "/production/tunnel",
    "/production/batching",
    "/production/shuttle",
    "/alerts",
    "/ai-chat",
    "/knowledge",
]
_AUDITOR = [
    "/",
    "/realtime",
    "/reports",
    "/ai-reports",
    "/alerts",
    "/carbon",
    "/verification",
    "/product-footprint",
    "/energy",
    "/furnaces",
    "/knowledge",
    "/ai-chat",
]
_BUSINESS = [
    "/",
    "/realtime",
    "/ai-chat",
    "/ai-reports",
    "/casting-yield",
    "/casting-peel",
    "/casting-qa-month",
    "/knowledge",
    "/data-collect",
    "/data-governance",
    "/decision-flow",
    "/model-package",
    "/production/tunnel",
    "/production/batching",
    "/production/shuttle",
    "/furnaces",
    "/devices",
    "/energy",
    "/energy-flow",
    "/optimization",
    "/budget",
    "/carbon",
    "/verification",
    "/product-footprint",
    "/supply-chain",
    "/carbon-asset",
    "/carbon-market",
    "/policy",
    "/aps/orders",
    "/aps/mps",
    "/aps/overview",
    "/aps/furnace-schedule",
    "/aps/loading",
    "/aps/capacity",
    "/aps/material",
    "/aps/optimization",
    "/aps/energy-schedule",
    "/aps/emergency",
    "/aps/execution",
    "/aps/performance",
    "/quality/overview",
    "/quality/realtime",
    "/quality/prediction",
    "/quality/procurement",
    "/quality/models",
    "/quality/correlation",
    "/quality/trace",
    "/quality/alert",
    "/quality/report",
    "/quality/standard",
    "/quality/test-data",
    "/fault/overview",
    "/fault/realtime",
    "/fault/collection",
    "/fault/prediction",
    "/fault/models",
    "/fault/lifecycle",
    "/fault/alerts",
    "/fault/diagnosis",
    "/fault/maintenance",
    "/fault/spares",
    "/fault/knowledge",
    "/fault/reports",
    "/agents/plan",
    "/agents/procurement",
    "/agents/warehouse",
    "/agents/quality-trace",
    "/agents/vision-inspection",
    "/agents/root-cause",
    "/agents/quality-closed",
    "/agents/ops-workflow",
    "/reports",
    "/alerts",
    "/settings",
]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "roles" in tables:
        cols = {c["name"] for c in inspector.get_columns("roles")}
        if "menus" not in cols:
            op.add_column(
                "roles",
                sa.Column("menus", mysql.JSON(), nullable=True, comment="可访问菜单 href 列表"),
            )
        rows = bind.execute(sa.text("SELECT id, code FROM roles")).fetchall()
        seed = {"operator": _OPERATOR, "auditor": _AUDITOR}
        for rid, code in rows:
            if code == "admin":
                continue
            payload = seed.get(code, _BUSINESS)
            bind.execute(
                sa.text("UPDATE roles SET menus = CAST(:menus AS JSON) WHERE id = :id"),
                {"menus": json.dumps(payload, ensure_ascii=False), "id": rid},
            )

    created_acl = False
    if "knowledge_base_acl" not in tables:
        op.create_table(
            "knowledge_base_acl",
            sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False, comment="主键"),
            sa.Column("base_id", sa.BigInteger(), nullable=False, comment="知识库ID"),
            sa.Column(
                "subject_type", sa.String(length=16), nullable=False, comment="主体：user / role"
            ),
            sa.Column("subject_id", sa.BigInteger(), nullable=False, comment="users.id 或 roles.id"),
            sa.Column(
                "can_view",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("1"),
                comment="列表与详情可见",
            ),
            sa.Column(
                "can_use",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
                comment="对话检索 / 语义检索",
            ),
            sa.Column(
                "can_manage",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
                comment="改库、导入删除资料、分配权限",
            ),
            sa.ForeignKeyConstraint(["base_id"], ["knowledge_bases.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("base_id", "subject_type", "subject_id", name="uq_kb_acl_subject"),
            comment="知识库访问授权",
        )
        op.create_index("ix_knowledge_base_acl_base_id", "knowledge_base_acl", ["base_id"])
        created_acl = True

    if ("knowledge_bases" in tables and "roles" in tables) and (
        created_acl or "knowledge_base_acl" in tables
    ):
        role_rows = bind.execute(sa.text("SELECT id, code, menus FROM roles")).fetchall()
        grant_role_ids: list[int] = []
        for rid, code, menus in role_rows:
            if code == "admin":
                continue
            hrefs: list[str] = []
            if isinstance(menus, list):
                hrefs = [str(x) for x in menus]
            elif isinstance(menus, str) and menus.strip():
                try:
                    data = json.loads(menus)
                    if isinstance(data, list):
                        hrefs = [str(x) for x in data]
                except json.JSONDecodeError:
                    hrefs = []
            if "/knowledge" in hrefs:
                grant_role_ids.append(int(rid))
        if grant_role_ids:
            bases = bind.execute(
                sa.text(
                    "SELECT id FROM knowledge_bases "
                    "WHERE status <> 'deleted' AND (purpose IS NULL OR purpose = 'rag')"
                )
            ).fetchall()
            for (base_id,) in bases:
                for rid in grant_role_ids:
                    bind.execute(
                        sa.text(
                            "INSERT IGNORE INTO knowledge_base_acl "
                            "(base_id, subject_type, subject_id, can_view, can_use, can_manage) "
                            "VALUES (:base_id, 'role', :rid, 1, 1, 0)"
                        ),
                        {"base_id": int(base_id), "rid": int(rid)},
                    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "knowledge_base_acl" in tables:
        op.drop_index("ix_knowledge_base_acl_base_id", table_name="knowledge_base_acl")
        op.drop_table("knowledge_base_acl")
    if "roles" in tables:
        cols = {c["name"] for c in inspector.get_columns("roles")}
        if "menus" in cols:
            op.drop_column("roles", "menus")
