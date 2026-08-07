"""数据治理任务服务。"""

from __future__ import annotations

import base64
import re
import secrets
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from common.config import get_settings
from common.errors import AppError
from db.models.governance import GovTask


def short_id(n: int = 12) -> str:
    return secrets.token_hex((n + 1) // 2)[:n]


def _serialize(row: GovTask) -> dict[str, Any]:
    return {
        "id": row.public_id,
        "name": row.name,
        "description": row.description or "",
        "owner": row.owner,
        "sourceType": row.source_type,
        "status": row.status,
        "excel": row.excel_preview,
        "fileKey": row.file_key,
        "createdAt": row.created_at.isoformat() if row.created_at else None,
        "updatedAt": row.updated_at.isoformat() if row.updated_at else None,
    }


def build_search_text(
    name: str,
    description: str | None,
    excel: dict[str, Any] | None,
) -> str:
    parts = [name, description or ""]
    if excel:
        parts.append(str(excel.get("fileName") or ""))
        parts.append(str(excel.get("sheetName") or ""))
        headers = excel.get("headers") or []
        parts.append(" ".join(str(h) for h in headers))
        for row in (excel.get("rows") or [])[:50]:
            parts.append(" ".join(str(c) for c in (row or [])))
    return "\n".join(parts)


async def list_tasks(db: AsyncSession) -> list[dict[str, Any]]:
    result = await db.execute(select(GovTask).order_by(GovTask.updated_at.desc()))
    return [_serialize(r) for r in result.scalars().all()]


async def get_task(db: AsyncSession, public_id: str) -> GovTask:
    result = await db.execute(select(GovTask).where(GovTask.public_id == public_id))
    row = result.scalar_one_or_none()
    if not row:
        raise AppError(40401, "治理任务不存在", status_code=404)
    return row


async def get_task_dict(db: AsyncSession, public_id: str) -> dict[str, Any]:
    return _serialize(await get_task(db, public_id))


async def create_task(
    db: AsyncSession,
    *,
    name: str,
    description: str | None,
    owner: str,
    source_type: str,
    created_by: int | None,
) -> dict[str, Any]:
    row = GovTask(
        public_id=short_id(12),
        name=name.strip(),
        description=(description or "").strip() or None,
        owner=(owner or "管理员").strip() or "管理员",
        source_type=source_type or "excel",
        status="draft",
        search_text=build_search_text(name, description, None),
        created_by=created_by,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _serialize(row)


async def update_task(
    db: AsyncSession,
    public_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    owner: str | None = None,
    source_type: str | None = None,
) -> dict[str, Any]:
    row = await get_task(db, public_id)
    if name is not None:
        row.name = name.strip()
    if description is not None:
        row.description = description.strip() or None
    if owner is not None:
        row.owner = owner.strip() or "管理员"
    if source_type is not None:
        row.source_type = source_type
    row.search_text = build_search_text(row.name, row.description, row.excel_preview)
    await db.commit()
    await db.refresh(row)
    return _serialize(row)


async def delete_task(db: AsyncSession, public_id: str) -> None:
    row = await get_task(db, public_id)
    if row.file_key:
        path = Path(get_settings().storage_root) / row.file_key
        if path.is_file():
            try:
                path.unlink()
            except OSError:
                pass
    await db.delete(row)
    await db.commit()


async def save_excel_preview(
    db: AsyncSession,
    public_id: str,
    *,
    excel: dict[str, Any],
    content_base64: str | None = None,
) -> dict[str, Any]:
    row = await get_task(db, public_id)
    preview = {
        "fileName": str(excel.get("fileName") or "import.xlsx"),
        "sheetName": str(excel.get("sheetName") or "Sheet1"),
        "headers": list(excel.get("headers") or []),
        "rows": list(excel.get("rows") or [])[:200],
        "rowCount": int(excel.get("rowCount") or len(excel.get("rows") or [])),
        "importedAt": str(excel.get("importedAt") or ""),
    }
    row.excel_preview = preview
    row.status = "imported"
    row.search_text = build_search_text(row.name, row.description, preview)

    if content_base64:
        raw = content_base64.split(",")[-1] if "," in content_base64 else content_base64
        data = base64.b64decode(raw)
        rel = f"governance/{row.public_id}/{preview['fileName']}"
        dest = Path(get_settings().storage_root) / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        row.file_key = rel

    await db.commit()
    await db.refresh(row)
    return _serialize(row)


def _csv_cell(value: Any) -> str:
    s = "" if value is None else str(value)
    if any(c in s for c in (",", '"', "\n", "\r")):
        return '"' + s.replace('"', '""') + '"'
    return s


def build_csv_from_preview(excel: dict[str, Any]) -> bytes:
    headers = list(excel.get("headers") or [])
    rows = list(excel.get("rows") or [])
    lines = [",".join(_csv_cell(h) for h in headers)]
    for row in rows:
        cells = list(row or [])
        width = len(headers) if headers else len(cells)
        while len(cells) < width:
            cells.append("")
        lines.append(",".join(_csv_cell(c) for c in cells[:width]))
    # utf-8-sig so Excel on Windows opens Chinese correctly
    return ("\r\n".join(lines) + "\r\n").encode("utf-8-sig")


async def export_task_bytes(
    db: AsyncSession,
    public_id: str,
) -> tuple[bytes, str, str]:
    """返回 (内容, 文件名, media_type)。优先原文件，否则由预览生成 CSV。"""
    row = await get_task(db, public_id)
    if row.file_key:
        path = Path(get_settings().storage_root) / row.file_key
        if path.is_file():
            name = path.name or "export.bin"
            suffix = path.suffix.lower()
            media = {
                ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                ".xls": "application/vnd.ms-excel",
                ".csv": "text/csv; charset=utf-8",
            }.get(suffix, "application/octet-stream")
            return path.read_bytes(), name, media

    excel = row.excel_preview
    if not excel or not (excel.get("headers") or excel.get("rows")):
        raise AppError(40402, "任务尚无可导出的数据", status_code=404)

    raw_name = str(excel.get("fileName") or f"{row.name or 'export'}.csv")
    stem = Path(raw_name).stem or "export"
    filename = f"{stem}.csv"
    return build_csv_from_preview(excel), filename, "text/csv; charset=utf-8"


def _tokenize(q: str) -> list[str]:
    """中英混合：空白切分 + 中文 2/3 字片，便于匹配任务名与表头。"""
    q = (q or "").strip().lower()
    if not q:
        return []
    parts = re.split(r"[\s,，。；;、/\-_|？?！!：:（）()【】\[\]]+", q)
    tokens: list[str] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        tokens.append(p)
        # 中文连续串抽 2～4 字片
        cn = re.findall(r"[\u4e00-\u9fff]+", p)
        for block in cn:
            if len(block) <= 4:
                tokens.append(block)
            else:
                for n in (2, 3, 4):
                    for i in range(0, len(block) - n + 1):
                        tokens.append(block[i : i + n])
    # 去重保序
    seen: set[str] = set()
    out: list[str] = []
    for t in tokens:
        if t in seen or len(t) < 1:
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= 40:
            break
    return out


_SOFT_KEYS = (
    "治理",
    "excel",
    "xls",
    "点位",
    "导入",
    "表格",
    "窑炉",
    "窑",
    "烧成",
    "配料",
    "温度",
    "流量",
    "数据",
    "设定",
    "空燃",
    "一区",
    "二区",
    "三区",
    "查看",
)


async def search_for_chat(
    db: AsyncSession,
    query: str,
    *,
    top_k: int = 3,
) -> list[dict[str, Any]]:
    """检索治理任务 Excel 预览，供对话注入。"""
    q = (query or "").strip()
    if not q:
        return []
    q_lower = q.lower()
    tokens = _tokenize(q)

    result = await db.execute(
        select(GovTask)
        .where(GovTask.excel_preview.isnot(None))
        .order_by(GovTask.updated_at.desc())
        .limit(50)
    )
    rows = list(result.scalars().all())
    if not rows:
        return []

    scored: list[tuple[float, GovTask]] = []
    soft = any(k.lower() in q_lower for k in _SOFT_KEYS)

    for row in rows:
        text = (row.search_text or "").lower()
        name = (row.name or "").strip()
        name_l = name.lower()
        file_name = str((row.excel_preview or {}).get("fileName") or "").lower()
        score = 0.0

        # 任务名互含（解决「窑烧成数据」整句匹配）
        if name and (name_l in q_lower or q_lower in name_l):
            score += 8.0
        if file_name and (file_name in q_lower or any(t in file_name for t in tokens if len(t) >= 3)):
            score += 3.0

        if text:
            for t in tokens:
                if len(t) >= 2 and t in text:
                    score += 1.0
                elif len(t) == 1 and t in name_l:
                    score += 0.2

        if score <= 0 and soft:
            score = 0.5  # 宽泛相关时带上最近导入任务

        if score > 0:
            scored.append((score, row))

    # 仍无命中但用户在问「数据/查看」且存在导入任务 → 返回最近 1～2 条
    if not scored and rows and any(k in q for k in ("数据", "查看", "导入", "表格", "excel", "Excel")):
        scored = [(0.2, r) for r in rows[:2]]

    scored.sort(key=lambda x: x[0], reverse=True)
    out: list[dict[str, Any]] = []
    for score, row in scored[:top_k]:
        excel = row.excel_preview or {}
        headers = excel.get("headers") or []
        sample_rows = excel.get("rows") or []
        # 多行表头时尽量多给几行，便于模型读数
        lines = [" | ".join(str(h) for h in headers)]
        for r in sample_rows[:20]:
            lines.append(" | ".join(str(c) for c in (r or [])))
        table = "\n".join(lines)
        out.append(
            {
                "taskId": row.public_id,
                "taskName": row.name,
                "fileName": excel.get("fileName"),
                "score": score,
                "content": (
                    f"治理任务「{row.name}」已导入文件 {excel.get('fileName')} "
                    f"（Sheet={excel.get('sheetName')}，共 {excel.get('rowCount')} 行）。\n"
                    f"预览表（含表头与样例行）：\n{table}"
                ),
            }
        )
    return out
