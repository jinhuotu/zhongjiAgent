"""Bounded archive extraction, source anchors and deterministic table normalization."""

from __future__ import annotations

import csv
import hashlib
import io
import math
import re
import stat
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from xml.etree import ElementTree as ET

from common.kiln_xls import (
    _guess_mapping,
    _load_xls,
    _load_xlsx,
    _parse_time,
    _zip_name,
    parse_upload,
)

MAX_UPLOAD = 80_000_000
MAX_TOTAL = 300_000_000
MAX_ROWS = 100_000
MAX_ARCHIVE_ENTRIES = 200
MAX_ARCHIVE_DEPTH = 3
FIELD_NAMES = {
    "ts": ("时间", "timestamp", "time", "日期时间"),
    "temp_sp": ("设定温度", "temp_sp", "温度设定"),
    "temp": ("实测温度", "平均温度", "temp", "temperature"),
    "gas": ("燃气流量", "gas", "gas_flow_instant", "燃气瞬时流量"),
    "gas_total": ("燃气累计流量", "gas_flow_total"),
    "air": ("助燃风流量", "air", "air_flow_instant", "助燃风瞬时流量"),
    "o2": ("氧含量", "o2", "o2_meas", "氧含量实测"),
    "pressure": ("窑压", "pressure", "furnace_p_meas", "窑压实测"),
}
STANDARD_KEYS = {
    "gas_flow_instant": "gas",
    "gas_flow_total": "gas_total",
    "air_flow_instant": "air",
    "o2_meas": "o2",
    "furnace_p_meas": "pressure",
}


@dataclass
class ArchiveBudget:
    entries: int = 0
    expanded_bytes: int = 0
    archives: list[str] = field(default_factory=list)
    max_depth: int = 0


def safe_members(filename: str, blob: bytes, *, budget: ArchiveBudget | None = None):
    """Expand ZIP leaves, sharing a budget across every nesting level/upload.

    Paths such as data.zip!/run.xlsx preserve archive provenance and disambiguate
    identical basenames. Office ZIP containers are left to their own parsers.
    """
    if len(blob) > MAX_UPLOAD:
        raise ValueError("上传超过 80 MB，请拆分资料包")
    budget = budget if budget is not None else ArchiveBudget()
    if not filename.lower().endswith(".zip"):
        budget.entries += 1
        budget.expanded_bytes += len(blob)
        if budget.entries > MAX_ARCHIVE_ENTRIES or budget.expanded_bytes > MAX_TOTAL:
            raise ValueError("资料包所有文件累计数量或解压大小超出限制")
        yield PurePosixPath(filename.replace("\\", "/")).name, blob
        return

    def expand(payload, prefix, depth, archive_name):
        if depth > MAX_ARCHIVE_DEPTH:
            raise ValueError(f"嵌套 ZIP 超过允许的 {MAX_ARCHIVE_DEPTH} 层：{archive_name}")
        budget.archives.append(archive_name)
        budget.max_depth = max(budget.max_depth, depth)
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                members = [entry for entry in archive.infolist() if not entry.is_dir()]
                budget.entries += len(members)
                if budget.entries > MAX_ARCHIVE_ENTRIES:
                    raise ValueError(
                        f"资料包所有层合计超过 {MAX_ARCHIVE_ENTRIES} 个文件（含内层ZIP），请拆分"
                    )
                seen = set()
                for entry in members:
                    name = _zip_name(entry).replace("\\", "/")
                    path = PurePosixPath(name)
                    full_name = prefix + path.as_posix()
                    if (
                        path.is_absolute()
                        or ".." in path.parts
                        or ":" in name
                        or not path.name
                        or len(PurePosixPath(full_name).parts) > 12
                        or "\x00" in name
                    ):
                        raise ValueError(f"资料包包含不安全的路径：{full_name}")
                    if full_name in seen:
                        raise ValueError(f"资料包包含重复路径：{full_name}")
                    seen.add(full_name)
                    if stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1:
                        raise ValueError(f"不支持符号链接或加密资料包：{full_name}")
                    budget.expanded_bytes += entry.file_size
                    if (
                        budget.expanded_bytes > MAX_TOTAL
                        or entry.file_size > MAX_UPLOAD
                        or entry.file_size / max(entry.compress_size, 1) > 500
                    ):
                        raise ValueError(f"所有层累计解压大小或压缩比例超出限制：{full_name}")
                    data = archive.read(entry)
                    if path.suffix.lower() == ".zip":
                        yield from expand(data, full_name + "!/", depth + 1, full_name)
                    else:
                        yield full_name, data
        except zipfile.BadZipFile as exc:
            raise ValueError(f"ZIP 文件损坏或格式无效：{archive_name}") from exc

    yield from expand(blob, "", 0, filename)


