"""解析三井窑次工况表。支持 xls、xlsx，以及只内含这些表的 zip。

温度表、压力表的列布局与 scripts/import_kiln_xls.py 一致。
图纸、照片、说明书在压缩包里会被跳过。
"""

from __future__ import annotations

import io
import math
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import xlrd

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
PKG = {"pr": "http://schemas.openxmlformats.org/package/2006/relationships"}
WB = {
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}

ZONE_CN = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七", 8: "八", 9: "九"}
EXCEL_EPOCH = datetime(1899, 12, 30)
MAX_ZIP_FILES = 60
MAX_UNCOMPRESSED = 150_000_000
MAX_FILE_BYTES = 80_000_000

FILE_WITH_BATCH = re.compile(
    r"^(?P<no>\d+)#_(?P<batch>[^_]+)_(?P<date>\d{8})",
    re.IGNORECASE,
)
FILE_DATE_ONLY = re.compile(r"^(?P<no>\d+)#(?P<date>\d{8})", re.IGNORECASE)
KILN_MARK = re.compile(r"(\d+)\s*#")
BATCH_MARK = re.compile(r"窑次\s*(\d+)")

SAMPLE_KEYS = (
    "temp_sp",
    "afr_sp",
    "zone_count",
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
)

FIELD_LABELS = {
    "ts": "时间",
    "temp_sp": "设定温度",
    "gas_flow_instant": "燃气瞬时流量",
    "gas_flow_total": "燃气累计流量",
    "air_flow_instant": "助燃风瞬时流量",
    "o2_meas": "氧含量实测",
    "o2_sp": "氧含量设定",
    "afr_sp": "空燃比设定",
    "furnace_p_meas": "窑压实测",
    **{
        f"z{i}_{suffix}": f"{cn}区{label}"
        for i, cn in list(ZONE_CN.items())[:6]
        for suffix, label in (
            ("temp", "温度"),
            ("gas_flow", "燃气流量"),
            ("air_flow", "助燃风流量"),
            ("air_valve", "阀位"),
        )
    },
}


@dataclass
class ParsedFile:
    filename: str
    status: str
    message: str
    kiln_code: str | None = None
    kiln_no: str | None = None
    batch_no: str = ""
    rows: list[dict[str, Any]] = field(default_factory=list)


