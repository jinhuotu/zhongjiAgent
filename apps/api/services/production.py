"""生产侧服务：快照 / 曲线 / 报警 / 模拟下发（真执行器预留）。"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from common.errors import AppError, ErrorCode
from db.models.production import ProdAlarm, ProdCommand, ProdSample, ProdSystem, ProdTag

SYSTEM_CODES = ("tunnel", "batching", "shuttle")


def short_id(n: int = 12) -> str:
    return secrets.token_hex((n + 1) // 2)[:n]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def list_systems(db: AsyncSession) -> list[dict[str, Any]]:
    result = await db.execute(
        select(ProdSystem).where(ProdSystem.enabled.is_(True)).order_by(ProdSystem.id)
    )
    items = []
    for row in result.scalars().all():
        items.append(
            {
                "code": row.code,
                "name": row.name,
                "kind": row.kind,
                "description": row.description,
                "meta": row.meta or {},
            }
        )
    return items


async def _require_system(db: AsyncSession, code: str) -> ProdSystem:
    result = await db.execute(select(ProdSystem).where(ProdSystem.code == code))
    row = result.scalar_one_or_none()
    if row is None or not row.enabled:
        raise AppError(ErrorCode.NOT_FOUND, f"system not found: {code}", status_code=404)
    return row


async def list_tags(db: AsyncSession, system_code: str) -> list[dict[str, Any]]:
    await _require_system(db, system_code)
    result = await db.execute(
        select(ProdTag)
        .where(ProdTag.system_code == system_code, ProdTag.enabled.is_(True))
        .order_by(ProdTag.sort_order, ProdTag.id)
    )
    return [
        {
            "tagCode": t.tag_code,
            "name": t.name,
            "unit": t.unit,
            "group": t.group_name,
            "dataType": t.data_type,
            "writable": bool(t.writable),
            "alarmLo": t.alarm_lo,
            "alarmHi": t.alarm_hi,
        }
        for t in result.scalars().all()
    ]


async def latest_values(
    db: AsyncSession,
    system_code: str,
    *,
    tag_codes: list[str] | None = None,
) -> dict[str, Any]:
    """每个 tag 取最新一条样本（单次 GROUP BY + JOIN，避免 N+1）。"""
    codes = tag_codes
    if codes is None:
        tags = await list_tags(db, system_code)
        codes = [t["tagCode"] for t in tags]
    values: dict[str, Any] = {c: None for c in codes}
    if not codes:
        return values

    max_ts_sub = (
        select(
            ProdSample.tag_code.label("tag_code"),
            func.max(ProdSample.ts).label("max_ts"),
        )
        .where(
            ProdSample.system_code == system_code,
            ProdSample.tag_code.in_(codes),
        )
        .group_by(ProdSample.tag_code)
        .subquery()
    )
    result = await db.execute(
        select(ProdSample)
        .join(
            max_ts_sub,
            (ProdSample.tag_code == max_ts_sub.c.tag_code)
            & (ProdSample.ts == max_ts_sub.c.max_ts),
        )
        .where(ProdSample.system_code == system_code)
    )
    for sample in result.scalars().all():
        if sample.value_num is not None:
            values[sample.tag_code] = sample.value_num
        else:
            values[sample.tag_code] = sample.value_text
    return values


async def get_snapshot(db: AsyncSession, system_code: str) -> dict[str, Any]:
    system = await _require_system(db, system_code)
    tags = await list_tags(db, system_code)
    values = await latest_values(
        db,
        system_code,
        tag_codes=[t["tagCode"] for t in tags],
    )

    groups: dict[str, list[dict[str, Any]]] = {}
    for t in tags:
        g = t["group"] or "default"
        groups.setdefault(g, []).append({**t, "value": values.get(t["tagCode"])})

    return {
        "system": {
            "code": system.code,
            "name": system.name,
            "kind": system.kind,
            "description": system.description,
            "meta": system.meta or {},
        },
        "ts": _utcnow().isoformat(sep=" ", timespec="seconds"),
        "values": values,
        "groups": groups,
        "tags": [{**t, "value": values.get(t["tagCode"])} for t in tags],
    }


async def get_series(
    db: AsyncSession,
    system_code: str,
    *,
    tag_codes: list[str],
    hours: int = 24,
    limit: int = 500,
) -> dict[str, Any]:
    await _require_system(db, system_code)
    if not tag_codes:
        raise AppError(ErrorCode.VALIDATION, "tags required", status_code=422)
    hours = max(1, min(int(hours or 24), 168))
    lim = max(1, min(int(limit or 500), 5000))
    since = _utcnow() - timedelta(hours=hours)
    codes = tag_codes[:20]

    # 一次查出多 tag，再在内存分组（比循环 N 次查询更稳）
    result = await db.execute(
        select(ProdSample)
        .where(
            ProdSample.system_code == system_code,
            ProdSample.tag_code.in_(codes),
            ProdSample.ts >= since,
        )
        .order_by(ProdSample.tag_code.asc(), ProdSample.ts.asc())
    )
    series: dict[str, list[dict[str, Any]]] = {t: [] for t in codes}
    for s in result.scalars().all():
        bucket = series.get(s.tag_code)
        if bucket is None:
            continue
        if len(bucket) >= lim:
            continue
        bucket.append(
            {
                "ts": s.ts.isoformat(sep=" ", timespec="seconds"),
                "value": s.value_num if s.value_num is not None else s.value_text,
            }
        )
    return {
        "systemCode": system_code,
        "from": since.isoformat(sep=" ", timespec="seconds"),
        "series": series,
    }


def _alarm_item(a: ProdAlarm) -> dict[str, Any]:
    return {
        "id": a.public_id,
        "systemCode": a.system_code,
        "tagCode": a.tag_code,
        "level": a.level,
        "title": a.title,
        "message": a.message,
        "status": a.status,
        "raisedAt": a.raised_at.isoformat(sep=" ", timespec="seconds") if a.raised_at else None,
        "ackedAt": a.acked_at.isoformat(sep=" ", timespec="seconds") if a.acked_at else None,
    }


async def list_alarms(
    db: AsyncSession,
    system_code: str,
    *,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    await _require_system(db, system_code)
    lim = max(1, min(int(limit or 50), 200))
    stmt = (
        select(ProdAlarm)
        .where(ProdAlarm.system_code == system_code)
        .order_by(ProdAlarm.raised_at.desc())
        .limit(lim)
    )
    if status:
        stmt = stmt.where(ProdAlarm.status == status)
    result = await db.execute(stmt)
    return [_alarm_item(a) for a in result.scalars().all()]


async def ack_alarm(db: AsyncSession, *, public_id: str, user_id: int) -> dict[str, Any]:
    result = await db.execute(select(ProdAlarm).where(ProdAlarm.public_id == public_id))
    row = result.scalar_one_or_none()
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, "alarm not found", status_code=404)
    if row.status == "active":
        row.status = "acked"
        row.acked_at = datetime.now(timezone.utc)
        row.acked_by = user_id
        await db.commit()
        await db.refresh(row)
    return _alarm_item(row)


async def _get_writable_tag(db: AsyncSession, system_code: str, tag_code: str) -> ProdTag:
    result = await db.execute(
        select(ProdTag).where(
            ProdTag.system_code == system_code,
            ProdTag.tag_code == tag_code,
            ProdTag.enabled.is_(True),
        )
    )
    tag = result.scalar_one_or_none()
    if tag is None:
        raise AppError(ErrorCode.NOT_FOUND, f"tag not found: {tag_code}", status_code=404)
    if not tag.writable:
        raise AppError(ErrorCode.FORBIDDEN, f"tag not writable: {tag_code}", status_code=403)
    return tag


class ControlExecutor:
    """执行器接口预留：一期用 SimulateExecutor。"""

    name = "base"

    async def execute(
        self,
        db: AsyncSession,
        *,
        system_code: str,
        tag: ProdTag,
        value: float | None,
        text: str | None,
    ) -> tuple[str, str]:
        raise NotImplementedError


class SimulateExecutor(ControlExecutor):
    name = "simulate"

    async def execute(
        self,
        db: AsyncSession,
        *,
        system_code: str,
        tag: ProdTag,
        value: float | None,
        text: str | None,
    ) -> tuple[str, str]:
        now = _utcnow()
        existing = await db.execute(
            select(ProdSample).where(
                ProdSample.system_code == system_code,
                ProdSample.tag_code == tag.tag_code,
                ProdSample.ts == now,
            )
        )
        row = existing.scalar_one_or_none()
        if row is not None:
            row.value_num = value
            row.value_text = text
        else:
            db.add(
                ProdSample(
                    system_code=system_code,
                    tag_code=tag.tag_code,
                    ts=now,
                    value_num=value,
                    value_text=text,
                )
            )
        await db.flush()
        return "simulated", f"模拟执行成功：{tag.tag_code}={value if value is not None else text}"


class PlcExecutor(ControlExecutor):
    """真 PLC / OPC 预留，未配置通道时失败。"""

    name = "plc"

    async def execute(
        self,
        db: AsyncSession,
        *,
        system_code: str,
        tag: ProdTag,
        value: float | None,
        text: str | None,
    ) -> tuple[str, str]:
        _ = db, system_code, tag, value, text
        return "failed", "PLC/OPC 通道未配置（预留接口），请使用模拟执行或接入采集适配器"


def get_executor(mode: str = "simulate") -> ControlExecutor:
    if mode == "plc":
        return PlcExecutor()
    return SimulateExecutor()


async def issue_command(
    db: AsyncSession,
    *,
    system_code: str,
    tag_code: str,
    user_id: int,
    target_value: float | None = None,
    target_text: str | None = None,
    executor_mode: str = "simulate",
) -> dict[str, Any]:
    await _require_system(db, system_code)
    tag = await _get_writable_tag(db, system_code, tag_code)
    if target_value is None and not target_text:
        raise AppError(ErrorCode.VALIDATION, "targetValue required", status_code=422)

    cmd = ProdCommand(
        public_id=short_id(12),
        system_code=system_code,
        tag_code=tag_code,
        action="write",
        target_value=target_value,
        target_text=target_text,
        status="pending",
        executor=executor_mode if executor_mode in ("simulate", "plc") else "simulate",
        requested_by=user_id,
    )
    db.add(cmd)
    await db.flush()

    executor = get_executor(cmd.executor)
    status, msg = await executor.execute(
        db,
        system_code=system_code,
        tag=tag,
        value=target_value,
        text=target_text,
    )
    cmd.status = status
    cmd.result_msg = msg[:512]
    cmd.finished_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(cmd)
    return {
        "id": cmd.public_id,
        "systemCode": cmd.system_code,
        "tagCode": cmd.tag_code,
        "targetValue": cmd.target_value,
        "status": cmd.status,
        "executor": cmd.executor,
        "resultMsg": cmd.result_msg,
        "createdAt": cmd.created_at.isoformat(sep=" ", timespec="seconds") if cmd.created_at else None,
    }


async def list_commands(
    db: AsyncSession,
    system_code: str,
    *,
    limit: int = 30,
) -> list[dict[str, Any]]:
    await _require_system(db, system_code)
    lim = max(1, min(int(limit or 30), 100))
    result = await db.execute(
        select(ProdCommand)
        .where(ProdCommand.system_code == system_code)
        .order_by(ProdCommand.id.desc())
        .limit(lim)
    )
    return [
        {
            "id": c.public_id,
            "tagCode": c.tag_code,
            "targetValue": c.target_value,
            "status": c.status,
            "executor": c.executor,
            "resultMsg": c.result_msg,
            "createdAt": c.created_at.isoformat(sep=" ", timespec="seconds") if c.created_at else None,
        }
        for c in result.scalars().all()
    ]


async def clear_system_data(db: AsyncSession, system_code: str) -> None:
    await db.execute(delete(ProdSample).where(ProdSample.system_code == system_code))
    await db.execute(delete(ProdAlarm).where(ProdAlarm.system_code == system_code))
    await db.execute(delete(ProdCommand).where(ProdCommand.system_code == system_code))
    await db.execute(delete(ProdTag).where(ProdTag.system_code == system_code))