def decode_text(blob: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030", "utf-16"):
        try:
            return blob.decode(encoding)
        except UnicodeError:
            pass
    raise ValueError("无法识别文本编码，请转换为 UTF-8")


def field_options(zones=16):
    return [{"key": k, "label": v[0]} for k, v in FIELD_NAMES.items()] + [
        {"key": f"z{i}_{suffix}", "label": f"{i}区{label}"}
        for i in range(1, zones + 1)
        for suffix, label in (("temp", "温度"), ("gas", "燃气流量"), ("air", "助燃风流量"))
    ]


def infer_mapping(sheets):
    out = []
    for si, (_, rows) in enumerate(sheets):
        fields = set()
        for ci, header in enumerate(rows[0] if rows else []):
            label = str(header).strip().lower()
            stripped = re.sub(r"[（(].*?[）)]", "", label).strip()
            key = next(
                (k for k, names in FIELD_NAMES.items() if stripped in names or stripped == k), None
            )
            if not key:
                match = re.fullmatch(r"z(\d+)_(temp|gas|air)", stripped)
                if match:
                    key = match.group()
                else:
                    cn = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8}
                    match = re.fullmatch(
                        r"([一二三四五六七八]|\d+)区(温度|燃气流量|助燃风流量)", stripped
                    )
                    if match:
                        zone = cn.get(match[1], match[1])
                        suffix = {"温度": "temp", "燃气流量": "gas", "助燃风流量": "air"}[match[2]]
                        key = f"z{zone}_{suffix}"
            if key and key not in fields:
                out.append({"column": f"{si}:{ci}", "field": key, "scale": 1, "offset": 0})
                fields.add(key)
        if "ts" in fields:
            return out
        out = []
    # Legacy aliases remain useful for older standard exports.
    old = _guess_mapping(sheets)
    return [
        {"column": col, "field": STANDARD_KEYS.get(key, key), "scale": 1, "offset": 0}
        for key, col in old.items()
        if key in FIELD_NAMES or key in STANDARD_KEYS or re.fullmatch(r"z\d+_temp", key)
    ]


