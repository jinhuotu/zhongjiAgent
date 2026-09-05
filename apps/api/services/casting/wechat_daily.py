"""微信群「质检日报」文字解析：规则优先，缺字段时再走对话模型。"""

from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from common.errors import AppError, ErrorCode
from common.logging import get_logger

logger = get_logger(__name__)

_DATE_RE = re.compile(
    r"(?:(?P<y>20\d{2})\s*[年\-/])?\s*(?P<m>\d{1,2})\s*[月\-/]\s*(?P<d>\d{1,2})\s*日?"
)
_ISO_DATE_RE = re.compile(r"(20\d{2})-(\d{1,2})-(\d{1,2})")
_YIELD_RE = re.compile(
    r"(?P<input>\d+(?:\.\d+)?)\s*吨\s*成\s*(?P<pass>\d+(?:\.\d+)?)"
    r"\s*[，,]\s*成品率\s*(?P<rate>\d+(?:\.\d+)?)\s*%"
)
_DEFECT_RE = re.compile(
    r"(?P<idx>\d+)\s*[.、．]\s*(?P<name>[^：:\d%]{1,20}?)\s*(?P<rate>\d+(?:\.\d+)?)\s*%"
)
_WEEKLY_HINT = re.compile(
    r"周报|上周验收|砂型车间|质量事故|处理率|（20\d{2}-\d{2}-\d{2}\s*[~～\-至]\s*20\d{2}-\d{2}-\d{2}）"
)
_AT_MENTION_RE = re.compile(
    r"@+"
    r"(?:[\u200b\u200c\u200d\ufeff]*)"
    r"[^\s@]+"
    r"(?:[ \t\u00a0\u2005]+[A-Za-z][A-Za-z.]*)*"
)

_LLM_PROMPT = """你是耐材质检日报抽取助手。用户会粘贴微信工作群里的「某日验收情况」文字（可能夹杂@人和截图说明）。
只抽取【日报】；若是周报（上周、周报、砂型/熔铸/加工车间事故处理率），kind 填 weekly，不要把周汇总写进日报字段。
details 中不要保留任何 @人名（如 @张三、@Lee X.R.）。
只输出一个 JSON 对象，不要 Markdown：
{
  "kind": "daily" 或 "weekly" 或 "unknown",
  "reportDate": "YYYY-MM-DD 或 null",
  "casting": {
    "inputTon": 数字或null,
    "passTon": 数字或null,
    "yieldRate": 数字或null,
    "topDefects": [{"name": "裂纹", "rate": 8}],
    "details": "一检/熔铸具体情况原文，保留换行"
  },
  "machining": {
    "inputTon": 数字或null,
    "passTon": 数字或null,
    "yieldRate": 数字或null,
    "topDefects": [{"name": "裂纹", "rate": 25}],
    "details": "二检/加工具体情况原文，保留换行"
  }
}
yieldRate 为百分数，如 90.8 表示 90.8%。吨和成品率以原文数字为准，不要自己重算后改写。
"""


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        num = float(str(value).replace("%", "").strip())
    except (TypeError, ValueError):
        return None
    if num != num:  # NaN
        return None
    return num


def _round1(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value * 10) / 10.0


