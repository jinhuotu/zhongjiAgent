"""通用工具 MCP（stdio）：中国时间 + 当前/历史天气。

供平台「MCP 管理」以 stdio 方式注册，供对话 / 场景智能体 / 工作流调用。

天气数据来自 Open-Meteo（无需 API Key）：
  - 当前：https://api.open-meteo.com/
  - 历史：https://archive-api.open-meteo.com/v1/archive

厂区默认坐标（历史天气未传 lat/lon 时使用，可在 MCP Server env 配置）：
  FACTORY_LAT / FACTORY_LON / FACTORY_NAME

用法（手动探测）：
  poetry run python scripts/mcp_utility_server.py

注册到库（幂等）：
  poetry run python scripts/seed_mcp_utility.py
"""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("zhongji-utility")


def _cn_tz() -> tzinfo:
    """Asia/Shanghai；Windows 无系统时区库时回退固定 UTC+8（可 pip install tzdata）。"""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("Asia/Shanghai")
    except Exception:  # noqa: BLE001
        return timezone(timedelta(hours=8), name="UTC+8")


_TZ_CN = _cn_tz()
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MAX_SPAN_DAYS = 731  # 约 2 年，避免一次拉过大窗口
_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

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


def _parse_ymd(value: str, *, field: str) -> date:
    text = (value or "").strip()
    if not _DATE_RE.match(text):
        raise ValueError(f"{field} 须为 YYYY-MM-DD，收到：{value!r}")
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field} 不是合法日期：{value!r}") from exc


def _factory_location() -> dict[str, Any]:
    lat_raw = (os.environ.get("FACTORY_LAT") or "").strip()
    lon_raw = (os.environ.get("FACTORY_LON") or "").strip()
    name = (os.environ.get("FACTORY_NAME") or "").strip() or "厂区"
    if not lat_raw or not lon_raw:
        return {}
    try:
        return {
            "latitude": float(lat_raw),
            "longitude": float(lon_raw),
            "name": name,
        }
    except ValueError as exc:
        raise ValueError(
            f"FACTORY_LAT/FACTORY_LON 须为数字，当前 LAT={lat_raw!r} LON={lon_raw!r}"
        ) from exc


def _resolve_location(
    *,
    latitude: float | None,
    longitude: float | None,
    location_name: str | None,
) -> dict[str, Any]:
    factory = _factory_location()
    lat = latitude if latitude is not None else factory.get("latitude")
    lon = longitude if longitude is not None else factory.get("longitude")
    if lat is None or lon is None:
        raise ValueError(
            "请传入 latitude/longitude，或在 MCP Server 环境变量中配置 "
            "FACTORY_LAT / FACTORY_LON（可选 FACTORY_NAME）"
        )
    name = (location_name or "").strip() or str(factory.get("name") or "指定坐标")
    return {
        "latitude": float(lat),
        "longitude": float(lon),
        "name": name,
    }


def _parse_dates_arg(dates: str | None) -> list[date]:
    """支持逗号分隔或 JSON 数组字符串。"""
    raw = (dates or "").strip()
    if not raw:
        return []
    items: list[str]
    if raw.startswith("["):
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            raise ValueError("dates JSON 须为字符串数组")
        items = [str(x).strip() for x in parsed]
    else:
        items = [p.strip() for p in raw.replace(";", ",").split(",")]
    out: list[date] = []
    for item in items:
        if not item:
            continue
        out.append(_parse_ymd(item, field="dates"))
    return out