def parse_file(name: str, blob: bytes, ocr=None):
    fid = hashlib.sha256(name.encode() + b"\0" + blob).hexdigest()[:24]
    manifest = {
        "id": fid,
        "path": name,
        "size": len(blob),
        "hash": hashlib.sha256(blob).hexdigest(),
        "kind": "other",
        "status": "unsupported",
        "message": "格式暂不支持；原文件已保留",
        "sources": [],
    }
    suffix = PurePosixPath(name).suffix.lower()
    data = {}
    try:
        if suffix in {".xls", ".xlsx", ".csv"}:
            if suffix == ".xlsx":
                with zipfile.ZipFile(io.BytesIO(blob)) as workbook:
                    if sum(item.file_size for item in workbook.infolist()) > MAX_TOTAL:
                        raise ValueError(f"XLSX 内部解压内容超过{MAX_TOTAL // 1_000_000}MB，请拆分")
            if suffix == ".csv":
                text = decode_text(blob)
                try:
                    dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t")
                except csv.Error:
                    dialect = csv.excel
                sheets = [("数据", list(csv.reader(io.StringIO(text), dialect)))]
            else:
                sheets = _load_xlsx(blob) if suffix == ".xlsx" else _load_xls(blob)
            if sum(len(rows) for _, rows in sheets) > MAX_ROWS:
                raise ValueError("单个表格超过 10 万行，请按窑次拆分")
            sheets = [
                (s, [[str(v) if isinstance(v, datetime) else v for v in row] for row in rows])
                for s, rows in sheets
            ]
            standard = []
            if suffix != ".csv" and any(s.strip() in {"温度", "压力"} for s, _ in sheets):
                parsed = parse_upload(PurePosixPath(name).name, blob, "KILN-01")[0]
                if parsed.status == "ok":
                    standard = [legacy_row(row) for row in parsed.rows]
            mapping = infer_mapping(sheets)
            columns = [
                {
                    "id": f"{si}:{ci}",
                    "label": f"{sheet} · {label}",
                    "sample": str(
                        next(
                            (row[ci] for row in rows[1:8] if ci < len(row) and row[ci] is not None),
                            "",
                        )
                    )[:80],
                }
                for si, (sheet, rows) in enumerate(sheets)
                for ci, label in enumerate(rows[0] if rows else [])
                if str(label or "").strip()
            ]
            manifest.update(
                kind="table",
                status="ready" if mapping or standard else "confirm",
                message="标准温度/压力双表合并" if standard else "请确认字段映射、单位与时间",
                columns=columns,
                mapping=mapping,
                standard=bool(standard),
                rowCount=sum(max(0, len(rows) - 1) for _, rows in sheets),
            )
            data = {"sheets": sheets, "standard": standard}
        else:
            chunks = []
            if suffix == ".docx":
                with zipfile.ZipFile(io.BytesIO(blob)) as document:
                    if sum(x.file_size for x in document.infolist()) > MAX_TOTAL:
                        raise ValueError("DOCX 内部文件过大")
                    root = ET.fromstring(document.read("word/document.xml"))
                    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
                    chunks = [
                        (f"段落 {i}", "".join(p.itertext()))
                        for i, p in enumerate(root.findall(".//w:body//w:p", ns), 1)
                    ]
            elif suffix in {".txt", ".md"}:
                chunks = [
                    (f"行 {i}", text) for i, text in enumerate(decode_text(blob).splitlines(), 1)
                ]
            elif suffix == ".pdf":
                from pypdf import PdfReader

                pdf = PdfReader(io.BytesIO(blob))
                if len(pdf.pages) > 300:
                    raise ValueError("PDF 超过 300 页，请拆分")
                chunks = [
                    (f"页 {i}", page.extract_text() or "") for i, page in enumerate(pdf.pages, 1)
                ]
            elif suffix in {".png", ".jpg", ".jpeg", ".bmp"} and ocr:
                chunks = [("图片 OCR", ocr(blob))]
            else:
                return manifest, data
            sources = []
            total_chars = 0
            for location, content in chunks:
                text = str(content).strip()
                if not text:
                    continue
                for start in range(0, len(text), 1800):
                    part = text[start : start + 1800]
                    total_chars += len(part)
                    if total_chars > 500_000:
                        raise ValueError("文档文本超过 50 万字，请拆分")
                    sources.append(
                        {
                            "id": f"{fid}:{len(sources) + 1}",
                            "fileId": fid,
                            "file": name,
                            "location": location,
                            "text": part,
                        }
                    )
            manifest.update(
                kind="document",
                status="ready" if sources else "confirm",
                message=f"提取 {len(sources)} 个可追溯片段"
                if sources
                else "未提取到文本，可能为扫描 PDF，请上传 OCR 后文本",
                sources=sources,
            )
    except Exception as exc:
        manifest.update(status="failed", message=str(exc)[:350])
    return manifest, data


def legacy_row(row):
    out = {
        STANDARD_KEYS.get(k, k): v
        for k, v in row.items()
        if k in STANDARD_KEYS or k == "temp_sp" or re.fullmatch(r"z\d+_temp", k)
    }
    out["ts"] = row["ts"].isoformat()
    for i in range(1, 17):
        for old, new in (("gas_flow", "gas"), ("air_flow", "air")):
            if row.get(f"z{i}_{old}") is not None:
                out[f"z{i}_{new}"] = row[f"z{i}_{old}"]
    return out