def parse_upload(
    filename: str,
    blob: bytes,
    kiln_code: str | None = None,
    mappings: dict[str, dict[str, str]] | None = None,
) -> list[ParsedFile]:
    name = Path(filename or "upload").name
    if len(blob) > MAX_FILE_BYTES:
        return [ParsedFile(name, "failed", "单个文件超过 80MB")]
    lower = name.lower()
    if lower.endswith(".zip"):
        return _parse_zip(name, blob, kiln_code, mappings)
    if lower.endswith((".xls", ".xlsx")) or blob[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return [_parse_one(name, blob, kiln_code, (mappings or {}).get(name))]
    if blob[:2] == b"PK":
        return _parse_zip(name, blob, kiln_code, mappings)
    return [ParsedFile(name, "skipped", "不是窑次工况表，图纸、照片和说明书不解析")]


def identity_from_name(filename: str, override: str | None) -> tuple[str, str, str]:
    base = Path(filename).name
    matched = FILE_WITH_BATCH.match(base)
    if matched:
        no = int(matched.group("no"))
        return f"TC-{no:02d}", f"{no}#", matched.group("batch")[:16]
    matched = FILE_DATE_ONLY.match(base)
    if matched:
        no = int(matched.group("no"))
        return f"TC-{no:02d}", f"{no}#", matched.group("date")[:16]
    mark = KILN_MARK.search(base)
    if mark:
        no = int(mark.group(1))
        batch = ""
        batch_mark = BATCH_MARK.search(base)
        if batch_mark:
            batch = batch_mark.group(1)[:16]
        code = _clean_code(override) or f"TC-{no:02d}"
        return code, f"{no}#", batch
    code = _clean_code(override) or "TC-03"
    no_mark = re.search(r"(\d+)", code)
    kiln_no = f"{int(no_mark.group(1))}#" if no_mark else code
    return code, kiln_no, ""


def _clean_code(value: str | None) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    cleaned = re.sub(r"[^\w\-]", "", text, flags=re.ASCII)
    return cleaned[:32]


def _parse_zip(
    name: str,
    blob: bytes,
    override: str | None,
    mappings: dict[str, dict[str, str]] | None = None,
) -> list[ParsedFile]:
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile:
        return [ParsedFile(name, "failed", "压缩包无法打开")]
    out: list[ParsedFile] = []
    total = 0
    with zf:
        infos = [info for info in zf.infolist() if not info.is_dir()]
        if len(infos) > MAX_ZIP_FILES:
            out.append(
                ParsedFile(
                    name,
                    "skipped",
                    f"压缩包超过 {MAX_ZIP_FILES} 个文件，只处理前 {MAX_ZIP_FILES} 个",
                )
            )
            infos = infos[:MAX_ZIP_FILES]
        for info in infos:
            entry = _zip_name(info)
            if "__MACOSX" in entry or entry.endswith("/"):
                continue
            base = Path(entry).name
            if not base or base.startswith(".") or base.startswith("~$"):
                continue
            lower = base.lower()
            if not lower.endswith((".xls", ".xlsx")):
                out.append(ParsedFile(base, "skipped", "图纸、照片或说明书不解析"))
                continue
            if info.file_size > MAX_FILE_BYTES:
                out.append(ParsedFile(base, "failed", "文件超过 80MB"))
                continue
            total += info.file_size
            if total > MAX_UNCOMPRESSED:
                out.append(ParsedFile(base, "failed", "解压后的表格总体积过大，已停止"))
                break
            out.append(_parse_one(base, zf.read(info), override, (mappings or {}).get(base)))
    if not out:
        out.append(ParsedFile(name, "failed", "压缩包里没有窑次表"))
    return out


def _zip_name(info: zipfile.ZipInfo) -> str:
    raw = info.filename
    if info.flag_bits & 0x800:
        return raw
    try:
        return raw.encode("cp437").decode("gbk")
    except (UnicodeError, LookupError):
        return raw


def _guess_mapping(sheets: list[tuple[str, list[list[Any]]]]) -> dict[str, str]:
    aliases = {
        "ts": ("时间", "采样时间", "timestamp", "time", "日期时间"),
        "temp_sp": ("设定温度", "温度设定", "temp_sp", "sp"),
        "gas_flow_instant": ("燃气瞬时流量", "总管燃气流量", "gas_flow_instant", "gas"),
        "gas_flow_total": ("燃气累计流量", "gas_flow_total"),
        "air_flow_instant": ("助燃风瞬时流量", "总管助燃风", "air_flow_instant", "air"),
        "o2_meas": ("氧含量实测", "氧含量", "o2"),
        "furnace_p_meas": ("窑压实测", "窑压", "窑内压力", "furnace_p"),
    }
    for sheet_index, (_, rows) in enumerate(sheets):
        mapping = {}
        for column, value in enumerate(rows[0] if rows else []):
            header = _cell_str(value).lower()
            for key, label in FIELD_LABELS.items():
                if header and header in (key.lower(), label.lower(), *aliases.get(key, ())):
                    mapping.setdefault(key, f"{sheet_index}:{column}")
        if "ts" in mapping:
            return mapping
    return {}


def _parse_mapped(
    sheets: list[tuple[str, list[list[Any]]]],
    mapping: dict[str, str],
    kiln_code: str,
    filename: str,
    batch_no: str,
) -> list[dict[str, Any]]:
    if "ts" not in mapping or not any(key != "ts" for key in mapping):
        raise ValueError("请映射时间列及至少一个工况字段")
    references: dict[str, tuple[int, int]] = {}
    for key, value in mapping.items():
        if key not in FIELD_LABELS or not re.fullmatch(r"\d+:\d+", value):
            raise ValueError("字段映射格式无效，请重新确认列对应关系")
        sheet, column = map(int, value.split(":"))
        if sheet >= len(sheets) or not sheets[sheet][1] or column >= len(sheets[sheet][1][0]):
            raise ValueError("映射的列不存在")
        references[key] = sheet, column
    if len(set(mapping.values())) != len(mapping):
        raise ValueError("同一数据列不能同时映射为多个字段")
    sheet_index = references["ts"][0]
    if any(sheet != sheet_index for sheet, _ in references.values()):
        raise ValueError("自定义列映射需来自同一工作表；标准温度/压力双表请使用自动识别")
    parsed: dict[datetime, dict[str, Any]] = {}
    for row in sheets[sheet_index][1][1:]:
        time_col = references["ts"][1]
        ts = _parse_time(row[time_col] if time_col < len(row) else None)
        if ts is None:
            continue
        item = {key: None for key in SAMPLE_KEYS}
        item.update(
            {
                "ts": ts,
                "kiln_code": kiln_code,
                "source_file": filename[:255],
                "batch_no": batch_no[:16],
            }
        )
        for key, (_, column) in references.items():
            if key != "ts":
                item[key] = _to_float(row[column] if column < len(row) else None)
        parsed[ts] = item
    return [parsed[ts] for ts in sorted(parsed)]


def preview_upload(filename: str, blob: bytes, kiln_code: str | None) -> list[dict[str, Any]]:
    """只预览识别结果；原文件由浏览器保留，确认后再上传入库。"""
    parsed = parse_upload(filename, blob, kiln_code)
    blobs = {Path(filename).name: blob}
    if filename.lower().endswith(".zip") and len(blob) <= MAX_FILE_BYTES:
        try:
            with zipfile.ZipFile(io.BytesIO(blob)) as zf:
                total = 0
                for info in zf.infolist()[:MAX_ZIP_FILES]:
                    if info.is_dir() or info.file_size > MAX_FILE_BYTES:
                        continue
                    total += info.file_size
                    if total > MAX_UNCOMPRESSED:
                        break
                    base = Path(_zip_name(info)).name
                    if base.lower().endswith((".xls", ".xlsx")):
                        blobs[base] = zf.read(info)
        except zipfile.BadZipFile:
            pass
    result = []
    for item in parsed:
        columns: list[dict[str, Any]] = []
        mapping: dict[str, str] = {}
        payload = blobs.get(item.filename)
        if payload and item.filename.lower().endswith((".xls", ".xlsx")):
            try:
                sheets = _load_xlsx(payload) if payload[:2] == b"PK" else _load_xls(payload)
                mapping = _guess_mapping(sheets)
                for index, (name, rows) in enumerate(sheets):
                    for col, label in enumerate(rows[0] if rows else []):
                        if not _cell_str(label):
                            continue
                        sample = next(
                            (
                                _cell_str(row[col])
                                for row in rows[1:5]
                                if col < len(row) and _parse_time(row[0]) is not None
                            ),
                            "",
                        )
                        columns.append(
                            {
                                "id": f"{index}:{col}",
                                "label": f"{name} · {_cell_str(label)}",
                                "sample": sample[:80],
                            }
                        )
            except (zipfile.BadZipFile, xlrd.XLRDError, ET.ParseError, ValueError, KeyError):
                pass
        result.append(
            {
                "name": item.filename,
                "status": item.status,
                "message": item.message,
                "kilnCode": item.kiln_code,
                "batchNo": item.batch_no,
                "rows": len(item.rows),
                "columns": columns,
                "mapping": mapping,
                "fields": [
                    {
                        "key": key,
                        "label": label,
                        "present": sum(row.get(key) is not None for row in item.rows),
                    }
                    for key, label in FIELD_LABELS.items()
                ],
            }
        )
    return result


def _parse_one(
    filename: str, blob: bytes, override: str | None, mapping: dict[str, str] | None = None
) -> ParsedFile:
    kiln_code, kiln_no, batch_no = identity_from_name(filename, override)
    try:
        if blob[:2] == b"PK":
            sheets = _load_xlsx(blob)
        else:
            sheets = _load_xls(blob)
    except (zipfile.BadZipFile, xlrd.XLRDError, ET.ParseError, ValueError, KeyError) as exc:
        return ParsedFile(filename, "failed", f"无法读取表格：{exc}", kiln_code, kiln_no, batch_no)
    if mapping is not None or not any(name.strip() in ("温度", "压力") for name, _ in sheets):
        selected_mapping = mapping if mapping is not None else _guess_mapping(sheets)
        if mapping is not None or selected_mapping.get("ts"):
            try:
                rows = _parse_mapped(sheets, selected_mapping, kiln_code, filename, batch_no)
            except ValueError as exc:
                return ParsedFile(filename, "failed", str(exc), kiln_code, kiln_no, batch_no)
            return ParsedFile(
                filename,
                "ok" if rows else "failed",
                f"按字段映射读取 {len(rows)} 行",
                kiln_code,
                kiln_no,
                batch_no,
                rows,
            )
    temp_rows: dict[datetime, dict[str, Any]] = {}
    pressure_rows: dict[datetime, dict[str, Any]] = {}
    for sheet_name, rows in sheets:
        kind = _sheet_kind(sheet_name, rows)
        if kind == "temp":
            temp_rows = _parse_temp(rows)
        elif kind == "pressure":
            pressure_rows = _parse_pressure(rows)
    if not temp_rows and not pressure_rows:
        return ParsedFile(
            filename,
            "failed",
            "没有识别到「温度」或「压力」表，或表里没有时间",
            kiln_code,
            kiln_no,
            batch_no,
        )
    merged = _merge(temp_rows, pressure_rows, kiln_code, filename, batch_no)
    notes: list[str] = []
    if not temp_rows:
        notes.append("缺少温度表")
    if not pressure_rows:
        notes.append("缺少压力表")
    message = f"已写入 {len(merged)} 行"
    if notes:
        message = f"{message}（{'，'.join(notes)}）"
    return ParsedFile(filename, "ok", message, kiln_code, kiln_no, batch_no, merged)


def _merge(
    temp_rows: dict[datetime, dict[str, Any]],
    pressure_rows: dict[datetime, dict[str, Any]],
    kiln_code: str,
    source_file: str,
    batch_no: str,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for ts in sorted(set(temp_rows) | set(pressure_rows)):
        temp = temp_rows.get(ts, {})
        pressure = pressure_rows.get(ts, {})
        row: dict[str, Any] = {
            "kiln_code": kiln_code,
            "ts": ts,
            "source_file": source_file[:255],
            "batch_no": batch_no[:16],
        }
        for key in SAMPLE_KEYS:
            row[key] = None
        row["temp_sp"] = temp.get("temp_sp")
        if row["temp_sp"] is None:
            row["temp_sp"] = pressure.get("temp_sp")
        row["afr_sp"] = temp.get("afr_sp")
        row["zone_count"] = temp.get("zone_count")
        for zone in range(1, 7):
            for suffix in ("temp", "gas_flow", "air_flow", "air_valve"):
                key = f"z{zone}_{suffix}"
                row[key] = temp.get(key)
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
            row[key] = pressure.get(key)
        out.append(row)
    return out


def _sheet_kind(name: str, rows: list[list[Any]]) -> str:
    title = (name or "").strip()
    if title == "温度":
        return "temp"
    if title == "压力":
        return "pressure"
    parts: list[str] = []
    for row in rows[:2]:
        parts.extend(_cell_str(cell) for cell in row)
    blob = " ".join(parts)
    if "一区温度" in blob or "空燃比" in blob:
        return "temp"
    if "窑内压力" in blob or "氧含量" in blob:
        return "pressure"
    return "unknown"


def _parse_temp(rows: list[list[Any]]) -> dict[datetime, dict[str, Any]]:
    if len(rows) < 3:
        return {}
    header = [_cell_str(cell) for cell in rows[0]]
    zones: list[tuple[int, int]] = []
    for index, text in enumerate(header):
        for zone_no, cn in ZONE_CN.items():
            if text == f"{cn}区温度":
                zones.append((index, zone_no))
                break
    zones.sort(key=lambda item: item[1])
    if not zones:
        width = max((len(row) for row in rows[:3]), default=0)
        zones = [(3 + i * 4, i + 1) for i in range(6) if 3 + i * 4 < width]
    parsed: dict[datetime, dict[str, Any]] = {}
    for row in rows[2:]:
        ts = _parse_time(row[0] if row else None)
        if ts is None:
            continue
        item: dict[str, Any] = {
            "temp_sp": _to_float(row[1] if len(row) > 1 else None),
            "afr_sp": _to_float(row[2] if len(row) > 2 else None),
            "zone_count": len([zone for _, zone in zones if zone <= 6]),
        }
        for col, zone_no in zones:
            if zone_no > 6:
                continue
            prefix = f"z{zone_no}"
            item[f"{prefix}_temp"] = _to_float(row[col] if col < len(row) else None)
            item[f"{prefix}_gas_flow"] = _to_float(row[col + 1] if col + 1 < len(row) else None)
            item[f"{prefix}_air_flow"] = _to_float(row[col + 2] if col + 2 < len(row) else None)
            item[f"{prefix}_air_valve"] = _to_float(row[col + 3] if col + 3 < len(row) else None)
        parsed[ts] = item
    return parsed


def _parse_pressure(rows: list[list[Any]]) -> dict[datetime, dict[str, Any]]:
    if len(rows) < 3:
        return {}
    parsed: dict[datetime, dict[str, Any]] = {}
    keys = (
        "temp_sp",
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
    )
    for row in rows[2:]:
        ts = _parse_time(row[0] if row else None)
        if ts is None:
            continue
        item: dict[str, Any] = {}
        for offset, key in enumerate(keys, start=1):
            item[key] = _to_float(row[offset] if offset < len(row) else None)
        parsed[ts] = item
    return parsed


def _cell_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    return str(value).strip()


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return None
    if isinstance(value, int | float):
        if not math.isfinite(float(value)):
            return None
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        result = float(text)
        return result if math.isfinite(result) else None
    except ValueError:
        return None


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, int | float) and 20_000 < float(value) < 80_000:
        return EXCEL_EPOCH + timedelta(days=float(value))
    text = _cell_str(value)
    if not text:
        return None
    for fmt in (
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _load_xls(blob: bytes) -> list[tuple[str, list[list[Any]]]]:
    book = xlrd.open_workbook(file_contents=blob, encoding_override="gbk")
    sheets: list[tuple[str, list[list[Any]]]] = []
    for index in range(book.nsheets):
        sheet = book.sheet_by_index(index)
        rows: list[list[Any]] = []
        for r in range(sheet.nrows):
            row: list[Any] = []
            for c in range(sheet.ncols):
                cell = sheet.cell(r, c)
                if cell.ctype == xlrd.XL_CELL_DATE:
                    try:
                        row.append(xlrd.xldate_as_datetime(cell.value, book.datemode))
                    except (ValueError, OverflowError):
                        row.append(cell.value)
                elif cell.ctype == xlrd.XL_CELL_NUMBER:
                    row.append(float(cell.value))
                elif cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
                    row.append(None)
                else:
                    row.append(_cell_str(cell.value))
            rows.append(row)
        sheets.append((sheet.name, rows))
    return sheets


def _load_xlsx(blob: bytes) -> list[tuple[str, list[list[Any]]]]:
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        names = set(zf.namelist())
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            shared = _shared_strings(zf.read("xl/sharedStrings.xml"))
        order = _xlsx_sheet_order(zf, names)
        sheets: list[tuple[str, list[list[Any]]]] = []
        for sheet_name, path in order:
            if path not in names:
                continue
            sheets.append((sheet_name, _xlsx_rows(zf.read(path), shared)))
        if not sheets:
            raise ValueError("xlsx 里没有工作表")
        return sheets


def _xlsx_sheet_order(zf: zipfile.ZipFile, names: set[str]) -> list[tuple[str, str]]:
    if "xl/workbook.xml" not in names or "xl/_rels/workbook.xml.rels" not in names:
        paths = sorted(name for name in names if name.startswith("xl/worksheets/sheet"))
        return [(path, path) for path in paths]
    rel_root = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    rels: dict[str, str] = {}
    for rel in rel_root.findall("pr:Relationship", PKG):
        rel_id = rel.get("Id") or ""
        target = rel.get("Target") or ""
        if target.startswith("/"):
            target = target.lstrip("/")
        elif not target.startswith("xl/"):
            target = f"xl/{target}"
        rels[rel_id] = target
    root = ET.fromstring(zf.read("xl/workbook.xml"))
    order: list[tuple[str, str]] = []
    for sheet in root.findall("m:sheets/m:sheet", WB):
        rel_id = (
            sheet.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
            or ""
        )
        path = rels.get(rel_id)
        if path:
            order.append((sheet.get("name") or path, path))
    return order


def _shared_strings(blob: bytes) -> list[str]:
    root = ET.fromstring(blob)
    values: list[str] = []
    for item in root.findall("m:si", NS):
        texts = [node.text or "" for node in item.findall(".//m:t", NS)]
        values.append("".join(texts))
    return values


def _xlsx_rows(blob: bytes, shared: list[str]) -> list[list[Any]]:
    root = ET.fromstring(blob)
    grid: dict[tuple[int, int], Any] = {}
    max_row = 0
    max_col = 0
    for cell in root.findall("m:sheetData/m:row/m:c", NS):
        ref = cell.get("r") or ""
        col, row = _cell_ref(ref)
        if col < 0 or row < 0 or row > 20_000 or col > 48:
            continue
        kind = cell.get("t")
        value_node = cell.find("m:v", NS)
        value: Any = None
        if kind == "s" and value_node is not None and value_node.text:
            index = int(value_node.text)
            value = shared[index] if 0 <= index < len(shared) else ""
        elif kind == "inlineStr":
            texts = [node.text or "" for node in cell.findall(".//m:t", NS)]
            value = "".join(texts)
        elif value_node is not None and value_node.text:
            try:
                value = float(value_node.text)
            except ValueError:
                value = value_node.text
        grid[(row, col)] = value
        max_row = max(max_row, row)
        max_col = max(max_col, col)
    if not grid:
        return []
    return [[grid.get((row, col)) for col in range(max_col + 1)] for row in range(max_row + 1)]


def _cell_ref(ref: str) -> tuple[int, int]:
    letters: list[str] = []
    digits: list[str] = []
    for char in ref:
        if char.isalpha():
            letters.append(char)
        elif char.isdigit():
            digits.append(char)
    if not letters or not digits:
        return -1, -1
    col = 0
    for char in letters:
        col = col * 26 + (ord(char.upper()) - 64)
    return col - 1, int("".join(digits)) - 1