def _collect_target_dates(
    *,
    day: str | None,
    dates: str | None,
    start_date: str | None,
    end_date: str | None,
) -> list[date]:
    collected: list[date] = []
    if day:
        collected.append(_parse_ymd(day, field="date"))
    collected.extend(_parse_dates_arg(dates))

    start_raw = (start_date or "").strip()
    end_raw = (end_date or "").strip()
    if start_raw or end_raw:
        if not start_raw or not end_raw:
            raise ValueError("start_date 与 end_date 须同时提供")
        start = _parse_ymd(start_raw, field="start_date")
        end = _parse_ymd(end_raw, field="end_date")
        if end < start:
            raise ValueError("end_date 不能早于 start_date")
        span = (end - start).days + 1
        if span > _MAX_SPAN_DAYS:
            raise ValueError(f"日期跨度过大（{span} 天），上限 {_MAX_SPAN_DAYS} 天")
        cur = start
        while cur <= end:
            collected.append(cur)
            cur += timedelta(days=1)

    # 去重并排序
    unique = sorted(set(collected))
    if not unique:
        raise ValueError(
            "请至少提供 date、dates，或 start_date+end_date 之一"
        )
    if len(unique) > _MAX_SPAN_DAYS:
        raise ValueError(f"日期数量过多（{len(unique)}），上限 {_MAX_SPAN_DAYS}")
    return unique


def _http_client(*, timeout: float) -> httpx.Client:
    # Windows 下部分代理/抓包环境会导致 httpx SSL EOF；优先直连。
    return httpx.Client(timeout=timeout, trust_env=False, follow_redirects=True)