def parse_timestamp(value):
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            if dt.tzinfo:
                dt = dt.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None)
            return dt
        except ValueError:
            pass
    return _parse_time(value)


def normalize_table(data, mappings, use_standard=False):
    if use_standard:
        if not data.get("standard"):
            raise ValueError("该文件没有可用的标准双表结果")
        return data["standard"], 0
    allowed = {x["key"] for x in field_options()}
    if len({m["field"] for m in mappings}) != len(mappings) or len(
        {m["column"] for m in mappings}
    ) != len(mappings):
        raise ValueError("同一标准字段或原始列不能重复映射")
    refs = {}
    for m in mappings:
        if m["field"] not in allowed or not re.fullmatch(r"\d+:\d+", m["column"]):
            raise ValueError("字段映射无效")
        si, ci = map(int, m["column"].split(":"))
        if (
            si >= len(data["sheets"])
            or not data["sheets"][si][1]
            or ci >= len(data["sheets"][si][1][0])
        ):
            raise ValueError("映射的列不存在")
        refs[m["field"]] = (si, ci, m)
    if "ts" not in refs or len(refs) < 2:
        raise ValueError("需要映射时间及至少一个数值字段")
    sheet = refs["ts"][0]
    if any(v[0] != sheet for v in refs.values()):
        raise ValueError("自定义映射需来自同一 Sheet；标准双表请选择自动合并")
    rows, skipped = [], 0
    for raw in data["sheets"][sheet][1][1:]:
        ci = refs["ts"][1]
        ts = parse_timestamp(raw[ci] if ci < len(raw) else None)
        if ts is None:
            skipped += 1
            continue
        point = {"ts": ts.isoformat()}
        for key, (_, ci, m) in refs.items():
            if key == "ts":
                continue
            try:
                value = float(raw[ci]) * m.get("scale", 1) + m.get("offset", 0)
                point[key] = value if math.isfinite(value) else None
            except (TypeError, ValueError, IndexError):
                point[key] = None
        rows.append(point)
    return rows, skipped


def prepare_cycle(rows, cycle_id, min_gas=0.5):
    points = {}
    duplicates = 0
    for row in rows:
        row = dict(row)
        if row["ts"] in points:
            duplicates += 1
        points[row["ts"]] = row
    ordered = [points[k] for k in sorted(points)]
    for i, row in enumerate(ordered):
        row["cycle"] = cycle_id
        row["time"] = (
            datetime.fromisoformat(row["ts"])
            .replace(tzinfo=timezone(timedelta(hours=8)))
            .timestamp()
            / 60
        )
        temps = [v for k, v in row.items() if re.fullmatch(r"z\d+_temp", k) and v is not None]
        if row.get("temp") is None and temps:
            row["temp"] = sum(temps) / len(temps)
            row["tempSource"] = "zoneAverage"
        elif row.get("temp") is not None:
            row["tempSource"] = "provided"
        for key in ("gas", "air"):
            vals = [v for k, v in row.items() if re.fullmatch(r"z\d+_" + key, k) and v is not None]
            if row.get(key) is None and vals:
                row[key] = sum(vals)
        if (
            row.get("gas") is None
            and i
            and row.get("gas_total") is not None
            and ordered[i - 1].get("gas_total") is not None
        ):
            dt = row["time"] - ordered[i - 1]["time"]
            delta = row["gas_total"] - ordered[i - 1]["gas_total"]
            row["gas"] = delta * 60 / dt if 0 < dt <= 30 and delta >= 0 else None
        gas, air = row.get("gas"), row.get("air")
        row["afr"] = (
            air / gas
            if gas is not None and gas >= min_gas and air is not None and air >= 0
            else None
        )
    return ordered, duplicates


def document_conflicts(files):
    joined = "\n".join(s["text"] for f in files for s in f.get("sources", []))
    warnings = []
    counts = set(re.findall(r"(\d+)\s*个[（(]?[^\n]{0,8}温区", joined))
    if len(counts) > 1:
        warnings.append("资料出现多个温区数量，请核对有效温区与监测区的区别")
    if any("4烧嘴" in f["path"] or "4 烧嘴" in f["path"] for f in files) and re.search(
        r"6个[（(]对应6个温区", joined
    ):
        warnings.append("文件名为4烧嘴，正文提到6个烧嘴：请分别确认烧嘴数和温区数")
    if "阀位" in joined and "空燃比" in joined:
        warnings.append("资料包含阀位比例；本工作台空燃比使用同口径空气流量/燃气流量，禁止混用")
    return warnings


