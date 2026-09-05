"""脱棱角报表口径单测（无 MES）。"""

from __future__ import annotations

from api.services.casting.peel_report import (
    build_pivot,
    build_position_pie,
    classify_peel_position,
    code_list_is_peel_213,
)


def test_peel_code_213_only() -> None:
    assert code_list_is_peel_213("213")
    assert code_list_is_peel_213("213:脱棱角")
    assert code_list_is_peel_213("103:一个面裂,213:脱棱角")
    assert not code_list_is_peel_213("212")
    assert not code_list_is_peel_213("212:缺棱角")
    assert not code_list_is_peel_213("103:一个面裂")
    assert not code_list_is_peel_213("")
    assert not code_list_is_peel_213(None)


def test_position_prefers_mouth_over_face() -> None:
    assert classify_peel_position("铸口面脱角30+20+20") == "待口"
    assert classify_peel_position("待口脱棱角") == "待口"
    assert classify_peel_position("底部脱棱角30+30+20") == "底部"
    assert classify_peel_position("一个面裂兼脱棱") == "面"
    assert classify_peel_position("200*200上下面") == "面"
    assert classify_peel_position("磕碰掉边") == "其他"


def test_pivot_and_position_pie() -> None:
    daily = [
        {
            "CastDate": "2026-06-12",
            "FurnaceName": "北",
            "ShiftName": "甲",
            "BrickCnt": 10,
            "PeelCnt": 1,
        },
        {
            "CastDate": "2026-06-12",
            "FurnaceName": "北",
            "ShiftName": "乙",
            "BrickCnt": 10,
            "PeelCnt": 2,
        },
        {
            "CastDate": "2026-06-13",
            "FurnaceName": "南",
            "ShiftName": "乙",
            "BrickCnt": 20,
            "PeelCnt": 4,
        },
    ]
    pivot = build_pivot(daily)
    assert pivot["dates"] == ["2026-06-12", "2026-06-13"]
    assert pivot["furnaces"] == ["南", "北"]
    assert pivot["summary"]["brickCnt"] == 40
    assert pivot["summary"]["peelCnt"] == 7
    assert pivot["summary"]["peelRate"] == 17.5
    kinds = [r["kind"] for r in pivot["rows"]]
    assert kinds.count("shift") == 4
    assert kinds.count("furnaceTotal") == 2
    assert kinds.count("grandTotal") == 1
    assert kinds.count("shiftTotal") == 2
    assert kinds[-2:] == ["shiftTotal", "shiftTotal"]
    jia = next(r for r in pivot["rows"] if r["kind"] == "shiftTotal" and r["shiftName"] == "甲")
    yi = next(r for r in pivot["rows"] if r["kind"] == "shiftTotal" and r["shiftName"] == "乙")
    assert jia["total"]["brickCnt"] == 10
    assert jia["total"]["peelCnt"] == 1
    assert yi["total"]["brickCnt"] == 30
    assert yi["total"]["peelCnt"] == 6

    pie = build_position_pie(
        [
            {"CauseDesc": "铸口面脱角", "PeelCnt": 5},
            {"CauseDesc": "底部脱棱角", "PeelCnt": 3},
            {"CauseDesc": "上下面缺棱", "PeelCnt": 2},
        ]
    )
    names = [p["name"] for p in pie]
    assert names == ["待口", "底部", "面"]
    assert pie[0]["share"] == 50.0
