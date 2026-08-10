"""通用工具 MCP（stdio）：中国时间 + 天气查询。

供平台「MCP 管理」以 stdio 方式注册，供对话 / 场景智能体 / 工作流调用。

天气数据来自 Open-Meteo（无需 API Key）：
  https://open-meteo.com/

用法（手动探测）：
  poetry run python scripts/mcp_utility_server.py

注册到库（幂等）：
  poetry run python scripts/seed_mcp_utility.py
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("zhongji-utility")

_TZ_CN = ZoneInfo("Asia/Shanghai")

# WMO Weather interpretation codes (Open-Meteo)
_WEATHER_CODE_ZH: dict[int, str] = {
    0: "晴",
    1: "大部晴朗",
    2: "多云",
    3: "阴",
    45: "雾",
    48: "雾凇",
    51: "毛毛雨",
    53: "毛毛雨",
    55: "毛毛雨",
    56: "冻毛毛雨",
    57: "冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "雪粒",
    80: "阵雨",
    81: "阵雨",
    82: "强阵雨",
    85: "阵雪",
    86: "阵雪",
    95: "雷暴",
    96: "雷暴伴冰雹",
    99: "雷暴伴冰雹",
}


def _weather_label(code: int | None) -> str:
    if code is None:
        return "未知"
    return _WEATHER_CODE_ZH.get(int(code), f"天气码 {code}")


@mcp.tool()
def get_china_time() -> str:
    """查询当前中国标准时间（Asia/Shanghai，UTC+8）。"""
    now = datetime.now(_TZ_CN)
    weekday = ["一", "二", "三", "四", "五", "六", "日"][now.weekday()]
    payload = {
        "timezone": "Asia/Shanghai",
        "offset": "+08:00",
        "iso": now.isoformat(timespec="seconds"),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "weekday": weekday,
        "display": f"{now.strftime('%Y年%m月%d日 %H:%M:%S')}（星期{weekday}）",
    }
    return json.dumps(payload, ensure_ascii=False)


def _geocode_city(city: str) -> dict[str, Any]:
    name = (city or "").strip() or "北京"
    with httpx.Client(timeout=20.0) as client:
        resp = client.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": name, "count": 1, "language": "zh", "format": "json"},
        )
        resp.raise_for_status()
        data = resp.json()
    results = data.get("results") if isinstance(data, dict) else None
    if not results:
        raise ValueError(f"未找到城市：{name}")
    hit = results[0]
    return {
        "name": hit.get("name") or name,
        "country": hit.get("country"),
        "admin1": hit.get("admin1"),
        "latitude": hit.get("latitude"),
        "longitude": hit.get("longitude"),
        "timezone": hit.get("timezone") or "Asia/Shanghai",
    }


@mcp.tool()
def get_weather(city: str = "北京") -> str:
    """查询指定城市当前天气（默认北京）。数据来源 Open-Meteo，无需 API Key。

    Args:
        city: 城市名，如 北京、上海、郑州、Hangzhou
    """
    place = _geocode_city(city)
    lat = place["latitude"]
    lon = place["longitude"]
    with httpx.Client(timeout=20.0) as client:
        resp = client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m,apparent_temperature",
                "timezone": place.get("timezone") or "Asia/Shanghai",
                "forecast_days": 1,
            },
        )
        resp.raise_for_status()
        data = resp.json()
    current = (data or {}).get("current") or {}
    code = current.get("weather_code")
    payload = {
        "city": place["name"],
        "admin1": place.get("admin1"),
        "country": place.get("country"),
        "latitude": lat,
        "longitude": lon,
        "timezone": place.get("timezone"),
        "observedAt": current.get("time"),
        "temperatureC": current.get("temperature_2m"),
        "feelsLikeC": current.get("apparent_temperature"),
        "humidityPercent": current.get("relative_humidity_2m"),
        "windSpeedKmh": current.get("wind_speed_10m"),
        "weatherCode": code,
        "weather": _weather_label(code if code is None else int(code)),
        "source": "Open-Meteo",
        "display": (
            f"{place['name']}：{_weather_label(code if code is None else int(code))}，"
            f"{current.get('temperature_2m')}℃"
            f"（体感 {current.get('apparent_temperature')}℃），"
            f"湿度 {current.get('relative_humidity_2m')}%，"
            f"风速 {current.get('wind_speed_10m')} km/h"
        ),
    }
    return json.dumps(payload, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run(transport="stdio")
