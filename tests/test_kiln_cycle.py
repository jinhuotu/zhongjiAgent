"""窑次表解析和四种气耗对照。不连数据库。"""

from __future__ import annotations

import io
import zipfile
from datetime import datetime, timedelta
from xml.sax.saxutils import escape

from common.kiln_predict import CycleSeries, build_workspace
from common.kiln_xls import parse_upload


def _points(start: datetime, bias: float = 0.0) -> list[dict]:
    points = []
    for index in range(41):
        hour = index * 0.5
        temp = 100 + 50 * hour if hour < 8 else 500
        slope = 50 if hour < 8 else 0
        gas = 0.2 * slope + 0.04 * max(temp - 40, 0) + bias
        points.append(
            {
                "ts": start + timedelta(hours=hour),
                "temp_sp": temp,
                "zone_temp": temp - 1,
                "gas": gas,
                "air": 100,
                "o2": 7.8,
                "o2_sp": 4,
                "afr": 10,
                "furnace_p": -4,
                "zones": [
                    {"name": "一区", "temp": temp, "gas": gas / 6, "air": 20, "valve": 35}
                ]
                * 6,
            }
        )
    return points


def _cycle(
    cycle_id: int,
    batch: str,
    points: list[dict],
    curve: str,
    load: float | None,
) -> CycleSeries:
    return CycleSeries(
        cycle_id=cycle_id,
        kiln_code="TC-03",
        batch_no=batch,
        source_file=f"3#_{batch}_20240517.xls",
        curve_version=curve,
        load_kg=load,
        points=points,
    )


def test_fusion_uses_other_cycles_and_keeps_this_one_out() -> None:
    start = datetime(2024, 5, 17, 8, 0, 0)
    target = _cycle(2, "2", _points(start, bias=4), "V3", 850)
    peer = _cycle(1, "1", _points(start - timedelta(days=3), bias=4), "V3", 860)
    result = build_workspace(target, [peer])

    assert result["trainCount"] == 1
    assert result["report"]["excluded"] == target.source_file
    by_key = {item["key"]: item for item in result["models"]}
    assert by_key["physics"]["available"] is True
    assert by_key["data"]["available"] is True
    assert by_key["similar"]["available"] is True
    assert by_key["fusion"]["available"] is True
    assert by_key["physics"]["mae"] is not None and by_key["physics"]["mae"] < 8
    assert by_key["fusion"]["mae"] <= by_key["physics"]["mae"] + 0.2
    assert abs(by_key["similar"]["totalGas"] - by_key["fusion"]["totalGas"]) < 80
    assert "对照" in result["report"]["scope"]
    assert not any("装载量未填写" in gap for gap in result["gaps"])
    assert result["points"]
    assert result["points"][0]["zones"][0]["name"] == "一区"


def test_missing_peers_and_params_are_called_out() -> None:
    start = datetime(2024, 5, 17, 8, 0, 0)
    target = _cycle(1, "1", _points(start), "", None)
    result = build_workspace(target, [])
    by_key = {item["key"]: item for item in result["models"]}
    assert by_key["similar"]["available"] is False
    assert by_key["data"]["available"] is False
    assert by_key["physics"]["available"] is True
    assert "调研报告" in by_key["physics"]["note"]
    text = " ".join(result["gaps"])
    assert "曲线版本未填写" in text
    assert "装载量未填写" in text
    assert "相似窑次" in text
    assert any("电耗未接入" in note for note in result["notes"])


def test_different_curve_version_is_not_a_similar_cycle() -> None:
    start = datetime(2024, 5, 17, 8, 0, 0)
    target = _cycle(2, "2", _points(start), "V2", 800)
    peer = _cycle(1, "1", _points(start - timedelta(days=2)), "V1", 800)
    result = build_workspace(target, [peer])
    by_key = {item["key"]: item for item in result["models"]}
    assert by_key["similar"]["available"] is False
    assert by_key["data"]["available"] is True


def _sheet(rows: list[list[str]]) -> str:
    body = []
    for row_no, row in enumerate(rows, start=1):
        cells = []
        for col_no, value in enumerate(row):
            ref = f"{chr(65 + col_no)}{row_no}"
            if value.startswith("="):
                cells.append(f'<c r="{ref}"><v>{value[1:]}</v></c>')
            else:
                cells.append(f'<c r="{ref}" t="s"><v>{value}</v></c>')
        body.append(f'<row r="{row_no}">{"".join(cells)}</row>')
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{''.join(body)}</sheetData></worksheet>"
    )


def test_zip_reads_kiln_workbook_and_skips_photos() -> None:
    strings = [
        "时间",
        "温度设定",
        "空燃比",
        "一区温度",
        "燃气流量",
        "助燃风流量",
        "阀位",
        "窑内压力",
    ]
    serial = (datetime(2024, 5, 17, 10, 0) - datetime(1899, 12, 30)).total_seconds() / 86400
    temp = _sheet(
        [
            ["0", "1", "2", "3", "4", "5", "6"],
            ["0", "1", "2", "3", "4", "5", "6"],
            [f"={serial}", "=800", "=10", "=790", "=4.5", "=40", "=30"],
        ]
    )
    pressure_header = [str(index) for index in range(8)]
    pressure_data = [f"={serial}"] + ["=1"] * 16
    pressure = _sheet([pressure_header, pressure_header, pressure_data])
    shared = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        + "".join(f"<si><t>{escape(item)}</t></si>" for item in strings)
        + "</sst>"
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="温度" sheetId="1" r:id="rId1"/>'
        '<sheet name="压力" sheetId="2" r:id="rId2"/></sheets></workbook>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://example/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="http://example/worksheet" Target="worksheets/sheet2.xml"/>'
        "</Relationships>"
    )
    book = io.BytesIO()
    with zipfile.ZipFile(book, "w") as zf:
        zf.writestr("xl/workbook.xml", workbook)
        zf.writestr("xl/_rels/workbook.xml.rels", rels)
        zf.writestr("xl/sharedStrings.xml", shared)
        zf.writestr("xl/worksheets/sheet1.xml", temp)
        zf.writestr("xl/worksheets/sheet2.xml", pressure)
    packed = io.BytesIO()
    with zipfile.ZipFile(packed, "w") as zf:
        zf.writestr("3#_2_20240517.xlsx", book.getvalue())
        zf.writestr("现场照片.jpg", b"not-a-photo")

    parsed = parse_upload("窑次.zip", packed.getvalue(), None)
    by_name = {item.filename: item for item in parsed}
    assert by_name["现场照片.jpg"].status == "skipped"
    table = by_name["3#_2_20240517.xlsx"]
    assert table.status == "ok"
    assert table.kiln_code == "TC-03"
    assert table.batch_no == "2"
    assert len(table.rows) == 1
    assert table.rows[0]["z1_temp"] == 790
    assert table.rows[0]["gas_flow_instant"] == 1
    assert table.rows[0]["o2_meas"] == 1
    assert table.rows[0]["ts"] == datetime(2024, 5, 17, 10, 0)
