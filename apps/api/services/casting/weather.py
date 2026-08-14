"""历史气温：Open-Meteo Archive（与 MCP get_historical_weather 同源）。"""

from __future__ import annotations

import os
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from common.logging import get_logger

logger = get_logger(__name__)

_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
_MAX_SPAN_DAYS = 366
_DAILY_FIELDS = ",".join(
    [
        "temperature_2m_max",
        "temperature_2m_min",
        "temperature_2m_mean",
        "relative_humidity_2m_mean",
        "weather_code",
    ]
)


def _cn_tz():
    try:
        return ZoneInfo("Asia/Shanghai")
    except Exception:  # noqa: BLE001
        return timezone(timedelta(hours=8))


def factory_location_from_env_map(env: dict[str, Any] | None) -> dict[str, Any] | None:
    """从 MCP Server.env 或进程环境读取厂区坐标。缺 lat/lon 则返回 None。"""
    data = env if isinstance(env, dict) else {}
    lat_raw = str(data.get("FACTORY_LAT") or "").strip()
    lon_raw = str(data.get("FACTORY_LON") or "").strip()
    name = str(data.get("FACTORY_NAME") or "").strip() or "厂区"
    if not lat_raw or not lon_raw:
        return None
    try:
        return {
            "latitude": float(lat_raw),
            "longitude": float(lon_raw),
            "name": name,
            "source": "mcp",
        }
    except (TypeError, ValueError):
        return None


def factory_location() -> dict[str, Any]:
    """进程环境 FACTORY_*；都未配时回退郑州示例（仅兜底）。"""
    from_env = factory_location_from_env_map(
        {
            "FACTORY_LAT": os.environ.get("FACTORY_LAT") or "",
            "FACTORY_LON": os.environ.get("FACTORY_LON") or "",
            "FACTORY_NAME": os.environ.get("FACTORY_NAME") or "",
        }
    )
    if from_env:
        from_env["source"] = "env"
        return from_env
    return {
        "latitude": 34.7466,
        "longitude": 113.6253,
        "name": "郑州厂区",
        "source": "default",
    }


def _tight_windows(unique: list[date]) -> list[tuple[date, date]]:
    """按自然年打包稀疏工序日，避免跨年连续拉满数百天空窗。"""
    by_year: dict[int, list[date]] = defaultdict(list)
    for d in unique:
        by_year[d.year].append(d)
    windows: list[tuple[date, date]] = []
    for year in sorted(by_year):
        days = sorted(by_year[year])
        start, end = days[0], days[-1]
        # 极端情况下同年跨度仍可能很大，再按窗口切
        cursor = start
        while cursor <= end:
            win_end = min(cursor + timedelta(days=_MAX_SPAN_DAYS - 1), end)
            windows.append((cursor, win_end))
            cursor = win_end + timedelta(days=1)
    return windows


def _empty_weather(
    *,
    name: str,
    lat: float,
    lon: float,
    display: str,
    missing: list[str] | None = None,
    error: str | None = None,
    loc_source: str = "mcp",
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "location": {
            "name": name,
            "latitude": lat,
            "longitude": lon,
            "source": loc_source,
        },
        "days": [],
        "summary": {"dayCount": 0},
        "missingDates": missing or [],
        "source": "Open-Meteo Historical Archive",
        "display": display,
    }
    if error:
        out["error"] = error
    return out


def _merge_daily(by_day: dict[str, dict[str, Any]], data: dict[str, Any]) -> None:
    daily = (data or {}).get("daily") or {}
    times = daily.get("time") or []
    tmax = daily.get("temperature_2m_max") or []
    tmin = daily.get("temperature_2m_min") or []
    tmean = daily.get("temperature_2m_mean") or []
    humidity = daily.get("relative_humidity_2m_mean") or []
    for i, t in enumerate(times):
        key = str(t)[:10]
        by_day[key] = {
            "date": key,
            "tempMaxC": tmax[i] if i < len(tmax) else None,
            "tempMinC": tmin[i] if i < len(tmin) else None,
            "tempMeanC": tmean[i] if i < len(tmean) else None,
            "humidityMean": humidity[i] if i < len(humidity) else None,
        }


