"""auditor 角色菜单增加审计日志 /logs；热配置审计纳入合规清理说明。

Revision ID: 0028_auditor_logs_menu
Revises: 0027_audit_p1
Create Date: 2026-09-21
"""

from __future__ import annotations

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0028_auditor_logs_menu"
down_revision: Union[str, None] = "0027_audit_p1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, menus FROM roles WHERE code = 'auditor'")
    ).fetchall()
    for row in rows:
        role_id, raw = row[0], row[1]
        menus: list[str] = []
        if isinstance(raw, list):
            menus = [str(x) for x in raw if x]
        elif isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = []
            if isinstance(parsed, list):
                menus = [str(x) for x in parsed if x]
        if "/logs" in menus:
            continue
        menus.append("/logs")
        conn.execute(
            sa.text("UPDATE roles SET menus = CAST(:menus AS JSON) WHERE id = :id"),
            {"menus": json.dumps(menus, ensure_ascii=False), "id": role_id},
        )


def downgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, menus FROM roles WHERE code = 'auditor'")
    ).fetchall()
    for row in rows:
        role_id, raw = row[0], row[1]
        menus: list[str] = []
        if isinstance(raw, list):
            menus = [str(x) for x in raw if x]
        elif isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = []
            if isinstance(parsed, list):
                menus = [str(x) for x in parsed if x]
        if "/logs" not in menus:
            continue
        menus = [m for m in menus if m != "/logs"]
        conn.execute(
            sa.text("UPDATE roles SET menus = CAST(:menus AS JSON) WHERE id = :id"),
            {"menus": json.dumps(menus, ensure_ascii=False), "id": role_id},
        )
