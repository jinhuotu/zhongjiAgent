"""预览不入库、可调整列映射和非有限数字处理。"""

import io
import zipfile
from datetime import datetime
from xml.sax.saxutils import escape

from common.kiln_xls import parse_upload, preview_upload


def workbook(rows):
    xml_rows = []
    for r, row in enumerate(rows, start=1):
        cells = []
        for c, value in enumerate(row):
            ref = chr(65 + c) + str(r)
            cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>')
        xml_rows.append("<row>" + "".join(cells) + "</row>")
    xml = (
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<sheetData>" + "".join(xml_rows) + "</sheetData></worksheet>"
    )
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as zf:
        zf.writestr("xl/worksheets/sheet1.xml", xml)
    return stream.getvalue()


def test_preview_and_auto_mapping_share_same_recognized_fields():
    blob = workbook(
        [
            ["时间", "设定温度", "一区温度", "燃气瞬时流量", "助燃风瞬时流量"],
            ["2026-10-01 08:00:00", 800, 790, 30, 400],
            ["2026-10-01 08:01:00", 810, 795, 31, 410],
        ]
    )
    preview = preview_upload("3#_1_20261001.xlsx", blob, None)[0]
    parsed = parse_upload("3#_1_20261001.xlsx", blob)[0]
    assert preview["status"] == parsed.status == "ok"
    assert preview["rows"] == len(parsed.rows) == 2
    assert preview["mapping"]["temp_sp"] == "0:1"
    assert parsed.rows[0]["ts"] == datetime(2026, 10, 1, 8)
    assert parsed.rows[0]["gas_flow_instant"] == 30
    assert parsed.rows[0]["z1_temp"] == 790
    assert next(f for f in preview["fields"] if f["key"] == "ts")["present"] == 2


def test_custom_mapping_supports_reordered_columns_and_does_not_change_source():
    blob = workbook(
        [
            ["累计数", "记录时刻", "工艺温度", "用气"],
            [50000, "2026-10-01 08:00:00", 850, 35],
            [50001, "2026-10-01 08:01:00", 860, 36],
        ]
    )
    original = bytes(blob)
    mapping = {"ts": "0:1", "temp_sp": "0:2", "gas_flow_instant": "0:3", "gas_flow_total": "0:0"}
    file = parse_upload("custom.xlsx", blob, "TC-05", {"custom.xlsx": mapping})[0]
    assert file.status == "ok"
    assert file.kiln_code == "TC-05"
    assert file.rows[0]["gas_flow_total"] == 50000
    assert file.rows[0]["temp_sp"] == 850
    assert blob == original


def test_invalid_and_duplicate_mappings_return_actionable_error():
    blob = workbook([["时间", "设定温度"], ["2026-10-01 08:00:00", 850]])
    for mapping in (
        {},
        {"temp_sp": "0:1"},
        {"ts": "0:0", "temp_sp": "0:0"},
        {"ts": "0:0", "temp_sp": "5:1"},
        {"ts": "0:0", "unsupported": "0:1"},
    ):
        file = parse_upload("custom.xlsx", blob, None, {"custom.xlsx": mapping})[0]
        assert file.status == "failed"
        assert not file.rows


def test_non_finite_cells_become_missing_values():
    blob = workbook(
        [
            ["时间", "设定温度", "燃气瞬时流量"],
            ["2026-10-01 08:00:00", "nan", "inf"],
        ]
    )
    row = parse_upload("3#_1_20261001.xlsx", blob)[0].rows[0]
    assert row["temp_sp"] is None
    assert row["gas_flow_instant"] is None