def _fetch_archive_daily(
    *,
    latitude: float,
    longitude: float,
    start: date,
    end: date,
    timezone: str,
) -> dict[str, Any]:
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "daily": ",".join(
            [
                "temperature_2m_max",
                "temperature_2m_min",
                "temperature_2m_mean",
                "relative_humidity_2m_mean",
                "weather_code",
            ]
        ),
        "timezone": timezone or "Asia/Shanghai",
    }
    try:
        with _http_client(timeout=45.0) as client:
            resp = client.get(_ARCHIVE_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as exc:
        raise ValueError(f"历史天气接口请求失败：{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("历史天气接口返回异常")
    if data.get("error"):
        raise ValueError(str(data.get("reason") or data.get("error")))
    return data


def _daily_rows(archive: dict[str, Any]) -> dict[str, dict[str, Any]]:
    daily = archive.get("daily") or {}
    if not isinstance(daily, dict):
        return {}
    times = daily.get("time") or []
    tmax = daily.get("temperature_2m_max") or []
    tmin = daily.get("temperature_2m_min") or []
    tmean = daily.get("temperature_2m_mean") or []
    humidity = daily.get("relative_humidity_2m_mean") or []
    codes = daily.get("weather_code") or daily.get("weathercode") or []

    rows: dict[str, dict[str, Any]] = {}
    for i, t in enumerate(times):
        key = str(t)[:10]
        code = codes[i] if i < len(codes) else None
        code_i = int(code) if code is not None else None
        rows[key] = {
            "date": key,
            "tempMaxC": tmax[i] if i < len(tmax) else None,
            "tempMinC": tmin[i] if i < len(tmin) else None,
            "tempMeanC": tmean[i] if i < len(tmean) else None,
            "humidityMean": humidity[i] if i < len(humidity) else None,
            "weatherCode": code_i,
            "weather": _weather_label(code_i),
        }
    return rows


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
    try:
        with _http_client(timeout=20.0) as client:
            resp = client.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": name, "count": 1, "language": "zh", "format": "json"},
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as exc:
        raise ValueError(f"地理编码请求失败：{exc}") from exc
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
    try:
        with _http_client(timeout=20.0) as client:
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
    except httpx.HTTPError as exc:
        raise ValueError(f"当前天气接口请求失败：{exc}") from exc
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


@mcp.tool()
def get_historical_weather(
    date: str | None = None,
    dates: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    timezone: str = "Asia/Shanghai",
    location_name: str | None = None,
) -> str:
    """按日期查询厂区（或指定坐标）历史日气温。用于同单良率分析补天气。

    数据来源：Open-Meteo Historical Archive（无需 API Key）。

    日期参数三选一（可组合，结果会去重）：
      - date: 单日 YYYY-MM-DD
      - dates: 多日，逗号分隔或 JSON 数组，如 "2024-01-01,2024-01-03" 或 '["2024-01-01"]'
      - start_date + end_date: 闭区间批量

    坐标：优先用参数 latitude/longitude；未传则用环境变量 FACTORY_LAT / FACTORY_LON。

    Args:
        date: 单日，如 2024-06-15
        dates: 多日列表字符串
        start_date: 区间起（含）
        end_date: 区间止（含）
        latitude: 纬度；默认读 FACTORY_LAT
        longitude: 经度；默认读 FACTORY_LON
        timezone: 时区，默认 Asia/Shanghai
        location_name: 位置显示名；默认 FACTORY_NAME
    """
    loc = _resolve_location(
        latitude=latitude,
        longitude=longitude,
        location_name=location_name,
    )
    tz = (timezone or "Asia/Shanghai").strip() or "Asia/Shanghai"
    targets = _collect_target_dates(
        day=date,
        dates=dates,
        start_date=start_date,
        end_date=end_date,
    )
    start = targets[0]
    end = targets[-1]
    # 历史库对「今天」可能尚未就绪，截断到昨天
    today = datetime.now(_TZ_CN).date()
    latest_ok = today - timedelta(days=1)
    if start > latest_ok:
        raise ValueError(
            f"历史天气最早可查到 {latest_ok.isoformat()}（不含今天及未来）"
        )
    if end > latest_ok:
        end = latest_ok
        targets = [d for d in targets if d <= latest_ok]
        if not targets:
            raise ValueError("过滤未来日期后无有效查询日")

    archive = _fetch_archive_daily(
        latitude=loc["latitude"],
        longitude=loc["longitude"],
        start=start,
        end=end,
        timezone=tz,
    )
    by_day = _daily_rows(archive)

    days_out: list[dict[str, Any]] = []
    missing: list[str] = []
    for d in targets:
        key = d.isoformat()
        row = by_day.get(key)
        if row is None:
            missing.append(key)
            continue
        days_out.append(row)

    means = [x["tempMeanC"] for x in days_out if x.get("tempMeanC") is not None]
    summary = {
        "dayCount": len(days_out),
        "missingCount": len(missing),
        "tempMeanMinC": min(means) if means else None,
        "tempMeanMaxC": max(means) if means else None,
        "tempMeanAvgC": round(sum(means) / len(means), 2) if means else None,
    }

    # 单日时保留扁平字段，方便工作流直接读
    flat: dict[str, Any] = {}
    if len(days_out) == 1:
        flat = {
            "date": days_out[0]["date"],
            "tempMaxC": days_out[0]["tempMaxC"],
            "tempMinC": days_out[0]["tempMinC"],
            "tempMeanC": days_out[0]["tempMeanC"],
            "humidityMean": days_out[0]["humidityMean"],
            "weatherCode": days_out[0]["weatherCode"],
            "weather": days_out[0]["weather"],
        }

    display_parts: list[str] = []
    for row in days_out[:8]:
        display_parts.append(
            f"{row['date']} 均温{row.get('tempMeanC')}℃"
            f"（{row.get('tempMinC')}~{row.get('tempMaxC')}℃，{row.get('weather')}）"
        )
    if len(days_out) > 8:
        display_parts.append(f"…共 {len(days_out)} 天")

    payload: dict[str, Any] = {
        "location": {
            "name": loc["name"],
            "latitude": loc["latitude"],
            "longitude": loc["longitude"],
        },
        "timezone": tz,
        "source": "Open-Meteo Historical Archive",
        "query": {
            "date": date,
            "dates": dates,
            "startDate": start_date,
            "endDate": end_date,
            "resolvedStart": start.isoformat(),
            "resolvedEnd": end.isoformat(),
        },
        "summary": summary,
        "missingDates": missing,
        "days": days_out,
        "display": f"{loc['name']}历史天气：" + "；".join(display_parts)
        if display_parts
        else f"{loc['name']}：无历史天气数据",
        **flat,
    }
    return json.dumps(payload, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run(transport="stdio")