def _norm_space(text: str) -> str:
    return (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\u3000", " ")


def strip_at_mentions(text: str) -> str:
    """去掉微信 @人名，避免写入明细格。"""
    s = _AT_MENTION_RE.sub(" ", text or "")
    s = re.sub(r"[ \t\u00a0\u2005]{2,}", " ", s)
    s = re.sub(r" *\n *", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _empty_section() -> dict[str, Any]:
    return {
        "inputTon": None,
        "passTon": None,
        "yieldRate": None,
        "topDefects": [],
        "details": "",
    }


def detect_kind(text: str) -> str:
    raw = _norm_space(text)
    if _WEEKLY_HINT.search(raw) and not re.search(r"\d{1,2}月\d{1,2}日验收", raw):
        return "weekly"
    if _WEEKLY_HINT.search(raw) and re.search(r"上周|周报", raw):
        if not _YIELD_RE.search(raw):
            return "weekly"
        # 同时有周报口径和「吨成」时仍可能是日报+周报粘在一起，优先当 daily
        if re.search(r"\d{1,2}月\d{1,2}日", raw) and "加工" in raw:
            return "daily"
        return "weekly"
    if _YIELD_RE.search(raw) or re.search(r"熔铸|加工环节|验收情况", raw):
        return "daily"
    return "unknown"


def parse_report_date(text: str, *, default_year: int, default_month: int | None) -> str | None:
    iso = _ISO_DATE_RE.search(text)
    if iso:
        y, m, d = int(iso.group(1)), int(iso.group(2)), int(iso.group(3))
        return f"{y:04d}-{m:02d}-{d:02d}"
    hit = _DATE_RE.search(text)
    if not hit:
        return None
    y = int(hit.group("y") or default_year)
    m = int(hit.group("m"))
    d = int(hit.group("d"))
    if default_month and not hit.group("y") and m != default_month:
        # 仍采用正文月份，调用方用 warnings 提示
        pass
    if m < 1 or m > 12 or d < 1 or d > 31:
        return None
    return f"{y:04d}-{m:02d}-{d:02d}"


def _split_sections(text: str) -> tuple[str, str]:
    raw = _norm_space(text)
    mark = re.search(r"(加工环节|加工作业|加工AZS|加工检验|加工阶段)", raw)
    if not mark:
        return raw, ""
    return raw[: mark.start()], raw[mark.start() :]


def _parse_top_defects(block: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in _DEFECT_RE.finditer(block):
        name = re.sub(r"\s+", "", match.group("name")).strip("：:、-")
        if not name or name in seen:
            continue
        if name in ("具体情况", "不良前三项"):
            continue
        seen.add(name)
        out.append({"name": name, "rate": _round1(_as_float(match.group("rate")))})
        if len(out) >= 3:
            break
    return out


def _parse_details(block: str) -> str:
    hit = re.search(r"具体情况[:：]?\s*", block)
    if not hit:
        return ""
    rest = block[hit.end() :].strip()
    rest = re.split(r"\n\s*(加工环节|加工作业|加工AZS|质量事故)", rest, maxsplit=1)[0]
    lines = [ln.strip() for ln in rest.split("\n") if ln.strip()]
    return "\n".join(lines)


def _parse_section(block: str) -> dict[str, Any]:
    sec = _empty_section()
    if not (block or "").strip():
        return sec
    y = _YIELD_RE.search(block)
    if y:
        sec["inputTon"] = _round1(_as_float(y.group("input")))
        sec["passTon"] = _round1(_as_float(y.group("pass")))
        sec["yieldRate"] = _round1(_as_float(y.group("rate")))
    sec["topDefects"] = _parse_top_defects(block)
    sec["details"] = _parse_details(block)
    return sec


def _yield_warning(label: str, sec: dict[str, Any]) -> str | None:
    inp = sec.get("inputTon")
    pas = sec.get("passTon")
    rate = sec.get("yieldRate")
    if not inp or inp <= 0 or pas is None or rate is None:
        return None
    expected = _round1(pas / inp * 100)
    if expected is None:
        return None
    if abs(expected - rate) > 0.8:
        return f"{label}成品率原文 {rate}% ，按吨验算约 {expected}%，已保留原文"
    return None


def _section_complete(sec: dict[str, Any]) -> bool:
    return sec.get("yieldRate") is not None and sec.get("inputTon") is not None


def parse_wechat_daily_rules(
    text: str,
    *,
    default_year: int,
    default_month: int | None = None,
) -> dict[str, Any]:
    raw = strip_at_mentions(_norm_space(text)).strip()
    kind = detect_kind(raw)
    report_date = parse_report_date(raw, default_year=default_year, default_month=default_month)
    casting_blk, mach_blk = _split_sections(raw)
    casting = _parse_section(casting_blk)
    machining = _parse_section(mach_blk)
    warnings: list[str] = []
    for label, sec in (("熔铸/一检", casting), ("加工/二检", machining)):
        w = _yield_warning(label, sec)
        if w:
            warnings.append(w)
    if report_date and default_month:
        month = int(report_date[5:7])
        if month != default_month:
            warnings.append(f"正文日期是 {report_date}，与当前选择月份不一致")
    overall = None
    cin, cpass = casting.get("inputTon"), casting.get("passTon")
    minp, mpass = machining.get("inputTon"), machining.get("passTon")
    if cin and minp and cpass is not None and mpass is not None and (cin + minp) > 0:
        overall = _round1((cpass + mpass) / (cin + minp) * 100)
    first = casting.get("yieldRate")
    second = machining.get("yieldRate")
    return {
        "kind": kind,
        "reportDate": report_date,
        "year": int(report_date[:4]) if report_date else default_year,
        "month": int(report_date[5:7]) if report_date else default_month,
        "day": int(report_date[8:10]) if report_date else None,
        "casting": casting,
        "machining": machining,
        "taiShu": cin,
        "heGeRate": overall if overall is not None else first,
        "firstRate": first,
        "secondRate": second,
        "recycleRate": 100.0,
        "targetRate": 100.0,
        "firstDeviation": _round1(first - 100) if first is not None else None,
        "secondDeviation": _round1(second - 100) if second is not None else None,
        "firstDetails": strip_at_mentions(casting.get("details") or ""),
        "secondDetails": strip_at_mentions(machining.get("details") or ""),
        "warnings": warnings,
        "parseSource": "rules",
    }


def _merge_llm(base: dict[str, Any], llm: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    if isinstance(llm.get("kind"), str) and llm["kind"] in ("daily", "weekly", "unknown"):
        if base.get("kind") == "unknown":
            out["kind"] = llm["kind"]
    if not out.get("reportDate") and llm.get("reportDate"):
        out["reportDate"] = str(llm["reportDate"])[:10]
        try:
            out["year"] = int(out["reportDate"][:4])
            out["month"] = int(out["reportDate"][5:7])
            out["day"] = int(out["reportDate"][8:10])
        except (TypeError, ValueError):
            pass
    for key in ("casting", "machining"):
        src = llm.get(key) if isinstance(llm.get(key), dict) else {}
        dst = dict(out.get(key) or _empty_section())
        for field in ("inputTon", "passTon", "yieldRate"):
            if dst.get(field) is None and src.get(field) is not None:
                dst[field] = _round1(_as_float(src.get(field)))
        if not dst.get("details") and src.get("details"):
            dst["details"] = strip_at_mentions(str(src["details"]))
        elif dst.get("details"):
            dst["details"] = strip_at_mentions(str(dst["details"]))
        if not dst.get("topDefects") and isinstance(src.get("topDefects"), list):
            dst["topDefects"] = [
                {"name": str(x.get("name") or "").strip(), "rate": _round1(_as_float(x.get("rate")))}
                for x in src["topDefects"]
                if isinstance(x, dict) and str(x.get("name") or "").strip()
            ][:3]
        out[key] = dst
    casting, machining = out["casting"], out["machining"]
    out["taiShu"] = casting.get("inputTon")
    first = casting.get("yieldRate")
    second = machining.get("yieldRate")
    cin, cpass = casting.get("inputTon"), casting.get("passTon")
    minp, mpass = machining.get("inputTon"), machining.get("passTon")
    overall = None
    if cin and minp and cpass is not None and mpass is not None and (cin + minp) > 0:
        overall = _round1((cpass + mpass) / (cin + minp) * 100)
    out["heGeRate"] = overall if overall is not None else first
    out["firstRate"] = first
    out["secondRate"] = second
    out["firstDeviation"] = _round1(first - 100) if first is not None else None
    out["secondDeviation"] = _round1(second - 100) if second is not None else None
    out["firstDetails"] = strip_at_mentions(casting.get("details") or "")
    out["secondDetails"] = strip_at_mentions(machining.get("details") or "")
    return out


def _strip_json_fence(text: str) -> str:
    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\s*```$", "", s)
    start = s.find("{")
    end = s.rfind("}")
    if start >= 0 and end > start:
        return s[start : end + 1]
    return s


def _rules_need_llm(parsed: dict[str, Any]) -> bool:
    if parsed.get("kind") == "weekly":
        return False
    if not parsed.get("reportDate"):
        return True
    if not _section_complete(parsed.get("casting") or {}):
        return True
    return False


async def parse_wechat_daily(
    db: AsyncSession,
    *,
    text: str,
    default_year: int,
    default_month: int | None = None,
    use_llm: bool = True,
) -> dict[str, Any]:
    raw = (text or "").strip()
    if len(raw) < 8:
        raise AppError(ErrorCode.VALIDATION, "请粘贴群聊日报文字", status_code=422)
    if len(raw) > 20000:
        raise AppError(ErrorCode.VALIDATION, "粘贴内容过长（上限 2 万字）", status_code=422)

    parsed = parse_wechat_daily_rules(
        raw, default_year=default_year, default_month=default_month
    )
    if parsed["kind"] == "weekly":
        parsed["warnings"] = [
            *(parsed.get("warnings") or []),
            "识别为周报/周汇总，未写入日列。请粘贴单日「验收情况」文字。",
        ]
        parsed["parseSource"] = "rules"
        return parsed

    if use_llm and _rules_need_llm(parsed):
        try:
            from api.services.models.runtime import build_llm_client

            client = await build_llm_client(db, "fast")
            content = await client.complete(
                [
                    {"role": "system", "content": _LLM_PROMPT},
                    {"role": "user", "content": strip_at_mentions(raw)[:12000]},
                ],
                mode="fast",
            )
            llm_obj = json.loads(_strip_json_fence(content))
            if isinstance(llm_obj, dict):
                parsed = _merge_llm(parsed, llm_obj)
                parsed["parseSource"] = "rules+llm"
        except AppError:
            parsed.setdefault("warnings", []).append("模型解析未成功，已保留规则抽取结果")
        except Exception:  # noqa: BLE001
            logger.exception("wechat daily llm parse failed")
            parsed.setdefault("warnings", []).append("模型解析失败，已保留规则抽取结果")

    if parsed.get("kind") != "weekly" and not parsed.get("day"):
        parsed.setdefault("warnings", []).append("未能识别日期，请在正文中写明如「8月14日」")
    return parsed
