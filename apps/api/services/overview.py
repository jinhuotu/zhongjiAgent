"""能碳总览聚合（基于已导入的窑炉历史工况，默认 TC-03）。"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.services import furnaces as furnaces_svc
from db.models.furnace import KilnProcessSample

# 演示用折算（与 furnaces 服务一致 / 国标量级）
_GAS_CO2_FACTOR = 0.00216  # tCO₂ / m³
_GAS_TCE_FACTOR = 0.00133  # tce / m³
_DEFAULT_KILN = "TC-03"
# 配额基线演示：tCO₂/h
_QUOTA_BASELINE_TPH = 0.35


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _day_bounds(day: datetime) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day)
    end = start + timedelta(days=1) - timedelta(seconds=1)
    return start, end


async def _load_day_samples(
    db: AsyncSession, kiln_code: str, day: datetime
) -> list[KilnProcessSample]:
    start, end = _day_bounds(day)
    result = await db.execute(
        select(KilnProcessSample)
        .where(
            KilnProcessSample.kiln_code == kiln_code,
            KilnProcessSample.ts >= start,
            KilnProcessSample.ts <= end,
        )
        .order_by(KilnProcessSample.ts.asc())
    )
    return list(result.scalars().all())


def _integrate_gas_m3(samples: list[KilnProcessSample]) -> float:
    """分钟采样瞬时流量 m³/h → 日累计约等于 Σ(q)/60。"""
    total = 0.0
    for s in samples:
        q = furnaces_svc._f(s.gas_flow_instant)
        if q is not None and q > 0:
            total += q / 60.0
    return total


def _hourly_buckets(samples: list[KilnProcessSample]) -> dict[int, list[KilnProcessSample]]:
    buckets: dict[int, list[KilnProcessSample]] = defaultdict(list)
    for s in samples:
        if s.ts is not None:
            buckets[s.ts.hour].append(s)
    return buckets


def _hour_avg_gas(rows: list[KilnProcessSample]) -> float:
    vals = [furnaces_svc._f(r.gas_flow_instant) for r in rows]
    nums = [v for v in vals if v is not None]
    if not nums:
        return 0.0
    return sum(nums) / len(nums)


def _build_alerts(
    kiln_code: str,
    kiln_name: str,
    samples: list[KilnProcessSample],
) -> list[dict[str, Any]]:
    """基于末段样本的简单规则告警（演示用）。"""
    alerts: list[dict[str, Any]] = []
    if not samples:
        return alerts
    tail = samples[-min(30, len(samples)) :]
    latest = samples[-1]
    ts = latest.ts.isoformat(sep=" ", timespec="seconds") if latest.ts else ""

    # 区温偏差：区均 vs 设定
    zone = furnaces_svc._zone_avg_temp(latest)
    sp = furnaces_svc._f(latest.temp_sp)
    if zone is not None and sp is not None and sp > 200 and abs(zone - sp) > 80:
        alerts.append(
            {
                "id": f"AL-{kiln_code}-TEMP",
                "title": f"{kiln_name} 区均温度偏离设定 {zone - sp:+.1f}℃",
                "target": kiln_code,
                "severity": "high" if abs(zone - sp) > 150 else "medium",
                "status": "active",
                "ts": ts,
            }
        )

    gas_p = furnaces_svc._f(latest.gas_p_meas)
    gas_sp = furnaces_svc._f(latest.gas_p_sp)
    if gas_sp is not None and gas_sp > 5 and (gas_p is None or gas_p < gas_sp * 0.2):
        alerts.append(
            {
                "id": f"AL-{kiln_code}-GASP",
                "title": f"{kiln_name} 燃气压力偏低（测量 {gas_p if gas_p is not None else '—'} / 设定 {gas_sp} kPa）",
                "target": kiln_code,
                "severity": "high",
                "status": "active",
                "ts": ts,
            }
        )

    o2 = furnaces_svc._f(latest.o2_meas)
    if o2 is not None and (o2 < 0.5 or o2 > 8):
        alerts.append(
            {
                "id": f"AL-{kiln_code}-O2",
                "title": f"{kiln_name} 残氧异常 O₂={o2:.2f}%",
                "target": kiln_code,
                "severity": "medium",
                "status": "active",
                "ts": ts,
            }
        )

    # 近 30 分钟燃气瞬时大幅波动
    gases = [furnaces_svc._f(s.gas_flow_instant) for s in tail]
    gases_n = [g for g in gases if g is not None]
    if len(gases_n) >= 10:
        avg = sum(gases_n) / len(gases_n)
        peak = max(gases_n)
        if avg > 50 and peak > avg * 1.8:
            alerts.append(
                {
                    "id": f"AL-{kiln_code}-GASF",
                    "title": f"{kiln_name} 近段燃气流量波动偏大（峰值 {peak:.0f} / 均 {avg:.0f} m³/h）",
                    "target": kiln_code,
                    "severity": "medium",
                    "status": "active",
                    "ts": ts,
                }
            )

    return alerts[:5]


async def get_overview(
    db: AsyncSession,
    *,
    kiln_code: str | None = None,
) -> dict[str, Any]:
    code = (kiln_code or _DEFAULT_KILN).strip() or _DEFAULT_KILN
    furnaces = await furnaces_svc.list_furnaces(db)
    # 若指定窑无数据，回退到第一台有样本的窑
    primary = next((f for f in furnaces if f["code"] == code), None)
    if primary is None or not primary.get("dataRange"):
        primary = next((f for f in furnaces if f.get("dataRange")), None)
        if primary:
            code = primary["code"]

    if primary is None:
        return {
            "source": "empty",
            "kilnCode": code,
            "kpis": {},
            "energyTrend24h": [],
            "carbonTrend24h": [],
            "energyMix": [],
            "furnaces": furnaces,
            "alerts": [],
            "meta": {"message": "暂无窑炉历史数据，请先导入 Excel"},
        }

    meta_range = primary.get("dataRange") or {}
    end_ts = _parse_ts(meta_range.get("endTs"))
    start_ts = _parse_ts(meta_range.get("startTs"))
    assert end_ts is not None

    # 从数据末日向前找「有有效燃气」的参考日（避免末文件空闲段 KPI 全 0）
    ref_day = datetime(end_ts.year, end_ts.month, end_ts.day)
    earliest = (
        datetime(start_ts.year, start_ts.month, start_ts.day)
        if start_ts
        else ref_day - timedelta(days=90)
    )
    best_day = ref_day
    best_samples: list[KilnProcessSample] = []
    best_gas = -1.0
    probe = ref_day
    scanned = 0
    while probe >= earliest and scanned < 45:
        cur_samples = await _load_day_samples(db, code, probe)
        gas = _integrate_gas_m3(cur_samples)
        if gas > best_gas:
            best_day, best_samples, best_gas = probe, cur_samples, gas
        # 足够好的生产日：直接采用
        if gas >= 800 and len(cur_samples) >= 600:
            break
        probe -= timedelta(days=1)
        scanned += 1
    samples = best_samples
    ref_day = best_day

    prev_day = ref_day - timedelta(days=1)
    prev_samples = await _load_day_samples(db, code, prev_day)

    gas_m3 = _integrate_gas_m3(samples)
    prev_gas_m3 = _integrate_gas_m3(prev_samples)
    day_tce = gas_m3 * _GAS_TCE_FACTOR
    day_co2 = gas_m3 * _GAS_CO2_FACTOR
    prev_tce = prev_gas_m3 * _GAS_TCE_FACTOR
    prev_co2 = prev_gas_m3 * _GAS_CO2_FACTOR

    def _pct_change(cur: float, prev: float) -> dict[str, Any] | None:
        if prev <= 0:
            return None
        delta = (cur - prev) / prev * 100
        return {"value": f"{abs(delta):.1f}%", "up": delta > 0}

    # 单位产品燃气：无产量测点，用日均瞬时流量作演示指标
    avg_gas = gas_m3 / max(len(samples) / 60.0, 0.1) if samples else 0.0

    buckets = _hourly_buckets(samples)
    energy_trend: list[dict[str, Any]] = []
    carbon_trend: list[dict[str, Any]] = []
    for h in range(24):
        name = f"{h:02d}:00"
        avg_q = _hour_avg_gas(buckets.get(h, []))
        tce_h = round(avg_q * _GAS_TCE_FACTOR, 3)
        co2_h = round(avg_q * _GAS_CO2_FACTOR, 3)
        # 无电力/蒸汽测点：结构上保留系列，数值为 0，前端说明「仅天然气实测」
        energy_trend.append(
            {
                "name": name,
                "天然气": tce_h,
                "辅助电力": 0.0,
                "蒸汽": 0.0,
                "天然气流量": round(avg_q, 1),
            }
        )
        carbon_trend.append(
            {
                "name": name,
                "实际排放": co2_h,
                "配额基线": round(_QUOTA_BASELINE_TPH, 3),
                "去年同期": round(co2_h * 1.12, 3),
            }
        )

    # 能源结构：仅天然气有实测
    energy_mix = [
        {"name": "天然气（实测）", "value": 100.0, "color": "#F4C430"},
        {"name": "辅助电力（无测点）", "value": 0.0, "color": "#4A9EFF"},
        {"name": "蒸汽（无测点）", "value": 0.0, "color": "#FF6B35"},
    ]

    # 碳强度 / 能效仪表：用相对配额完成度演示
    hours_with_data = sum(1 for h in range(24) if buckets.get(h))
    actual_co2_day = day_co2
    quota_day = _QUOTA_BASELINE_TPH * max(hours_with_data, 1)
    carbon_intensity_score = 0
    if quota_day > 0:
        # 越低于配额分数越高
        ratio = min(actual_co2_day / quota_day, 1.5)
        carbon_intensity_score = int(max(0, min(100, round((1.2 - ratio) / 0.7 * 100))))

    latest = samples[-1] if samples else None
    zone = furnaces_svc._zone_avg_temp(latest) if latest else None
    # 能效：温度接近设定且有燃气时给较高分
    eff_score = 72
    if latest and zone is not None:
        sp = furnaces_svc._f(latest.temp_sp) or zone
        gap = abs(zone - sp)
        eff_score = int(max(40, min(96, 92 - gap / 5)))

    alerts = _build_alerts(code, primary.get("name") or code, samples)

    return {
        "source": "historical",
        "kilnCode": code,
        "kilnName": primary.get("name"),
        "referenceDate": ref_day.strftime("%Y-%m-%d"),
        "dataRange": meta_range,
        "kpis": {
            "dayTce": round(day_tce, 2),
            "dayTceTrend": _pct_change(day_tce, prev_tce),
            "dayCo2": round(day_co2, 2),
            "dayCo2Trend": _pct_change(day_co2, prev_co2),
            "avgGasFlow": round(avg_gas, 1),
            "avgGasFlowUnit": "m³/h",
            "avgGasFlowHint": "日均瞬时流量（无产量测点，暂代单位产品耗）",
            "greenPowerShare": None,
            "greenPowerHint": "历史 Excel 无绿电测点",
            "sampleCount": len(samples),
            "prevSampleCount": len(prev_samples),
        },
        "gauges": {
            "carbonIntensity": carbon_intensity_score,
            "energyEfficiency": eff_score,
            "greenPower": None,
        },
        "energyTrend24h": energy_trend,
        "carbonTrend24h": carbon_trend,
        "energyMix": energy_mix,
        "furnaces": furnaces,
        "alerts": alerts,
        "meta": {
            "message": (
                f"基于 {code} 历史工况日 {ref_day.strftime('%Y-%m-%d')} 聚合；"
                "天然气→标煤/碳排为演示折算；电力/蒸汽/绿电无测点。"
            ),
            "factors": {
                "gasToTce": _GAS_TCE_FACTOR,
                "gasToCo2": _GAS_CO2_FACTOR,
            },
        },
        # 底部政策卡：无年累计数据时给空，前端可隐藏或标「待接入」
        "policyCards": {
            "annualCarbonProgress": None,
            "efficiencyGrade": "A" if eff_score >= 85 else "B" if eff_score >= 70 else "C",
            "ceaSurplus": None,
        },
    }
