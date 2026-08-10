"""业务统计报表（能碳日报/周报等，非 AI 报告）。"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from common.errors import AppError, ErrorCode
from db.models.biz_report import BizReport
from db.models.furnace import Furnace, KilnProcessSample
from db.models.production import ProdAlarm
from db.models.user import User

TEMPLATES: list[dict[str, str]] = [
    {
        "key": "daily",
        "name": "能碳日报",
        "desc": "每日 08:00 自动生成，含能耗 / 碳排 / 告警",
        "icon": "日",
        "type": "日报",
    },
    {
        "key": "weekly",
        "name": "能碳周报",
        "desc": "每周一 08:00 自动生成，本周 vs 上周",
        "icon": "周",
        "type": "周报",
    },
    {
        "key": "monthly",
        "name": "能碳月报",
        "desc": "每月 1 日 16:00，含工序能耗对标",
        "icon": "月",
        "type": "月报",
    },
    {
        "key": "yearly",
        "name": "能碳年报",
        "desc": "每年 1 月，含双碳目标完成度",
        "icon": "年",
        "type": "年报",
    },
    {
        "key": "mrv",
        "name": "碳核查报告",
        "desc": "GB/T 32151 第三方核查就绪版",
        "icon": "核",
        "type": "专项",
    },
    {
        "key": "efficiency",
        "name": "能效专项",
        "desc": "GB 21256 对标分析",
        "icon": "效",
        "type": "专项",
    },
]

_TEMPLATE_BY_KEY = {t["key"]: t for t in TEMPLATES}
_TEMPLATE_BY_NAME = {t["name"]: t for t in TEMPLATES}


def short_id(n: int = 12) -> str:
    return secrets.token_hex(n // 2 + n % 2)[:n]


def _size_label(n: int) -> str:
    if n <= 0:
        return "—"
    kb = max(n / 1024.0, 0.1)
    if kb < 1024:
        return f"{kb:.1f} KB"
    return f"{kb / 1024.0:.1f} MB"


def _fmt_dt(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _item(row: BizReport) -> dict[str, Any]:
    return {
        "id": row.public_id,
        "title": row.title,
        "type": row.report_type,
        "period": row.period,
        "size": row.size_label or _size_label(row.char_count),
        "createdBy": row.created_by_name or "—",
        "createdAt": _fmt_dt(row.created_at),
        "status": row.status,
        "templateKey": row.template_key,
    }


def list_templates() -> list[dict[str, str]]:
    return list(TEMPLATES)


def _period_for(template_key: str, now: datetime) -> tuple[str, str]:
    local = now.astimezone() if now.tzinfo else now
    if template_key == "daily":
        day = local.strftime("%Y-%m-%d")
        return day, f"能碳日报 · {day}"
    if template_key == "weekly":
        # ISO week
        iso = local.isocalendar()
        period = f"{iso.year}-W{iso.week:02d}"
        return period, f"能碳周报 · {period}"
    if template_key == "monthly":
        period = local.strftime("%Y-%m")
        return period, f"能碳月报 · {period}"
    if template_key == "yearly":
        period = local.strftime("%Y")
        return period, f"能碳年报 · {period}"
    if template_key == "mrv":
        period = local.strftime("%Y")
        return period, f"碳核查报告 · {period}"
    period = local.strftime("%Y-%m")
    return period, f"能效专项 · {period}"


async def _build_content(db: AsyncSession, *, title: str, period: str) -> str:
    furnaces = list((await db.execute(select(Furnace).where(Furnace.enabled.is_(True)))).scalars())
    furnace_lines = [f"- {f.code} / {f.name}（窑号 {f.kiln_no or '—'}）" for f in furnaces] or [
        "- （暂无启用窑台账，请先导入 furnaces）"
    ]

    sample_count = await db.scalar(select(func.count()).select_from(KilnProcessSample)) or 0
    alarm_active = await db.scalar(
        select(func.count()).select_from(ProdAlarm).where(ProdAlarm.status == "active")
    ) or 0

    latest_ts = await db.scalar(select(func.max(KilnProcessSample.ts)))
    latest_txt = "—"
    if latest_ts:
        latest_txt = (
            latest_ts.strftime("%Y-%m-%d %H:%M")
            if hasattr(latest_ts, "strftime")
            else str(latest_ts)
        )

    # 粗算：近 24h 样本数
    now = datetime.now(timezone.utc)
    day_ago = now - timedelta(hours=24)
    recent = await db.scalar(
        select(func.count())
        .select_from(KilnProcessSample)
        .where(KilnProcessSample.ts >= day_ago.replace(tzinfo=None))
    ) or 0

    return "\n".join(
        [
            f"# {title}",
            "",
            f"- 统计周期：{period}",
            f"- 生成时间：{_fmt_dt(now)}",
            f"- 数据追溯：kiln_process_samples 累计 {int(sample_count)} 条；近 24h {int(recent)} 条",
            f"- 工况最新时刻：{latest_txt}",
            f"- 未处置生产报警：{int(alarm_active)}",
            "",
            "## 窑炉台账",
            *furnace_lines,
            "",
            "## 说明",
            "本报表由平台统计报表模块基于台账与工况样本自动汇总，供内部经营与监管报送参考。",
            "若需 AI 叙事分析，请使用「AI 智能报告」。",
            "",
        ]
    )


async def list_reports(
    db: AsyncSession,
    *,
    report_type: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    lim = max(1, min(int(limit or 50), 200))
    stmt = select(BizReport).order_by(BizReport.created_at.desc()).limit(lim)
    if report_type:
        stmt = stmt.where(BizReport.report_type == report_type)
    if status:
        stmt = stmt.where(BizReport.status == status)
    rows = list((await db.execute(stmt)).scalars().all())

    total = await db.scalar(select(func.count()).select_from(BizReport)) or 0
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month_gen = await db.scalar(
        select(func.count())
        .select_from(BizReport)
        .where(BizReport.created_at >= month_start)
    ) or 0
    pending = await db.scalar(
        select(func.count())
        .select_from(BizReport)
        .where(BizReport.status == "pending_review")
    ) or 0
    ready = await db.scalar(
        select(func.count()).select_from(BizReport).where(BizReport.status == "ready")
    ) or 0
    coverage = round((ready / total) * 100) if total else 0

    return {
        "items": [_item(r) for r in rows],
        "summary": {
            "totalCount": int(total),
            "monthGenerated": int(month_gen),
            "pendingReview": int(pending),
            "automationCoverage": int(coverage),
        },
    }


async def get_report(db: AsyncSession, public_id: str) -> BizReport:
    result = await db.execute(select(BizReport).where(BizReport.public_id == public_id))
    row = result.scalar_one_or_none()
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, "report not found", status_code=404)
    return row


async def get_report_item(db: AsyncSession, public_id: str) -> dict[str, Any]:
    row = await get_report(db, public_id)
    data = _item(row)
    data["content"] = row.content or ""
    return data


async def generate_report(
    db: AsyncSession,
    *,
    user: User,
    template_key: str | None = None,
    template_name: str | None = None,
    report_type: str | None = None,
    period: str | None = None,
) -> dict[str, Any]:
    tpl = None
    if template_key:
        tpl = _TEMPLATE_BY_KEY.get(template_key)
    elif template_name:
        tpl = _TEMPLATE_BY_NAME.get(template_name)
    elif report_type:
        tpl = next((t for t in TEMPLATES if t["type"] == report_type), None)
    if tpl is None:
        tpl = _TEMPLATE_BY_KEY["daily"]

    now = datetime.now(timezone.utc)
    auto_period, title = _period_for(tpl["key"], now)
    use_period = (period or "").strip() or auto_period

    row = BizReport(
        public_id=short_id(12),
        title=title if not period else f"{tpl['name']} · {use_period}",
        report_type=tpl["type"],
        period=use_period,
        status="generating",
        created_by=user.id,
        created_by_name=user.display_name or user.username,
        template_key=tpl["key"],
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)

    try:
        content = await _build_content(db, title=row.title, period=row.period)
        row.content = content
        row.char_count = len(content)
        row.size_label = _size_label(row.char_count)
        row.status = "ready"
        row.error_msg = None
    except Exception as exc:  # noqa: BLE001
        row.status = "failed"
        row.error_msg = str(exc)[:500]
    await db.commit()
    await db.refresh(row)
    if row.status == "failed":
        raise AppError(ErrorCode.INTERNAL, row.error_msg or "generate failed", status_code=500)
    return _item(row)
