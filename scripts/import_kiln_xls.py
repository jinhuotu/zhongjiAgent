"""批量导入 3# 窑历史工况 .xls → MySQL。

每个文件含两个 Sheet：
  - 温度：设定温 / 空燃比 / 一～N 区测量值与流量阀位
  - 压力：窑压 / 燃气压 / 助燃风压 / 总流量瞬时累计 / 氧含量

按时间戳 merge 成宽表一行；累计流量跨文件原样入库（可能重置）。
幂等键：(kiln_code, ts)

用法：
  poetry run python -m alembic upgrade head
  poetry run python scripts/import_kiln_xls.py --dir data/raw/kiln --kiln TC-03
  poetry run python scripts/import_kiln_xls.py --dir data/raw/kiln --dry-run
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import xlrd
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.orm import Session, sessionmaker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages"))
sys.path.insert(0, str(ROOT / "apps"))

from common.config import get_settings  # noqa: E402
from db.models.furnace import Furnace, KilnProcessSample  # noqa: E402

ZONE_CN = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七", 8: "八", 9: "九"}
FILENAME_RE = re.compile(
    r"^(?P<kiln_no>\d+)#_(?P<batch>\d+)_(?P<date>\d{8})\.xls$",
    re.IGNORECASE,
)

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


def _sync_db_url(async_url: str) -> str:
    url = async_url.replace("mysql+asyncmy://", "mysql+pymysql://", 1)
    if "charset=" not in url:
        url = f"{url}{'&' if '?' in url else '?'}charset=utf8mb4"
    return url


def _cell_str(sheet: xlrd.sheet.Sheet, r: int, c: int) -> str:
    if c >= sheet.ncols or r >= sheet.nrows:
        return ""
    v = sheet.cell_value(r, c)
    if v is None:
        return ""
    if isinstance(v, float) and v == int(v):
        return str(int(v))
    return str(v).strip()


def _to_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        if isinstance(v, float) and (v != v):  # NaN
            return None
        return float(v)
    s = str(v).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_ts(sheet: xlrd.sheet.Sheet, r: int, c: int = 0) -> datetime | None:
    cell = sheet.cell(r, c)
    if cell.ctype == xlrd.XL_CELL_DATE:
        return xlrd.xldate_as_datetime(cell.value, sheet.book.datemode)
    raw = _cell_str(sheet, r, c)
    if not raw:
        return None
    for fmt in (
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y-%m-%d %H:%M",
    ):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def detect_sheet_kind(sheet: xlrd.sheet.Sheet) -> str:
    blob = " ".join(
        _cell_str(sheet, r, c)
        for r in range(min(2, sheet.nrows))
        for c in range(sheet.ncols)
    )
    name = (sheet.name or "").strip()
    if name in ("温度",) or "一区温度" in blob or "空燃比" in blob:
        return "temp"
    if name in ("压力",) or "窑内压力" in blob or "氧含量" in blob:
        return "pressure"
    return "unknown"


def parse_filename(path: Path) -> tuple[str | None, str | None]:
    m = FILENAME_RE.match(path.name)
    if not m:
        return None, None
    return f"{m.group('kiln_no')}#", m.group("batch")


def _find_zone_starts(header0: list[str]) -> list[tuple[int, int]]:
    """返回 [(col_index, zone_no), ...] 按一区…九区。"""
    found: list[tuple[int, int]] = []
    for c, text in enumerate(header0):
        for z, cn in ZONE_CN.items():
            if text == f"{cn}区温度":
                found.append((c, z))
                break
    found.sort(key=lambda x: x[1])
    return found


def parse_temp_sheet(sheet: xlrd.sheet.Sheet) -> dict[datetime, dict[str, Any]]:
    if sheet.nrows < 3:
        return {}
    header0 = [_cell_str(sheet, 0, c) for c in range(sheet.ncols)]
    zones = _find_zone_starts(header0)
    if not zones:
        # 兜底：从第 3 列起每 4 列一区
        zones = [(3 + i * 4, i + 1) for i in range(6) if 3 + i * 4 + 3 < sheet.ncols]

    rows: dict[datetime, dict[str, Any]] = {}
    for r in range(2, sheet.nrows):
        ts = _parse_ts(sheet, r, 0)
        if ts is None:
            continue
        item: dict[str, Any] = {
            "temp_sp": _to_float(sheet.cell_value(r, 1)) if sheet.ncols > 1 else None,
            "afr_sp": _to_float(sheet.cell_value(r, 2)) if sheet.ncols > 2 else None,
            "zone_count": len(zones),
        }
        for col, z in zones:
            if z > 6:
                continue
            prefix = f"z{z}"
            item[f"{prefix}_temp"] = _to_float(sheet.cell_value(r, col)) if col < sheet.ncols else None
            item[f"{prefix}_gas_flow"] = (
                _to_float(sheet.cell_value(r, col + 1)) if col + 1 < sheet.ncols else None
            )
            item[f"{prefix}_air_flow"] = (
                _to_float(sheet.cell_value(r, col + 2)) if col + 2 < sheet.ncols else None
            )
            item[f"{prefix}_air_valve"] = (
                _to_float(sheet.cell_value(r, col + 3)) if col + 3 < sheet.ncols else None
            )
        rows[ts] = item
    return rows


def parse_pressure_sheet(sheet: xlrd.sheet.Sheet) -> dict[datetime, dict[str, Any]]:
    if sheet.nrows < 3:
        return {}
    # 固定 17 列布局（与现有甲方导出一致）
    rows: dict[datetime, dict[str, Any]] = {}
    for r in range(2, sheet.nrows):
        ts = _parse_ts(sheet, r, 0)
        if ts is None:
            continue
        vals = [sheet.cell_value(r, c) if c < sheet.ncols else None for c in range(17)]
        rows[ts] = {
            "temp_sp": _to_float(vals[1]),
            "furnace_p_sp": _to_float(vals[2]),
            "furnace_p_meas": _to_float(vals[3]),
            "furnace_p_out": _to_float(vals[4]),
            "gas_p_sp": _to_float(vals[5]),
            "gas_p_meas": _to_float(vals[6]),
            "gas_p_out": _to_float(vals[7]),
            "air_p_sp": _to_float(vals[8]),
            "air_p_meas": _to_float(vals[9]),
            "air_p_out": _to_float(vals[10]),
            "gas_flow_instant": _to_float(vals[11]),
            "gas_flow_total": _to_float(vals[12]),
            "air_flow_instant": _to_float(vals[13]),
            "air_flow_total": _to_float(vals[14]),
            "o2_sp": _to_float(vals[15]),
            "o2_meas": _to_float(vals[16]),
        }
    return rows


def merge_sheets(
    temp_rows: dict[datetime, dict[str, Any]],
    pressure_rows: dict[datetime, dict[str, Any]],
    *,
    kiln_code: str,
    source_file: str,
    batch_no: str | None,
) -> list[dict[str, Any]]:
    all_ts = sorted(set(temp_rows) | set(pressure_rows))
    out: list[dict[str, Any]] = []
    for ts in all_ts:
        t = temp_rows.get(ts, {})
        p = pressure_rows.get(ts, {})
        row: dict[str, Any] = {
            "kiln_code": kiln_code,
            "ts": ts,
            "source_file": source_file,
            "batch_no": batch_no,
        }
        # 温度设定优先取温度 sheet；压力 sheet 作补
        row["temp_sp"] = t.get("temp_sp") if t.get("temp_sp") is not None else p.get("temp_sp")
        row["afr_sp"] = t.get("afr_sp")
        row["zone_count"] = t.get("zone_count")
        for z in range(1, 7):
            for suffix in ("temp", "gas_flow", "air_flow", "air_valve"):
                key = f"z{z}_{suffix}"
                row[key] = t.get(key)
        for key in (
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
        ):
            row[key] = p.get(key)
        out.append(row)
    return out


def parse_xls_file(path: Path, kiln_code: str) -> list[dict[str, Any]]:
    book = xlrd.open_workbook(str(path), encoding_override="gbk")
    temp_rows: dict[datetime, dict[str, Any]] = {}
    pressure_rows: dict[datetime, dict[str, Any]] = {}
    for i in range(book.nsheets):
        sheet = book.sheet_by_index(i)
        kind = detect_sheet_kind(sheet)
        if kind == "temp":
            temp_rows = parse_temp_sheet(sheet)
        elif kind == "pressure":
            pressure_rows = parse_pressure_sheet(sheet)
        else:
            print(f"  [warn] skip unknown sheet: {sheet.name!r}")
    _kiln_no, batch = parse_filename(path)
    return merge_sheets(
        temp_rows,
        pressure_rows,
        kiln_code=kiln_code,
        source_file=path.name,
        batch_no=batch,
    )


def ensure_furnace(session: Session, kiln_code: str) -> None:
    row = session.execute(select(Furnace).where(Furnace.code == kiln_code)).scalar_one_or_none()
    if row is not None:
        return
    session.add(
        Furnace(
            code=kiln_code,
            name="3# 车式窑",
            kiln_no="3#",
            type="燃气车式窑",
            workshop="热处理车间",
            status="idle",
            remark="历史工况 Excel 导入（2024）",
            enabled=True,
        )
    )
    session.commit()
    print(f"[ok] seeded furnace {kiln_code}")


def upsert_rows(session: Session, rows: list[dict[str, Any]], batch_size: int = 800) -> int:
    if not rows:
        return 0
    total = 0
    table = KilnProcessSample.__table__
    for i in range(0, len(rows), batch_size):
        chunk = rows[i : i + batch_size]
        stmt = mysql_insert(table).values(chunk)
        update_map = {c: stmt.inserted[c] for c in SAMPLE_UPDATE_COLS}
        stmt = stmt.on_duplicate_key_update(**update_map)
        session.execute(stmt)
        session.commit()
        total += len(chunk)
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description="导入窑炉历史 .xls 工况")
    parser.add_argument(
        "--dir",
        default=str(ROOT / "data" / "raw" / "kiln"),
        help="xls 目录",
    )
    parser.add_argument("--kiln", default="TC-03", help="前端窑 ID / kiln_code")
    parser.add_argument("--dry-run", action="store_true", help="只解析不写库")
    parser.add_argument("--limit", type=int, default=0, help="最多处理文件数（0=全部）")
    args = parser.parse_args()

    data_dir = Path(args.dir)
    if not data_dir.is_dir():
        raise SystemExit(f"directory not found: {data_dir}")

    files = sorted(data_dir.glob("*.xls"))
    if args.limit > 0:
        files = files[: args.limit]
    if not files:
        raise SystemExit(f"no .xls under {data_dir}")

    print(f"files={len(files)} kiln={args.kiln} dry_run={args.dry_run}")

    session: Session | None = None
    if not args.dry_run:
        settings = get_settings()
        engine = create_engine(_sync_db_url(settings.database_url), pool_pre_ping=True)
        SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        session = SessionLocal()
        ensure_furnace(session, args.kiln)

    total_rows = 0
    failed: list[str] = []
    try:
        for idx, path in enumerate(files, start=1):
            try:
                rows = parse_xls_file(path, args.kiln)
                if args.dry_run:
                    sample_ts = rows[0]["ts"] if rows else None
                    print(
                        f"[{idx}/{len(files)}] {path.name}: rows={len(rows)} first_ts={sample_ts}"
                    )
                else:
                    assert session is not None
                    n = upsert_rows(session, rows)
                    total_rows += n
                    print(f"[{idx}/{len(files)}] {path.name}: upserted={n}")
            except Exception as exc:  # noqa: BLE001
                failed.append(f"{path.name}: {exc}")
                print(f"[{idx}/{len(files)}] {path.name}: ERROR {exc}")
    finally:
        if session is not None:
            session.close()

    print(f"done total_upserted={total_rows} failed={len(failed)}")
    if failed:
        print("failures:")
        for line in failed:
            print(" ", line)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
