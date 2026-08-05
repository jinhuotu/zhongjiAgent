"""AI 智能报告：上下文组装、提示词、历史 CRUD。"""

from __future__ import annotations

import secrets
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.services import furnaces as furnace_svc
from api.services.knowledge.ingest import search_chunks
from common.errors import AppError, ErrorCode
from db.models.ai_report import AiReport

ReportType = Literal["fault", "forecast", "efficiency", "carbon"]

REPORT_TYPES: dict[str, dict[str, str]] = {
    "fault": {
        "name": "烧成故障分析报告",
        "desc": "基于近 24h 工艺曲线 + 知识库故障树，定位异常根因，给出干预建议",
        "rag": "故障 异常 残氧 空燃比 炉压 处置",
    },
    "forecast": {
        "name": "烧成温度趋势研判报告（30 分钟）",
        "desc": "基于近时窗曲线做趋势研判与超限风险提示（非专用预测模型时须标注假设）",
        "rag": "温度趋势 升温速率 保温 工艺曲线",
    },
    "efficiency": {
        "name": "设备能效分析报告",
        "desc": "对标 GB 21369-2008 单位产品燃气耗 / 热效率，识别能效短板",
        "rag": "热效率 单位燃气耗 GB 21369 空燃比 残氧",
    },
    "carbon": {
        "name": "设备能碳分析报告",
        "desc": "范围一燃气排放粗算 + 碳强度提示 + 降碳路径建议",
        "rag": "碳排核算 排放因子 GB/T 32151 天然气",
    },
}

SYSTEM_BASE = """你是「炉境 LuJing」工业燃气车式窑领域的资深热处理与能碳专家。
你正在为客户编写一份**生产可用**的工业级报告，要求：
1. 完全围绕"工业燃气车式窑（车底炉）"场景；
2. 引用 GB 21369-2008 / GB/T 17358-2009 / GB/T 32151 / GB 13271 等标准时务必带标准号；
3. 输出格式：**严格使用 Markdown**，包含一级标题、要点列表、表格、关键结论加粗；
4. 数值要给出单位（℃ / Nm³/h / kgCE/t / kgCO₂/t / λ / %）；
5. 当数据不足时明确写出"⚠️ 假设条件"或"⚠️ 数据缺失"，不要编造精确数字；
6. 全文中文，长度 800~1500 字。"""

TEMPLATES: dict[str, str] = {
    "fault": """请输出《车式窑烧成故障分析报告》，包含：
# 一、报告摘要（异常等级 / 风险窗口 / 建议响应时长）
# 二、关键参数实测对标（表格：参数 / 实测 / 标准范围 / 偏差 / 评估）
# 三、故障树根因定位（按"现象 → 中间环节 → 根因"分级，结合知识库标准）
# 四、干预与处置建议（分立即 / 24h 内 / 一周内三档）
# 五、预期收益（修复后单耗 / 残氧 / 故障率改善量化；缺数据则给方向性估计并标注假设）""",
    "forecast": """请输出《车式窑烧成温度趋势研判报告（未来约 30 分钟）》，包含：
# 一、研判摘要（趋势方向 / 关键时点估计 / 置信度说明）
# 二、关键驱动因子（升温速率 / 空燃比 / 炉压 / 分区温度 / 燃气流量，结合近时窗特征）
# 三、风险评估（是否会超过工艺红线 / 是否会跌破保温下限）
# 四、干预建议（烧嘴负荷调节、阀位整定、装载策略）
# 五、模型假设与局限（明确：当前基于历史曲线趋势外推，非专用预测模型点位）""",
    "efficiency": """请输出《车式窑设备能效分析报告》，对标 GB 21369-2008 与 GB/T 17358-2009：
# 一、能效画像（热效率 / 燃气流量 / 残氧 / 空燃比设定 / 分区温度概况）
# 二、对标分析（与 GB 21369 准入 / 限定 / 先进值对比；缺产量数据时只做方向性对标并标注）
# 三、能效短板（结合空燃比 λ / 残氧 O₂ / 温压流量，识别最多 3 项短板）
# 四、节能改造建议（蓄热式烧嘴 / 烟气余热回收 / 密封 / AI 寻优，给定性回收期）
# 五、阶段目标（30 / 90 / 365 天能效提升路径）""",
    "carbon": """请输出《车式窑设备能碳分析报告》，对标 GB/T 32151：
# 一、范围一排放粗算（以天然气流量为主；因子来源须写明；缺产量则不做 kgCO₂/t）
# 二、碳强度提示（有产量则算；无产量则说明数据缺口）
# 三、降碳路径（燃料结构 / 余热 / 工艺优化 / 绿电（若无电量数据则标注缺失））
# 四、合规与披露建议（简要）
# 五、数据完善清单（下一步应补齐的台账字段）""",
}


