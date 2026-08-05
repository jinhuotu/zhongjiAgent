"""知识库检索：向量召回 + 关键词重排（提升「停电」「禁令」等专名命中）。"""

from __future__ import annotations

import re
from typing import Any

_WS_RE = re.compile(r"\s+")


def keyword_overlap_score(query: str, content: str) -> float:
    """基于中文滑动窗口的关键词重合分，范围约 [0, 1]。"""
    q = _WS_RE.sub("", (query or "").strip())
    c = content or ""
    if not q or not c:
        return 0.0
    if q in c:
        return 1.0

    best = 0.0
    for n in (8, 6, 4, 2):
        if len(q) < n:
            continue
        step = max(1, n // 2)
        windows = list(range(0, len(q) - n + 1, step))
        if not windows:
            continue
        hit = sum(1 for i in windows if q[i : i + n] in c)
        best = max(best, hit / len(windows))
    return min(1.0, best)


def hybrid_rerank(
    query: str,
    hits: list[dict[str, Any]],
    *,
    top_k: int,
    keyword_weight: float,
) -> list[dict[str, Any]]:
    """对向量召回结果做关键词加权重排。"""
    w = max(0.0, min(1.0, float(keyword_weight)))
    ranked: list[dict[str, Any]] = []
    for h in hits:
        content = str(h.get("content") or "")
        vec = float(h.get("score") or 0.0)
        kw = keyword_overlap_score(query, content)
        final = (1.0 - w) * vec + w * kw
        item = dict(h)
        item["vector_score"] = vec
        item["keyword_score"] = round(kw, 4)
        item["score"] = round(final, 6)
        ranked.append(item)
    ranked.sort(key=lambda x: float(x.get("score") or 0.0), reverse=True)
    return ranked[: max(1, top_k)]