def equipment_suggestions(files):
    sources = [s for file in files for s in file.get("sources", [])]
    suggestion = {"zoneCount": None, "burnerCount": None, "evidence": []}
    for key, pattern in (
        ("zoneCount", r"(\d+)\s*个(?:有效)?温区"),
        ("burnerCount", r"(\d+)\s*个(?:燃气)?烧嘴"),
    ):
        for source in sources:
            match = re.search(pattern, source["text"])
            if match:
                value = int(match[1])
                if 1 <= value <= (16 if key == "zoneCount" else 64):
                    suggestion[key] = value
                    suggestion["evidence"].append(source)
                    break
    zones = [
        int(m[1])
        for file in files
        for mapping in file.get("mapping", [])
        if (m := re.fullmatch(r"z(\d+)_temp", mapping["field"]))
    ]
    if suggestion["zoneCount"] is None and zones:
        suggestion["zoneCount"] = max(zones)
    suggestion["dataZoneCount"] = max(zones) if zones else None
    return suggestion


def demo_archive():
    """Explicitly synthetic, reproducible training data; never labelled as plant measurements."""
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for cycle in range(5):
            out = io.StringIO()
            writer = csv.writer(out)
            writer.writerow(
                [
                    "时间",
                    "设定温度",
                    *[f"z{i}_temp" for i in range(1, 5)],
                    "燃气流量",
                    "助燃风流量",
                    "氧含量",
                    "窑压",
                ]
            )
            temp = 25.0
            for t in range(0, 1800, 5):
                sp = min(1120, 25 + t * 1.05) if t < 1300 else max(100, 1120 - (t - 1300) * 1.8)
                temp += (sp - temp) * (1 - math.exp(-5 / (26 + cycle)))
                gas = max(
                    0,
                    (
                        20000
                        * max(
                            0,
                            (
                                sp
                                - (
                                    min(1120, 25 + (t - 5) * 1.05)
                                    if t < 1300
                                    else max(100, 1120 - (t - 1305) * 1.8)
                                )
                            ),
                        )
                        * 12
                        + 0.3 * 3600 * max(sp - 25, 0)
                    )
                    / (0.75 * 36000),
                )
                gas *= 1 + 0.03 * math.sin(t / 47 + cycle)
                ratio = 11.1 + 0.5 * math.sin(t / 130) + cycle * 0.08
                writer.writerow(
                    [
                        (datetime(2026, 1, 1) + timedelta(days=cycle * 3, minutes=t)).isoformat(),
                        round(sp, 2),
                        *[round(temp + 2 * math.sin(t / 35 + i), 2) for i in range(1, 5)],
                        round(gas, 4),
                        round(gas * ratio, 4),
                        round(2.1 + 0.3 * math.sin(t / 130), 3),
                        round(-8 + 2 * math.sin(t / 90), 2),
                    ]
                )
            z.writestr(f"合成工况/cycle_{cycle + 1}.csv", out.getvalue().encode("utf-8-sig"))
        z.writestr(
            "设备说明.txt",
            (
                "合成演示数据，非现场实测，不证明生产精度。\n"
                "设备结构：4个有效温区，4个烧嘴。\n"
                "燃气低位热值36 MJ/Nm³；燃烧效率0.75；环境温度25℃。\n"
                "简化能量平衡：燃气输入=有效蓄热+等效散热。\n"
                "等效热容20000 kJ/K，等效散热系数0.3 kW/K。\n"
                "闭环温度响应时间常数约30分钟。\n"
                "空燃比为空气/燃气标况体积流量比，理论空燃比9.5，过量空气系数1.1。\n"
                "未提供排烟温度、物料质量和电量，不能计算完整热损失或综合能耗。"
            ).encode(),
        )
    return target.getvalue()