def short_id(n: int = 12) -> str:
    return secrets.token_hex(n // 2 + n % 2)[:n]


def to_item(row: AiReport, *, include_content: bool = False) -> dict[str, Any]:
    meta = REPORT_TYPES.get(row.report_type, {})
    item: dict[str, Any] = {
        "id": row.public_id,
        "type": row.report_type,
        "typeName": meta.get("name") or row.report_type,
        "title": row.title,
        "furnaceId": row.kiln_code,
        "furnaceName": row.kiln_name,
        "mode": row.mode,
        "status": row.status,
        "charCount": row.char_count,
        "size": f"{(row.char_count / 1000):.1f} K 字" if row.char_count else "—",
        "refsCount": len(row.refs or []),
        "errorMsg": row.error_msg,
        "createdAt": int(row.created_at.timestamp() * 1000) if row.created_at else 0,
        "updatedAt": int(row.updated_at.timestamp() * 1000) if row.updated_at else 0,
    }
    if include_content:
        item["content"] = row.content or ""
        item["refs"] = row.refs or []
        item["contextSummary"] = row.context_summary
    return item


def list_report_types() -> list[dict[str, str]]:
    return [
        {"id": k, "name": v["name"], "desc": v["desc"]}
        for k, v in REPORT_TYPES.items()
    ]


def _fmt(v: Any, unit: str = "") -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        text = f"{v:.2f}".rstrip("0").rstrip(".")
    else:
        text = str(v)
    return f"{text}{unit}" if unit else text


def _series_stats(points: list[dict[str, Any]]) -> dict[str, Any]:
    if not points:
        return {}

    def col(key: str) -> list[float]:
        out: list[float] = []
        for p in points:
            v = p.get(key)
            if isinstance(v, (int, float)):
                out.append(float(v))
        return out

    def agg(vals: list[float]) -> dict[str, float] | None:
        if not vals:
            return None
        return {
            "min": round(min(vals), 2),
            "max": round(max(vals), 2),
            "avg": round(sum(vals) / len(vals), 2),
            "last": round(vals[-1], 2),
        }

    return {
        "pointCount": len(points),
        "from": points[0].get("ts"),
        "to": points[-1].get("ts"),
        "zoneAvgTemp": agg(col("zoneAvgTemp")),
        "gasFlowInstant": agg(col("gasFlowInstant")),
        "o2Meas": agg(col("o2Meas")),
        "furnacePMeas": agg(col("furnacePMeas")),
        "afrSp": agg(col("afrSp")),
        "co2Hourly": agg(col("co2Hourly")),
    }


async def build_context(
    db: AsyncSession,
    *,
    kiln_code: str,
) -> dict[str, Any]:
    """组装窑台账 + 最新快照 + 近 24h 抽样统计。"""
    try:
        furnace = await furnace_svc.get_furnace(db, kiln_code)
    except AppError:
        raise AppError(
            ErrorCode.NOT_FOUND,
            f"窑炉不存在或未启用：{kiln_code}。请先在台账中配置并导入工况数据。",
            status_code=404,
        ) from None

    snap = furnace.get("snapshot") or {}
    data_range = furnace.get("dataRange")
    series = await furnace_svc.get_series(
        db,
        kiln_code,
        step_minutes=5,
        limit=400,
    )
    stats = _series_stats(series.get("points") or [])

    lines = [
        f"【设备】{furnace.get('code')} {furnace.get('name')}（车间：{furnace.get('workshop') or '—'}）",
        f"【规格】{furnace.get('capacity') or '—'} · 类型 {furnace.get('type') or '—'} · 窑号 {furnace.get('kilnNo') or '—'}",
        f"【状态】{furnace.get('status') or '—'} · 最新时刻 {furnace.get('latestTs') or '—'}",
        (
            "【最新快照】"
            f"区均温 {_fmt(snap.get('zoneAvgTemp'), '℃')} · "
            f"设定温 {_fmt(snap.get('tempSp'), '℃')} · "
            f"炉压 {_fmt(snap.get('furnacePMeas'), 'Pa')} · "
            f"天然气 {_fmt(snap.get('gasFlowInstant'), 'Nm³/h')} · "
            f"残氧 O₂ {_fmt(snap.get('o2Meas'), '%')} · "
            f"空燃比设定 λ {_fmt(snap.get('afrSp'))} · "
            f"助燃风 {_fmt(snap.get('airFlowInstant'), 'Nm³/h')} · "
            f"粗算 CO₂ {_fmt(snap.get('co2Hourly'), ' t/h')}"
        ),
    ]
    if data_range:
        lines.append(
            "【数据覆盖】"
            f"{data_range.get('startTs')} ~ {data_range.get('endTs')} · "
            f"样本 {data_range.get('sampleCount')} 条"
        )
    else:
        lines.append("【数据覆盖】⚠️ 尚无工艺样本，报告将主要依赖知识库与通用经验")

    if stats:
        lines.append(
            "【近窗统计（默认数据末尾约 24h，5min 抽稀）】"
            f"点数 {stats.get('pointCount')} · "
            f"区间 {stats.get('from')} ~ {stats.get('to')}"
        )
        for key, label, unit in (
            ("zoneAvgTemp", "区均温", "℃"),
            ("gasFlowInstant", "燃气瞬时", "Nm³/h"),
            ("o2Meas", "残氧", "%"),
            ("furnacePMeas", "炉压", "Pa"),
            ("afrSp", "空燃比设定", ""),
            ("co2Hourly", "粗算CO₂", "t/h"),
        ):
            a = stats.get(key)
            if not a:
                continue
            lines.append(
                f"  - {label}: min={_fmt(a['min'], unit)} / "
                f"avg={_fmt(a['avg'], unit)} / max={_fmt(a['max'], unit)} / "
                f"last={_fmt(a['last'], unit)}"
            )
    else:
        lines.append("【近窗统计】⚠️ 无可用曲线点")

    lines.append(
        "【说明】产量、电量、绿电、精确热效率等字段当前可能缺失；"
        "排放因子为演示粗算，正式核算请替换厂内因子。"
    )

    text = "\n".join(lines)
    return {
        "furnace": furnace,
        "contextText": text,
        "hasSamples": bool(data_range),
        "stats": stats,
    }


def build_prompt(
    *,
    report_type: str,
    context_text: str,
    chunks: list[dict[str, Any]],
) -> list[dict[str, str]]:
    if report_type not in TEMPLATES:
        raise AppError(ErrorCode.VALIDATION, "invalid report type", status_code=422)

    refs = (
        "\n\n---\n\n".join(
            f"[#{i + 1} sim={float(c.get('score') or 0):.3f}] {c.get('content')}"
            for i, c in enumerate(chunks[:5])
        )
        if chunks
        else "（本次未检索到匹配的知识库片段，请基于工业窑炉通用工程经验作答，并在文末加注「⚠️ 部分结论非来自知识库」）"
    )
    user = (
        f"{context_text}\n\n"
        f"【知识库参考片段】\n{refs}\n\n"
        f"【生成指令】\n{TEMPLATES[report_type]}"
    )
    return [
        {"role": "system", "content": SYSTEM_BASE},
        {"role": "user", "content": user},
    ]


async def search_report_chunks(
    db: AsyncSession,
    *,
    report_type: str,
    kiln_name: str,
) -> list[dict[str, Any]]:
    meta = REPORT_TYPES.get(report_type) or {}
    query = f"{kiln_name} {meta.get('rag') or report_type}"
    try:
        return await search_chunks(db, query=query, top_k=5, min_score=0.0)
    except Exception:  # noqa: BLE001
        return []


async def create_draft(
    db: AsyncSession,
    *,
    user_id: int,
    report_type: str,
    kiln_code: str,
    kiln_name: str | None,
    mode: str,
    title: str,
    context_summary: str | None,
) -> AiReport:
    row = AiReport(
        public_id=short_id(12),
        user_id=user_id,
        report_type=report_type,
        title=title[:256],
        kiln_code=kiln_code,
        kiln_name=(kiln_name or "")[:128] or None,
        mode=mode,
        status="generating",
        content=None,
        char_count=0,
        context_summary=(context_summary or "")[:8000] or None,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def finalize_success(
    db: AsyncSession,
    *,
    report: AiReport,
    content: str,
    refs: list[dict[str, Any]] | None,
) -> AiReport:
    report.content = content
    report.char_count = len(content)
    report.refs = refs or []
    report.status = "done"
    report.error_msg = None
    await db.commit()
    await db.refresh(report)
    return report


async def finalize_error(
    db: AsyncSession,
    *,
    report: AiReport,
    error_msg: str,
    content: str = "",
) -> AiReport:
    report.content = content or None
    report.char_count = len(content or "")
    report.status = "failed"
    report.error_msg = (error_msg or "generate failed")[:512]
    await db.commit()
    await db.refresh(report)
    return report


async def list_reports(
    db: AsyncSession,
    *,
    user_id: int,
    report_type: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    lim = max(1, min(int(limit or 50), 100))
    stmt = (
        select(AiReport)
        .where(AiReport.user_id == user_id)
        .order_by(AiReport.id.desc())
        .limit(lim)
    )
    if report_type:
        stmt = stmt.where(AiReport.report_type == report_type)
    result = await db.execute(stmt)
    return [to_item(r) for r in result.scalars().all()]


async def get_report(
    db: AsyncSession,
    *,
    public_id: str,
    user_id: int,
) -> dict[str, Any]:
    result = await db.execute(
        select(AiReport).where(
            AiReport.public_id == public_id,
            AiReport.user_id == user_id,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, "report not found", status_code=404)
    return to_item(row, include_content=True)


async def delete_report(
    db: AsyncSession,
    *,
    public_id: str,
    user_id: int,
) -> None:
    result = await db.execute(
        select(AiReport).where(
            AiReport.public_id == public_id,
            AiReport.user_id == user_id,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, "report not found", status_code=404)
    await db.delete(row)
    await db.commit()
