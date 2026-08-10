"""告警中心：聚合生产报警 + 规则配置。"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from common.errors import AppError, ErrorCode
from db.models.alert import AlertRule
from db.models.production import ProdAlarm, ProdSystem
from db.models.user import User

_LEVEL_TO_SEV = {"alarm": "high", "warning": "medium", "info": "low"}
_SEV_TO_LEVEL = {"high": "alarm", "medium": "warning", "low": "info"}
_STATUS_OUT = {"active": "active", "acked": "ack", "closed": "closed"}
_STATUS_IN = {"active": "active", "ack": "acked", "closed": "closed"}

_TYPE_COLORS = {
    "能耗超标": "#FF6B35",
    "碳排异常": "#F4C430",
    "设备故障": "#4A9EFF",
    "工艺偏差": "#5DD3E0",
    "安全/环保": "#7FB069",
}

_DEFAULT_RULES: list[dict[str, Any]] = [
    {
        "name": "单位产品燃气耗超限",
        "category": "能耗",
        "expression": "avg(单耗,5m) > limit × 1.02",
        "threshold": ">168 m³/t",
        "channels": "钉钉 + 短信",
        "devices": "全部车式窑",
        "enabled": True,
    },
    {
        "name": "炉膛温度场偏差",
        "category": "工艺",
        "expression": "ΔT(前后区) > 25℃",
        "threshold": ">25℃",
        "channels": "电话 + 钉钉",
        "devices": "全部车式窑",
        "enabled": True,
    },
    {
        "name": "空燃比 / 残氧异常",
        "category": "工艺",
        "expression": "O₂ > 4.5% 且 λ > 1.15",
        "threshold": ">4.5%",
        "channels": "钉钉",
        "devices": "正火/调质窑",
        "enabled": True,
    },
    {
        "name": "升温速率超工艺曲线",
        "category": "工艺",
        "expression": "dT/dt > 80℃/h",
        "threshold": ">80℃/h",
        "channels": "企业微信",
        "devices": "风电/核电件窑",
        "enabled": True,
    },
    {
        "name": "烟气余热回收效率下降",
        "category": "能效",
        "expression": "η-烟气 < 62%",
        "threshold": "<62%",
        "channels": "钉钉 + 邮件",
        "devices": "蓄热式车式窑",
        "enabled": True,
    },
    {
        "name": "碳排基线超出",
        "category": "碳排",
        "expression": "CO₂(h) > baseline × 1.05",
        "threshold": "+5%",
        "channels": "钉钉 + 邮件",
        "devices": "全部",
        "enabled": True,
    },
    {
        "name": "台车密封气幕压力偏低",
        "category": "本体",
        "expression": "P-气幕 < 80 Pa",
        "threshold": "<80 Pa",
        "channels": "企业微信",
        "devices": "全部车式窑",
        "enabled": False,
    },
]


def short_id(n: int = 12) -> str:
    return secrets.token_hex(n // 2 + n % 2)[:n]


def _fmt_dt(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _infer_type(title: str, meta: dict | None) -> str:
    if meta and meta.get("type"):
        return str(meta["type"])
    t = title or ""
    if any(k in t for k in ("碳", "CO2", "CO₂")):
        return "碳排异常"
    if any(k in t for k in ("能耗", "燃气", "电耗", "流量")):
        return "能耗超标"
    if any(k in t for k in ("故障", "停机", "通讯", "断线")):
        return "设备故障"
    if any(k in t for k in ("安全", "环保", "排放")):
        return "安全/环保"
    return "工艺偏差"


def _alarm_to_item(
    a: ProdAlarm,
    *,
    system_name: str | None,
    owner_name: str | None,
) -> dict[str, Any]:
    meta = a.meta if isinstance(a.meta, dict) else {}
    return {
        "id": a.public_id,
        "severity": _LEVEL_TO_SEV.get(a.level, "medium"),
        "target": system_name or a.system_code,
        "title": a.title,
        "rule": meta.get("rule") or a.tag_code or "—",
        "occurred": _fmt_dt(a.raised_at),
        "status": _STATUS_OUT.get(a.status, a.status),
        "owner": owner_name or meta.get("owner") or "—",
        "type": _infer_type(a.title, meta),
        "source": meta.get("source") or "prod",
        "message": a.message,
        "systemCode": a.system_code,
        "tagCode": a.tag_code,
    }


async def ensure_default_rules(db: AsyncSession) -> None:
    count = await db.scalar(select(func.count()).select_from(AlertRule))
    if count and count > 0:
        return
    for item in _DEFAULT_RULES:
        db.add(
            AlertRule(
                public_id=short_id(12),
                name=item["name"],
                category=item["category"],
                expression=item["expression"],
                threshold=item.get("threshold"),
                channels=item.get("channels"),
                devices=item.get("devices"),
                enabled=bool(item.get("enabled", True)),
            )
        )
    await db.commit()


async def list_rules(db: AsyncSession) -> list[dict[str, Any]]:
    await ensure_default_rules(db)
    result = await db.execute(select(AlertRule).order_by(AlertRule.id.asc()))
    rows = list(result.scalars().all())
    return [
        {
            "id": r.public_id,
            "name": r.name,
            "type": r.category,
            "expression": r.expression,
            "threshold": r.threshold or "—",
            "channels": r.channels or "—",
            "devices": r.devices or "—",
            "enabled": bool(r.enabled),
        }
        for r in rows
    ]


async def list_alerts(
    db: AsyncSession,
    *,
    status: str | None = None,
    severity: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    lim = max(1, min(int(limit or 50), 200))
    stmt = select(ProdAlarm).order_by(ProdAlarm.raised_at.desc()).limit(lim)
    if status:
        db_status = _STATUS_IN.get(status, status)
        stmt = stmt.where(ProdAlarm.status == db_status)
    if severity:
        level = _SEV_TO_LEVEL.get(severity, severity)
        stmt = stmt.where(ProdAlarm.level == level)

    result = await db.execute(stmt)
    alarms = list(result.scalars().all())

    sys_codes = {a.system_code for a in alarms}
    sys_map: dict[str, str] = {}
    if sys_codes:
        sres = await db.execute(select(ProdSystem).where(ProdSystem.code.in_(sys_codes)))
        for s in sres.scalars().all():
            sys_map[s.code] = s.name

    owner_ids = {a.acked_by for a in alarms if a.acked_by}
    owner_map: dict[int, str] = {}
    if owner_ids:
        ures = await db.execute(select(User).where(User.id.in_(owner_ids)))
        for u in ures.scalars().all():
            owner_map[u.id] = u.display_name or u.username

    items = [
        _alarm_to_item(
            a,
            system_name=sys_map.get(a.system_code),
            owner_name=owner_map.get(a.acked_by) if a.acked_by else None,
        )
        for a in alarms
    ]

    # summary（全量统计，不受 limit 筛选中 severity 以外的影响尽量简单）
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    monthly = await db.scalar(
        select(func.count())
        .select_from(ProdAlarm)
        .where(ProdAlarm.raised_at >= month_start)
    )
    high_count = await db.scalar(
        select(func.count())
        .select_from(ProdAlarm)
        .where(ProdAlarm.level == "alarm", ProdAlarm.status != "closed")
    )
    active_count = await db.scalar(
        select(func.count()).select_from(ProdAlarm).where(ProdAlarm.status == "active")
    )
    ack_count = await db.scalar(
        select(func.count()).select_from(ProdAlarm).where(ProdAlarm.status == "acked")
    )

    # 平均响应 / MTTR（有 ack/close 时间的样本）
    ack_rows = await db.execute(
        select(ProdAlarm.raised_at, ProdAlarm.acked_at).where(
            ProdAlarm.acked_at.is_not(None),
            ProdAlarm.raised_at >= now - timedelta(days=90),
        )
    )
    resp_mins: list[float] = []
    for raised, acked in ack_rows.all():
        if raised and acked:
            resp_mins.append(max(0.0, (acked - raised).total_seconds() / 60.0))
    avg_resp = round(sum(resp_mins) / len(resp_mins), 1) if resp_mins else 0.0

    close_rows = await db.execute(
        select(ProdAlarm.raised_at, ProdAlarm.closed_at).where(
            ProdAlarm.closed_at.is_not(None),
            ProdAlarm.raised_at >= now - timedelta(days=90),
        )
    )
    mttr_mins: list[float] = []
    for raised, closed in close_rows.all():
        if raised and closed:
            mttr_mins.append(max(0.0, (closed - raised).total_seconds() / 60.0))
    mttr = round(sum(mttr_mins) / len(mttr_mins), 1) if mttr_mins else 0.0

    type_counter: dict[str, int] = {k: 0 for k in _TYPE_COLORS}
    for it in items:
        t = it.get("type") or "工艺偏差"
        type_counter[t] = type_counter.get(t, 0) + 1
    type_dist = [
        {"name": name, "value": type_counter.get(name, 0), "color": color}
        for name, color in _TYPE_COLORS.items()
    ]

    return {
        "items": items,
        "summary": {
            "monthlyTriggered": int(monthly or 0),
            "highCount": int(high_count or 0),
            "avgResponseMinutes": avg_resp,
            "mttrMinutes": mttr,
            "activeCount": int(active_count or 0),
            "ackCount": int(ack_count or 0),
            "typeDist": type_dist,
        },
    }


async def ack_alert(db: AsyncSession, *, public_id: str, user_id: int) -> dict[str, Any]:
    result = await db.execute(select(ProdAlarm).where(ProdAlarm.public_id == public_id))
    row = result.scalar_one_or_none()
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, "alert not found", status_code=404)
    if row.status == "active":
        row.status = "acked"
        row.acked_at = datetime.now(timezone.utc)
        row.acked_by = user_id
        await db.commit()
        await db.refresh(row)

    sys_name = None
    sres = await db.execute(select(ProdSystem).where(ProdSystem.code == row.system_code))
    sys = sres.scalar_one_or_none()
    if sys:
        sys_name = sys.name
    owner_name = None
    if row.acked_by:
        ures = await db.execute(select(User).where(User.id == row.acked_by))
        u = ures.scalar_one_or_none()
        if u:
            owner_name = u.display_name or u.username
    return _alarm_to_item(row, system_name=sys_name, owner_name=owner_name)


async def close_alert(db: AsyncSession, *, public_id: str, user_id: int) -> dict[str, Any]:
    result = await db.execute(select(ProdAlarm).where(ProdAlarm.public_id == public_id))
    row = result.scalar_one_or_none()
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, "alert not found", status_code=404)
    now = datetime.now(timezone.utc)
    if row.status == "active":
        row.status = "acked"
        row.acked_at = now
        row.acked_by = user_id
    if row.status != "closed":
        row.status = "closed"
        row.closed_at = now
        await db.commit()
        await db.refresh(row)

    sys_name = None
    sres = await db.execute(select(ProdSystem).where(ProdSystem.code == row.system_code))
    sys = sres.scalar_one_or_none()
    if sys:
        sys_name = sys.name
    owner_name = None
    if row.acked_by:
        ures = await db.execute(select(User).where(User.id == row.acked_by))
        u = ures.scalar_one_or_none()
        if u:
            owner_name = u.display_name or u.username
    return _alarm_to_item(row, system_name=sys_name, owner_name=owner_name)
