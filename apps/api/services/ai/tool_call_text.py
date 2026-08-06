"""从模型正文中解析/剥离非标准 tool call 标记（如豆包 DSML）。"""

from __future__ import annotations

import json
import re
import secrets
from typing import Any

# 豆包等模型可能用 ASCII `|` 或全角 `｜`(U+FF5C)，且常写成双竖线：
#   <|DSML|tool_calls>  或  <｜｜DSML｜｜tool_calls>
_PIPE_CHARS = "|\uff5c"


def normalize_dsml_delimiters(text: str) -> str:
    """把全角/双竖线 DSML 定界符统一成 <|DSML|...> / </|DSML|...>。"""
    if not text:
        return ""
    # 全角竖线 → ASCII
    out = text.replace("\uff5c", "|")
    # <||DSML||xxx> / <|DSML|xxx> → <|DSML|xxx>
    out = re.sub(r"<\|+\s*DSML\s*\|+", "<|DSML|", out, flags=re.IGNORECASE)
    out = re.sub(r"</\|+\s*DSML\s*\|+", "</|DSML|", out, flags=re.IGNORECASE)
    return out


def looks_like_dsml(text: str) -> bool:
    if not text:
        return False
    n = text.replace("\uff5c", "|")
    upper = n.upper()
    return "DSML|" in upper or "<|DSML" in upper or "||DSML" in upper


# <|DSML|tool_calls> ... </|DSML|tool_calls>
_DSML_BLOCK = re.compile(
    r"<\|DSML\|tool_calls\s*>(?P<body>.*?)</\|DSML\|tool_calls\s*>",
    re.DOTALL | re.IGNORECASE,
)
_DSML_INVOKE = re.compile(
    r"<\|DSML\|invoke\s+name=\"(?P<name>[^\"]+)\"\s*>(?P<body>.*?)</\|DSML\|invoke\s*>",
    re.DOTALL | re.IGNORECASE,
)
_DSML_PARAM = re.compile(
    r"<\|DSML\|parameter\s+name=\"(?P<name>[^\"]+)\"[^>]*>(?P<value>.*?)</\|DSML\|parameter\s*>",
    re.DOTALL | re.IGNORECASE,
)
# 不完整/变体标记（展示层兜底剥离）
_DSML_ANY = re.compile(
    r"<\|?\s*DSML\s*\|[^>]*>.*?(?:</\|?\s*DSML\s*\|[^>]*>|$)",
    re.DOTALL | re.IGNORECASE,
)
# 常见 XML/伪标签工具调用
_XML_FUNC = re.compile(
    r"<tool_call>\s*<name>(?P<name>[^<]+)</name>\s*<arguments>(?P<args>.*?)</arguments>\s*</tool_call>",
    re.DOTALL | re.IGNORECASE,
)


def strip_tool_call_markup(text: str) -> str:
    """去掉正文中的工具调用标记，仅保留自然语言。"""
    if not text:
        return ""
    out = normalize_dsml_delimiters(text)
    out = _DSML_BLOCK.sub("", out)
    out = _XML_FUNC.sub("", out)
    out = _DSML_ANY.sub("", out)
    # 清理残留单独标签行
    out = re.sub(r"<\|DSML\|[^>]*>", "", out, flags=re.IGNORECASE)
    out = re.sub(r"</\|DSML\|[^>]*>", "", out, flags=re.IGNORECASE)
    # 再清一遍可能残留的全角变体
    out = re.sub(
        rf"<[{re.escape(_PIPE_CHARS)}]+\s*DSML\s*[{re.escape(_PIPE_CHARS)}]+[^>]*>",
        "",
        out,
        flags=re.IGNORECASE,
    )
    out = re.sub(
        rf"</[{re.escape(_PIPE_CHARS)}]+\s*DSML\s*[{re.escape(_PIPE_CHARS)}]+[^>]*>",
        "",
        out,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def parse_tool_calls_from_content(content: str) -> tuple[str, list[dict[str, Any]]]:
    """若 content 内嵌 DSML/XML 工具调用，解析为 OpenAI tool_calls 并清洗正文。"""
    raw = normalize_dsml_delimiters(content or "")
    if not raw.strip():
        return "", []

    tool_calls: list[dict[str, Any]] = []

    for m in _DSML_BLOCK.finditer(raw):
        body = m.group("body") or ""
        for inv in _DSML_INVOKE.finditer(body):
            name = (inv.group("name") or "").strip()
            if not name:
                continue
            params: dict[str, Any] = {}
            for pm in _DSML_PARAM.finditer(inv.group("body") or ""):
                pname = (pm.group("name") or "").strip()
                pval = (pm.group("value") or "").strip()
                if not pname:
                    continue
                # 尝试 JSON 值，否则当字符串
                try:
                    params[pname] = json.loads(pval)
                except json.JSONDecodeError:
                    params[pname] = pval
            tool_calls.append(
                {
                    "id": f"dsml_{secrets.token_hex(8)}",
                    "name": name,
                    "arguments": json.dumps(params, ensure_ascii=False),
                }
            )

    if not tool_calls:
        for m in _XML_FUNC.finditer(raw):
            name = (m.group("name") or "").strip()
            args_raw = (m.group("args") or "").strip()
            if not name:
                continue
            try:
                args_obj = json.loads(args_raw) if args_raw else {}
            except json.JSONDecodeError:
                args_obj = {"input": args_raw}
            if not isinstance(args_obj, dict):
                args_obj = {"value": args_obj}
            tool_calls.append(
                {
                    "id": f"xml_{secrets.token_hex(8)}",
                    "name": name,
                    "arguments": json.dumps(args_obj, ensure_ascii=False),
                }
            )

    cleaned = strip_tool_call_markup(raw)
    return cleaned, tool_calls
