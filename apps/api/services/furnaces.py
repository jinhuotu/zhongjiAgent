"""车式窑台账 + 历史工况查询（只读）。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from common.errors import AppError, ErrorCode
from db.models.furnace import Furnace, KilnProcessSample

# 天然气粗算排放因子 tCO₂ / (m³·h) 演示用
_GAS_CO2_FACTOR = 0.00216


def _f(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _zone_avg_temp(row: KilnProcessSample) -> float | None:
    vals = [
        _f(row.z1_temp),
        _f(row.z2_temp),
        _f(row.z3_temp),
        _f(row.z4_temp),
        _f(row.z5_temp),
        _f(row.z6_temp),
    ]
    nums = [v for v in vals if v is not None]
    if not nums:
        return None
    return round(sum(nums) / len(nums), 2)


def _derive_status(sample: KilnProcessSample | None) -> str:
    if sample is None:
        return "idle"
    temp = _zone_avg_temp(sample)
    gas = _f(sample.gas_flow_instant) or 0.0
    if temp is not None and temp >= 200 and gas > 1:
        return "running"
    if temp is not None and temp >= 80:
        return "idle"
    return "idle"


def sample_to_dict(row: KilnProcessSample) -> dict[str, Any]:
    zone_avg = _zone_avg_temp(row)
    gas = _f(row.gas_flow_instant)
    return {
        "ts": row.ts.isoformat(sep=" ", timespec="seconds") if row.ts else None,
        "tempSp": _f(row.temp_sp),
        "afrSp": _f(row.afr_sp),
        "zoneAvgTemp": zone_avg,
        "z1Temp": _f(row.z1_temp),
        "z1GasFlow": _f(row.z1_gas_flow),
        "z1AirFlow": _f(row.z1_air_flow),
        "z1AirValve": _f(row.z1_air_valve),
        "z2Temp": _f(row.z2_temp),
        "z2GasFlow": _f(row.z2_gas_flow),
        "z2AirFlow": _f(row.z2_air_flow),
        "z2AirValve": _f(row.z2_air_valve),
        "z3Temp": _f(row.z3_temp),
        "z3GasFlow": _f(row.z3_gas_flow),
        "z3AirFlow": _f(row.z3_air_flow),
        "z3AirValve": _f(row.z3_air_valve),
        "z4Temp": _f(row.z4_temp),
        "z4GasFlow": _f(row.z4_gas_flow),
        "z4AirFlow": _f(row.z4_air_flow),
        "z4AirValve": _f(row.z4_air_valve),
        "z5Temp": _f(row.z5_temp),
        "z5GasFlow": _f(row.z5_gas_flow),
        "z5AirFlow": _f(row.z5_air_flow),
        "z5AirValve": _f(row.z5_air_valve),
        "z6Temp": _f(row.z6_temp),
        "z6GasFlow": _f(row.z6_gas_flow),
        "z6AirFlow": _f(row.z6_air_flow),
        "z6AirValve": _f(row.z6_air_valve),
        "furnacePSp": _f(row.furnace_p_sp),
        "furnacePMeas": _f(row.furnace_p_meas),
        "furnacePOut": _f(row.furnace_p_out),
        "gasPSp": _f(row.gas_p_sp),
        "gasPMeas": _f(row.gas_p_meas),
        "gasPOut": _f(row.gas_p_out),
        "airPSp": _f(row.air_p_sp),
        "airPMeas": _f(row.air_p_meas),
        "airPOut": _f(row.air_p_out),
        "gasFlowInstant": gas,
        "gasFlowTotal": _f(row.gas_flow_total),
        "airFlowInstant": _f(row.air_flow_instant),
        "airFlowTotal": _f(row.air_flow_total),
        "o2Sp": _f(row.o2_sp),
        "o2Meas": _f(row.o2_meas),
        "co2Hourly": round(gas * _GAS_CO2_FACTOR, 3) if gas is not None else None,
        "sourceFile": row.source_file,
        "batchNo": row.batch_no,
        "zoneCount": row.zone_count,
    }


def furnace_to_item(
    furnace: Furnace,
    *,
    latest: KilnProcessSample | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    snap = sample_to_dict(latest) if latest else None
    status = furnace.status or "idle"
    if latest is not None:
        status = _derive_status(latest)
    return {
        "id": furnace.code,
        "code": furnace.code,
        "name": furnace.name,
        "kilnNo": furnace.kiln_no,
        "type": furnace.type,
        "workshop": furnace.workshop,
        "capacity": furnace.capacity,
        "status": status,
        "remark": furnace.remark,
        "enabled": bool(furnace.enabled),
        "temperature": snap["zoneAvgTemp"] if snap else None,
        "gas": snap["gasFlowInstant"] if snap else None,
        "afr": snap["afrSp"] if snap else None,
        "o2": snap["o2Meas"] if snap else None,
        "furnacePressure": snap["furnacePMeas"] if snap else None,
        "tempSp": snap["tempSp"] if snap else None,
        "co2Hourly": snap["co2Hourly"] if snap else None,
        "power": 0,
        "eff": None,
        "operator": "——",
        "runHours": 0,
        "latestTs": snap["ts"] if snap else None,
        "snapshot": snap,
        "dataRange": meta,
    }


async def _get_furnace(db: AsyncSession, code: str) -> Furnace:
    result = await db.execute(select(Furnace).where(Furnace.code == code))
    row = result.scalar_one_or_none()
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, f"furnace not found: {code}", status_code=404)
    return row


async def data_range(
    db: AsyncSession, kiln_code: str, *, with_count: bool = True
) -> dict[str, Any] | None:
    if with_count:
        result = await db.execute(
            select(
                func.min(KilnProcessSample.ts),
                func.max(KilnProcessSample.ts),
                func.count(KilnProcessSample.id),
            ).where(KilnProcessSample.kiln_code == kiln_code)
        )
        mn, mx, cnt = result.one()
        if not cnt:
            return None
        return {
            "startTs": mn.isoformat(sep=" ", timespec="seconds") if mn else None,
            "endTs": mx.isoformat(sep=" ", timespec="seconds") if mx else None,
            "sampleCount": int(cnt),
        }

    # 轻量：仅 min/max，避免大表 COUNT(*)
    result = await db.execute(
        select(
            func.min(KilnProcessSample.ts),
            func.max(KilnProcessSample.ts),
        ).where(KilnProcessSample.kiln_code == kiln_code)
    )
    mn, mx = result.one()
    if mn is None and mx is None:
        return None
    return {
        "startTs": mn.isoformat(sep=" ", timespec="seconds") if mn else None,
        "endTs": mx.isoformat(sep=" ", timespec="seconds") if mx else None,
        "sampleCount": -1,
    }


async def latest_sample(db: AsyncSession, kiln_code: str) -> KilnProcessSample | None:
    result = await db.execute(
        select(KilnProcessSample)
        .where(KilnProcessSample.kiln_code == kiln_code)
        .order_by(KilnProcessSample.ts.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def sample_at_or_before(
    db: AsyncSession, kiln_code: str, at: datetime
) -> KilnProcessSample | None:
    result = await db.execute(
        select(KilnProcessSample)
        .where(
            KilnProcessSample.kiln_code == kiln_code,
            KilnProcessSample.ts <= at,
        )
        .order_by(KilnProcessSample.ts.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def list_furnaces(db: AsyncSession, *, lite: bool = False) -> list[dict[str, Any]]:
    """窑列表。

    lite=True：用于下拉选择等场景——只取最新快照，不做 COUNT，并批量取最新样本。
    """
    result = await db.execute(
        select(Furnace).where(Furnace.enabled.is_(True)).order_by(Furnace.code)
    )
    furnaces = list(result.scalars().all())
    if not furnaces:
        return []

    codes = [f.code for f in furnaces]
    latest_map: dict[str, KilnProcessSample] = {}

    # 每窑最新样本：一次 GROUP BY + JOIN，避免 N 次查询
    max_ts_sub = (
        select(
            KilnProcessSample.kiln_code.label("kiln_code"),
            func.max(KilnProcessSample.ts).label("max_ts"),
        )
        .where(KilnProcessSample.kiln_code.in_(codes))
        .group_by(KilnProcessSample.kiln_code)
        .subquery()
    )
    latest_rows = await db.execute(
        select(KilnProcessSample).join(
            max_ts_sub,
            (KilnProcessSample.kiln_code == max_ts_sub.c.kiln_code)
            & (KilnProcessSample.ts == max_ts_sub.c.max_ts),
        )
    )
    for row in latest_rows.scalars().all():
        latest_map[row.kiln_code] = row

    items: list[dict[str, Any]] = []
    for f in furnaces:
        latest = latest_map.get(f.code)
        meta = None
        if not lite:
            meta = await data_range(db, f.code, with_count=False)
        elif latest is not None:
            meta = {
                "startTs": None,
                "endTs": latest.ts.isoformat(sep=" ", timespec="seconds") if latest.ts else None,
                "sampleCount": -1,
            }
        items.append(furnace_to_item(f, latest=latest, meta=meta))
    return items


async def get_furnace(db: AsyncSession, code: str) -> dict[str, Any]:
    furnace = await _get_furnace(db, code)
    latest = await latest_sample(db, code)
    meta = await data_range(db, code, with_count=False)
    return furnace_to_item(furnace, latest=latest, meta=meta)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise AppError(ErrorCode.VALIDATION, f"invalid datetime: {value}", status_code=422)


async def get_series(
    db: AsyncSession,
    code: str,
    *,
    from_ts: str | None = None,
    to_ts: str | None = None,
    step_minutes: int = 1,
    limit: int = 2000,
) -> dict[str, Any]:
    await _get_furnace(db, code)
    meta = await data_range(db, code)
    if meta is None:
        return {"kilnCode": code, "points": [], "dataRange": None, "stepMinutes": step_minutes}

    start = _parse_dt(from_ts)
    end = _parse_dt(to_ts)
    if start is None or end is None:
        # 默认取数据末尾 24 小时
        end = _parse_dt(meta["endTs"])
        assert end is not None
        start = end - timedelta(hours=24)
        data_start = _parse_dt(meta["startTs"])
        if data_start and start < data_start:
            start = data_start

    step = max(1, min(int(step_minutes or 1), 60))
    lim = max(1, min(int(limit or 2000), 10000))

    result = await db.execute(
        select(KilnProcessSample)
        .where(
            KilnProcessSample.kiln_code == code,
            KilnProcessSample.ts >= start,
            KilnProcessSample.ts <= end,
        )
        .order_by(KilnProcessSample.ts.asc())
        .limit(lim * step)  # 先多取再抽稀
    )
    rows = list(result.scalars().all())
    if step > 1:
        rows = rows[::step][:lim]
    else:
        rows = rows[:lim]

    return {
        "kilnCode": code,
        "from": start.isoformat(sep=" ", timespec="seconds"),
        "to": end.isoformat(sep=" ", timespec="seconds"),
        "stepMinutes": step,
        "dataRange": meta,
        "points": [sample_to_dict(r) for r in rows],
    }


async def get_snapshot(
    db: AsyncSession,
    code: str,
    *,
    at: str | None = None,
    offset_minutes: int | None = None,
) -> dict[str, Any]:
    furnace = await _get_furnace(db, code)
    meta = await data_range(db, code)
    if meta is None:
        raise AppError(ErrorCode.NOT_FOUND, "no process samples for furnace", status_code=404)

    target: datetime | None = None
    if at:
        target = _parse_dt(at)
    elif offset_minutes is not None:
        start = _parse_dt(meta["startTs"])
        assert start is not None
        target = start + timedelta(minutes=max(0, int(offset_minutes)))
    else:
        # 默认最新一条
        latest = await latest_sample(db, code)
        return {
            "item": furnace_to_item(furnace, latest=latest, meta=meta),
            "sample": sample_to_dict(latest) if latest else None,
            "playback": {
                "mode": "latest",
                "offsetMinutes": None,
                "at": sample_to_dict(latest)["ts"] if latest else None,
            },
        }

    assert target is not None
    sample = await sample_at_or_before(db, code, target)
    if sample is None:
        # 若偏移早于首条，取首条
        result = await db.execute(
            select(KilnProcessSample)
            .where(KilnProcessSample.kiln_code == code)
            .order_by(KilnProcessSample.ts.asc())
            .limit(1)
        )
        sample = result.scalar_one_or_none()

    start = _parse_dt(meta["startTs"])
    offset = None
    if start and sample and sample.ts:
        offset = int((sample.ts - start).total_seconds() // 60)

    return {
        "item": furnace_to_item(furnace, latest=sample, meta=meta),
        "sample": sample_to_dict(sample) if sample else None,
        "playback": {
            "mode": "offset" if offset_minutes is not None else "at",
            "offsetMinutes": offset,
            "at": sample_to_dict(sample)["ts"] if sample else None,
            "requestedAt": target.isoformat(sep=" ", timespec="seconds"),
        },
    }
