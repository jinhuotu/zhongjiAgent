"""微信质检日报文字解析（无模型）。"""

from __future__ import annotations

from api.services.casting.wechat_daily import detect_kind, parse_wechat_daily_rules, strip_at_mentions

DAILY = """
谷瑞霞 8月14日验收情况 @张三 @李四
熔铸出AZS砖：25.4吨成23.0，成品率90.8%
不良前三项：1.裂纹 8%；2.缺棱角 1%；3.平整度 1%。
具体情况：
1. 裂纹 8%：26051#，33#PT，侧墙 6块，斜碹 5块，缺陷：裂纹
2. 缺棱角 1%：26051#，33#PT，斜碹 1块，缺陷：掉角
3. 平整度 1%：26051#，33#PT，侧墙 1块，缺陷：飘盖
加工环节：加工AZS 8.1吨成5.6，成品率88.9%
不良前三项：1.裂纹 25%；2.缺棱角 4%；3.夹腰 1%。
具体情况：
1. 裂纹 25%：33#WS 26032#，铺面 9块，喷嘴 3块；41#WS 26057#，池壁 3块，缺陷：裂纹
2. 缺棱角 4%：26032#，33#PT，斜碹 1块；33#WS，铺面 1块，喷嘴 1块，缺陷：掉角
3. 夹腰 1%：33#WS 26032#，铺面 1块，缺陷：双面夹腰
@宛泰 @Lee X.R. @龙龙龙 @明辉 @张保红 @冯会鹏 @ZLS
""".strip()

WEEKLY = """
上周验收情况（2026-08-08～2026-08-14）
砂型车间：4项，处理4项，处理率100%
熔铸车间：17项，处理13项，处理率76%
加工车间：23项，处理21项，处理率91%
熔铸：186.5吨成166.3，成品率89.2%
""".strip()


def test_parse_aug14_daily() -> None:
    parsed = parse_wechat_daily_rules(DAILY, default_year=2026, default_month=8)
    assert parsed["kind"] == "daily"
    assert parsed["reportDate"] == "2026-08-14"
    assert parsed["day"] == 14
    assert parsed["casting"]["inputTon"] == 25.4
    assert parsed["casting"]["passTon"] == 23.0
    assert parsed["casting"]["yieldRate"] == 90.8
    assert parsed["machining"]["inputTon"] == 8.1
    assert parsed["machining"]["yieldRate"] == 88.9
    assert parsed["firstRate"] == 90.8
    assert parsed["secondRate"] == 88.9
    assert parsed["firstDeviation"] == -9.2
    assert parsed["secondDeviation"] == -11.1
    assert parsed["taiShu"] == 25.4
    names = [d["name"] for d in parsed["casting"]["topDefects"]]
    assert names[:3] == ["裂纹", "缺棱角", "平整度"]
    assert "飘盖" in parsed["firstDetails"]
    assert "双面夹腰" in parsed["secondDetails"]
    assert parsed["heGeRate"] == 85.4  # (23+5.6)/(25.4+8.1)
    assert "@" not in parsed["firstDetails"]
    assert "@" not in parsed["secondDetails"]
    assert "龙龙龙" not in parsed["secondDetails"]
    assert "冯会鹏" not in parsed["secondDetails"]


def test_detect_weekly() -> None:
    assert detect_kind(WEEKLY) == "weekly"
    parsed = parse_wechat_daily_rules(WEEKLY, default_year=2026, default_month=8)
    assert parsed["kind"] == "weekly"


def test_yield_mismatch_warning() -> None:
    text = "8月1日验收情况\n熔铸出AZS砖：10吨成9，成品率50%\n加工环节：加工AZS 2吨成2，成品率100%"
    parsed = parse_wechat_daily_rules(text, default_year=2026, default_month=8)
    assert any("验算" in w for w in parsed["warnings"])
    assert parsed["firstRate"] == 50.0


def test_strip_at_mentions() -> None:
    raw = "具体情况：裂纹 1块 @Lee X.R. @龙龙龙 结束"
    cleaned = strip_at_mentions(raw)
    assert "@" not in cleaned
    assert "Lee" not in cleaned
    assert "龙龙龙" not in cleaned
    assert "裂纹 1块" in cleaned
    assert "结束" in cleaned
