"""窑次上传、参数匹配和预测工作台。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import UploadFile
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from common.errors import AppError, ErrorCode
from common.kiln_predict import CycleSeries, build_workspace, derive_gas
from common.kiln_xls import MAX_FILE_BYTES, ParsedFile, parse_upload, preview_upload
from db.models.furnace import Furnace, KilnCycle, KilnProcessSample

ZONE_NAMES = ("一区", "二区", "三区", "四区", "五区", "六区")
SAMPLE_UPDATE_COLS = [
    "temp_sp",
    "afr_sp",
    "z1_temp",
    "z1_gas_flow",
    "z1_air_flow",
    "z1_air_valve",
    "z2_temp",
    "z2_gas_flow",
    "z2_air_flow",
    "z2_air_valve",
    "z3_temp",
    "z3_gas_flow",
    "z3_air_flow",
    "z3_air_valve",
    "z4_temp",
    "z4_gas_flow",
    "z4_air_flow",
    "z4_air_valve",
    "z5_temp",
    "z5_gas_flow",
    "z5_air_flow",
    "z5_air_valve",
    "z6_temp",
    "z6_gas_flow",
    "z6_air_flow",
    "z6_air_valve",
    "furnace_p_sp",
    "furnace_p_meas",
    "furnace_p_out",
    "gas_p_sp",
    "gas_p_meas",
    "gas_p_out",
    "air_p_sp",
    "air_p_meas",
    "air_p_out",
    "gas_flow_instant",
    "gas_flow_total",
    "air_flow_instant",
    "air_flow_total",
    "o2_sp",
    "o2_meas",
    "source_file",
    "batch_no",
    "zone_count",
]


async def upload_cycles(
    db: AsyncSession,
    files: list[UploadFile],
    kiln_code: str | None,
    mappings: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    if not files:
        raise AppError(ErrorCode.BAD_REQUEST, "请上传 xls、xlsx 或包含窑次表的 zip", 400)
    parsed: list[ParsedFile] = []
    for upload in files:
        blob = await upload.read(MAX_FILE_BYTES + 1)
        parsed.extend(parse_upload(upload.filename or "upload", blob, kiln_code, mappings))
        await upload.close()
    saved_ids: list[int] = []
    file_results: list[dict[str, Any]] = []
    for item in parsed:
        if item.status != "ok" or not item.rows or not item.kiln_code:
            file_results.append(_file_result(item, None))
            continue
        await _ensure_furnace(db, item.kiln_code, item.kiln_no or item.kiln_code)
        await _upsert_samples(db, item.rows)
        cycle = await _upsert_cycle(db, item)
        saved_ids.append(cycle.id)
        file_results.append(_file_result(item, cycle.id))
    await db.commit()
    items = await list_cycles(db)
    return {"files": file_results, "cycles": items, "cycleIds": saved_ids}


async def preview_cycles(files: list[UploadFile], kiln_code: str | None) -> list[dict]:
    result = []
    for upload in files:
        blob = await upload.read(MAX_FILE_BYTES + 1)
        result.extend(preview_upload(upload.filename or "upload", blob, kiln_code))
        await upload.close()
    return result


async def list_cycles(db: AsyncSession) -> list[dict[str, Any]]:
    await _sync_existing(db)
    result = await db.execute(
        select(KilnCycle).order_by(KilnCycle.start_ts.desc(), KilnCycle.id.desc())
    )
    cycles = list(result.scalars().all())
    names = await _furnace_names(db, {cycle.kiln_code for cycle in cycles})
    return [_cycle_dict(cycle, names.get(cycle.kiln_code)) for cycle in cycles]


async def update_cycle_meta(
    db: AsyncSession,
    cycle_id: int,
    *,
    curve_version: str,
    load_kg: float | None,
) -> dict[str, Any]:
    cycle = await _get_cycle(db, cycle_id)
    text = curve_version.strip()
    cycle.curve_version = text[:64] or None
    if load_kg is not None and (load_kg < 0 or load_kg > 1_000_000):
        raise AppError(ErrorCode.BAD_REQUEST, "装载量需要在 0 到 1000000 kg", 400)
    cycle.load_kg = load_kg
    await db.commit()
    await db.refresh(cycle)
    names = await _furnace_names(db, {cycle.kiln_code})
    return _cycle_dict(cycle, names.get(cycle.kiln_code))


async def cycle_workspace(
    db: AsyncSession, cycle_id: int, *, downsample: bool = True
) -> dict[str, Any]:
    await _sync_existing(db)
    cycle = await _get_cycle(db, cycle_id)
    target_points = await _load_points(db, cycle)
    derive_gas(target_points)
    peers = await _peer_cycles(db, cycle)
    peer_series: list[CycleSeries] = []
    for peer in peers:
        points = await _load_points(db, peer)
        derive_gas(points)
        if points:
            peer_series.append(_series(peer, points))
    workspace = build_workspace(_series(cycle, target_points), peer_series, downsample=downsample)
    names = await _furnace_names(db, {cycle.kiln_code})
    workspace["cycle"] = _cycle_dict(cycle, names.get(cycle.kiln_code))
    return workspace


async def _sync_existing(db: AsyncSession) -> None:
    source = func.coalesce(KilnProcessSample.source_file, "")
    batch = func.coalesce(KilnProcessSample.batch_no, "")
    result = await db.execute(
        select(
            KilnProcessSample.kiln_code,
            source.label("source_file"),
            batch.label("batch_no"),
            func.min(KilnProcessSample.ts),
            func.max(KilnProcessSample.ts),
            func.count(),
        ).group_by(KilnProcessSample.kiln_code, source, batch)
    )
    rows = result.all()
    if not rows:
        return
    payload = []
    for kiln_code, source_file, batch_no, start_ts, end_ts, count in rows:
        stored_source = source_file or _legacy_source(batch_no, start_ts)
        payload.append(
            {
                "kiln_code": kiln_code,
                "batch_no": (batch_no or "")[:16],
                "source_file": stored_source[:255],
                "start_ts": start_ts,
                "end_ts": end_ts,
                "sample_count": int(count),
            }
        )
    for chunk in _chunks(payload, 200):
        stmt = mysql_insert(KilnCycle).values(chunk)
        stmt = stmt.on_duplicate_key_update(
            start_ts=stmt.inserted.start_ts,
            end_ts=stmt.inserted.end_ts,
            sample_count=stmt.inserted.sample_count,
        )
        await db.execute(stmt)
    await db.commit()


async def _peer_cycles(db: AsyncSession, cycle: KilnCycle) -> list[KilnCycle]:
    result = await db.execute(
        select(KilnCycle)
        .where(KilnCycle.id != cycle.id, KilnCycle.end_ts < cycle.start_ts)
        .order_by(KilnCycle.end_ts.desc())
        .limit(20)
    )
    return list(result.scalars().all())


async def _load_points(db: AsyncSession, cycle: KilnCycle) -> list[dict[str, Any]]:
    filters = [KilnProcessSample.kiln_code == cycle.kiln_code]
    if cycle.source_file.startswith("legacy:"):
        filters.append(KilnProcessSample.source_file.is_(None))
    else:
        filters.append(KilnProcessSample.source_file == cycle.source_file)
    if cycle.batch_no:
        filters.append(KilnProcessSample.batch_no == cycle.batch_no)
    else:
        filters.append(or_(KilnProcessSample.batch_no == "", KilnProcessSample.batch_no.is_(None)))
    result = await db.execute(
        select(KilnProcessSample).where(*filters).order_by(KilnProcessSample.ts.asc())
    )
    return [_point(row) for row in result.scalars().all()]


async def _upsert_samples(db: AsyncSession, rows: list[dict[str, Any]]) -> None:
    table = KilnProcessSample.__table__
    for chunk in _chunks(rows, 400):
        stmt = mysql_insert(table).values(chunk)
        updates = {col: stmt.inserted[col] for col in SAMPLE_UPDATE_COLS}
        stmt = stmt.on_duplicate_key_update(**updates)
        await db.execute(stmt)


async def _upsert_cycle(db: AsyncSession, item: ParsedFile) -> KilnCycle:
    start_ts = min(row["ts"] for row in item.rows)
    end_ts = max(row["ts"] for row in item.rows)
    payload = {
        "kiln_code": item.kiln_code,
        "batch_no": (item.batch_no or "")[:16],
        "source_file": (item.filename or "")[:255],
        "start_ts": start_ts,
        "end_ts": end_ts,
        "sample_count": len(item.rows),
    }
    stmt = mysql_insert(KilnCycle).values(payload)
    stmt = stmt.on_duplicate_key_update(
        start_ts=stmt.inserted.start_ts,
        end_ts=stmt.inserted.end_ts,
        sample_count=stmt.inserted.sample_count,
    )
    await db.execute(stmt)
    await db.flush()
    found = await db.execute(
        select(KilnCycle).where(
            KilnCycle.kiln_code == payload["kiln_code"],
            KilnCycle.source_file == payload["source_file"],
            KilnCycle.batch_no == payload["batch_no"],
        )
    )
    return found.scalar_one()


async def _ensure_furnace(db: AsyncSession, code: str, kiln_no: str) -> None:
    found = await db.execute(select(Furnace.id).where(Furnace.code == code))
    if found.scalar_one_or_none() is not None:
        return
    db.add(
        Furnace(
            code=code,
            name=f"{kiln_no} 燃气梭式窑",
            kiln_no=kiln_no,
            type="燃气梭式窑",
            workshop="烧结车间",
            status="idle",
            remark="窑次工况表上传",
            enabled=True,
        )
    )
    await db.flush()


async def _get_cycle(db: AsyncSession, cycle_id: int) -> KilnCycle:
    cycle = await db.get(KilnCycle, cycle_id)
    if cycle is None:
        raise AppError(ErrorCode.NOT_FOUND, "窑次不存在", 404)
    return cycle


async def _furnace_names(db: AsyncSession, codes: set[str]) -> dict[str, str]:
    if not codes:
        return {}
    result = await db.execute(select(Furnace).where(Furnace.code.in_(codes)))
    return {row.code: row.name for row in result.scalars().all()}


def _series(cycle: KilnCycle, points: list[dict[str, Any]]) -> CycleSeries:
    return CycleSeries(
        cycle_id=cycle.id,
        kiln_code=cycle.kiln_code,
        batch_no=cycle.batch_no,
        source_file=cycle.source_file,
        curve_version=cycle.curve_version,
        load_kg=cycle.load_kg,
        points=points,
    )


def _point(row: KilnProcessSample) -> dict[str, Any]:
    zones = []
    temps: list[float] = []
    for index, name in enumerate(ZONE_NAMES, start=1):
        temp = getattr(row, f"z{index}_temp")
        gas = getattr(row, f"z{index}_gas_flow")
        air = getattr(row, f"z{index}_air_flow")
        valve = getattr(row, f"z{index}_air_valve")
        if temp is not None:
            temps.append(float(temp))
        zones.append(
            {
                "name": name,
                "temp": temp,
                "gas": gas,
                "air": air,
                "valve": valve,
            }
        )
    return {
        "ts": row.ts,
        "temp_sp": row.temp_sp,
        "zone_temp": sum(temps) / len(temps) if temps else None,
        "gas": row.gas_flow_instant,
        "gas_total": row.gas_flow_total,
        "air": row.air_flow_instant,
        "o2": row.o2_meas,
        "o2_sp": row.o2_sp,
        "afr": row.afr_sp,
        "furnace_p": row.furnace_p_meas,
        "zones": zones,
    }


def _cycle_dict(cycle: KilnCycle, kiln_name: str | None) -> dict[str, Any]:
    batch = cycle.batch_no or "未编号"
    return {
        "id": cycle.id,
        "kilnCode": cycle.kiln_code,
        "kilnName": kiln_name or cycle.kiln_code,
        "batchNo": cycle.batch_no,
        "sourceFile": cycle.source_file,
        "startTs": _iso(cycle.start_ts),
        "endTs": _iso(cycle.end_ts),
        "sampleCount": cycle.sample_count,
        "curveVersion": cycle.curve_version,
        "loadKg": cycle.load_kg,
        "label": f"{cycle.kiln_code} · 窑次 {batch} · {cycle.source_file}",
    }


def _file_result(item: ParsedFile, cycle_id: int | None) -> dict[str, Any]:
    return {
        "name": item.filename,
        "status": item.status,
        "message": item.message,
        "kilnCode": item.kiln_code,
        "batchNo": item.batch_no,
        "rows": len(item.rows),
        "cycleId": cycle_id,
    }


def _legacy_source(batch_no: str, start_ts: datetime | None) -> str:
    stamp = start_ts.strftime("%Y%m%d%H%M%S") if start_ts else "na"
    return f"legacy:{batch_no or 'na'}:{stamp}"


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat(sep=" ", timespec="seconds")


def _chunks(rows: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [rows[index : index + size] for index in range(0, len(rows), size)]