def _finalize(
    *,
    name: str,
    lat: float,
    lon: float,
    timezone_name: str,
    unique: list[date],
    by_day: dict[str, dict[str, Any]],
    loc_source: str = "mcp",
) -> dict[str, Any]:
    days_out: list[dict[str, Any]] = []
    missing: list[str] = []
    for d in unique:
        key = d.isoformat()
        row = by_day.get(key)
        if row is None:
            missing.append(key)
        else:
            days_out.append(row)

    means = [x["tempMeanC"] for x in days_out if x.get("tempMeanC") is not None]
    summary = {
        "dayCount": len(days_out),
        "missingCount": len(missing),
        "tempMeanMinC": min(means) if means else None,
        "tempMeanMaxC": max(means) if means else None,
        "tempMeanAvgC": round(sum(means) / len(means), 2) if means else None,
    }
    preview = "；".join(
        f"{r['date']} 均温{r.get('tempMeanC')}℃" for r in days_out[:6]
    )
    if len(days_out) > 6:
        preview += f"…共{len(days_out)}天"
    return {
        "location": {
            "name": name,
            "latitude": lat,
            "longitude": lon,
            "source": loc_source,
        },
        "timezone": timezone_name,
        "source": "Open-Meteo Historical Archive",
        "summary": summary,
        "missingDates": missing,
        "days": days_out,
        "display": f"{name}历史天气：" + (preview or "无数据"),
    }


def fetch_historical_weather(
    *,
    dates: list[str],
    latitude: float | None = None,
    longitude: float | None = None,
    location_name: str | None = None,
    location: dict[str, Any] | None = None,
    timezone_name: str = "Asia/Shanghai",
) -> dict[str, Any]:
    """同步拉取（请在 asyncio.to_thread 中调用，避免阻塞 FastAPI 事件循环）。"""
    loc = location if isinstance(location, dict) else factory_location()
    lat = float(latitude if latitude is not None else loc["latitude"])
    lon = float(longitude if longitude is not None else loc["longitude"])
    name = (location_name or str(loc.get("name") or "厂区")).strip()
    loc_source = str(loc.get("source") or "mcp")

    parsed: list[date] = []
    for raw in dates:
        text = str(raw or "").strip()[:10]
        if len(text) < 10:
            continue
        try:
            parsed.append(date.fromisoformat(text))
        except ValueError:
            continue
    unique = sorted(set(parsed))
    if not unique:
        return _empty_weather(
            name=name, lat=lat, lon=lon, display="无有效日期", loc_source=loc_source
        )

    today = datetime.now(_cn_tz()).date()
    latest_ok = today - timedelta(days=1)
    unique = [d for d in unique if d <= latest_ok]
    if not unique:
        return _empty_weather(
            name=name,
            lat=lat,
            lon=lon,
            display="日期均不可用于历史查询（今天/未来）",
            missing=[d.isoformat() for d in parsed],
            loc_source=loc_source,
        )

    windows = _tight_windows(unique)
    logger.warning(
        "historical_weather start location=%s dates=%s windows=%s span=%s~%s",
        name,
        len(unique),
        len(windows),
        unique[0].isoformat(),
        unique[-1].isoformat(),
    )
    by_day: dict[str, dict[str, Any]] = {}
    try:
        with httpx.Client(timeout=45.0, trust_env=False, follow_redirects=True) as client:
            for idx, (start, end) in enumerate(windows, start=1):
                logger.warning(
                    "historical_weather window %s/%s %s~%s",
                    idx,
                    len(windows),
                    start.isoformat(),
                    end.isoformat(),
                )
                params = {
                    "latitude": lat,
                    "longitude": lon,
                    "start_date": start.isoformat(),
                    "end_date": end.isoformat(),
                    "daily": _DAILY_FIELDS,
                    "timezone": timezone_name or "Asia/Shanghai",
                }
                resp = client.get(_ARCHIVE_URL, params=params)
                resp.raise_for_status()
                _merge_daily(by_day, resp.json())
    except httpx.HTTPError as exc:
        logger.warning("historical weather fetch failed: %s", exc)
        return _empty_weather(
            name=name,
            lat=lat,
            lon=lon,
            display=f"天气查询失败：{exc}",
            error=str(exc),
            loc_source=loc_source,
        )

    result = _finalize(
        name=name,
        lat=lat,
        lon=lon,
        timezone_name=timezone_name,
        unique=unique,
        by_day=by_day,
        loc_source=loc_source,
    )
    logger.warning(
        "historical_weather done dayCount=%s missing=%s",
        (result.get("summary") or {}).get("dayCount"),
        (result.get("summary") or {}).get("missingCount"),
    )
    return result
