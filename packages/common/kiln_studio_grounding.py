"""Resolve model references to actual source text; never use fuzzy fact matching."""

from __future__ import annotations

import re

from common.kiln_studio_schema import AIInterpretation, SelectionEvidenceRef

_LAYOUT = re.compile(r"[\s\u200b\ufeff\u00ad]+|[^\s\u200b\ufeff\u00ad]")


def source_references(sources):
    """Short IDs are easy to select; canonical IDs remain internal to provenance."""
    references, lookup = [], {}
    for index, source in enumerate(sources, 1):
        alias = f"S{index}"
        references.append(
            {
                "sourceId": alias,
                "file": source["file"],
                "location": source["location"],
                "text": source["text"],
            }
        )
        lookup[alias] = source
        lookup[source["id"]] = source
    return references, lookup


def _layout_text(text):
    normalized, positions = [], []
    for part in _LAYOUT.finditer(text):
        value = part.group()
        if value.isspace() or all(char in "\u200b\ufeff\u00ad" or char.isspace() for char in value):
            # Do not accidentally turn two table cells '3 6' into the number '36'.
            if (
                normalized
                and normalized[-1].isdigit()
                and text[part.end() : part.end() + 1].isdigit()
            ):
                normalized.append(" ")
                positions.append(part.start())
        else:
            normalized.append(value)
            positions.append(part.start())
    return "".join(normalized), positions


def source_quote(text, proposed):
    if not proposed.strip():
        return text, "source_reference"
    if proposed in text:
        return proposed, "exact"
    normalized, positions = _layout_text(text)
    needle, _ = _layout_text(proposed)
    if not needle:
        return None, None
    start = normalized.find(needle)
    if start < 0:
        return None, None
    # Return the original characters, not the model's reformatted quote.
    return text[positions[start] : positions[start + len(needle) - 1] + 1], "layout_normalized"


def ground_evidence(parsed: AIInterpretation, lookup):
    verified, rejected = [], []
    seen = set()
    for evidence in parsed.evidence:
        reference = evidence.sourceId.strip()
        source = lookup.get(reference)
        if source is None:
            rejected.append({"sourceId": reference, "reason": "来源编号不存在于本次提供的资料中"})
            continue
        quote, verification = source_quote(source["text"], evidence.quote)
        if quote is None:
            rejected.append({"sourceId": reference, "reason": "引文与该来源原文不匹配，未采用"})
            continue
        identity = (source["id"], quote, evidence.reason)
        if identity in seen:
            continue
        seen.add(identity)
        verified.append(
            {
                "sourceId": source["id"],
                "referenceId": reference,
                "quote": quote,
                "reason": evidence.reason,
                "verification": verification,
                "source": source,
            }
        )
    return verified, rejected


def ground_selection_refs(
    ids: list[str], lookup: dict[str, SelectionEvidenceRef], mechanism_lookup
) -> tuple[list[SelectionEvidenceRef], bool]:
    """结构化事实按本次资产查表，机理引用从原文提取。"""
    verified = []
    rejected = False
    for reference in dict.fromkeys(ids):
        evidence = lookup.get(reference)
        if evidence is None:
            rejected = True
            continue
        if evidence.kind == "mechanism":
            source = mechanism_lookup.get(reference)
            if source is None:
                rejected = True
                continue
            quote, _ = source_quote(source["text"], "")
            if not quote or not quote.strip():
                rejected = True
                continue
            evidence = evidence.model_copy(
                update={"detail": quote, "file": source["file"], "location": source["location"]}
            )
        verified.append(evidence)
    return verified, rejected


def dataset_context(payload):
    """Aggregate all file coverage; avoid repeating 82 nearly identical dictionaries."""
    summaries = payload.get("summary", [])
    fields = {}
    for file in summaries:
        for field in file.get("fields", []):
            counts = fields.setdefault(field["key"], {"present": 0, "total": 0})
            counts["present"] += field["present"]
            counts["total"] += field["total"]
    total_rows = payload.get("rowCount", sum(file.get("count", 0) for file in summaries))
    for counts in fields.values():
        counts["total"] = total_rows
    return {
        "rowCount": total_rows,
        "fileCount": len(summaries),
        "fields": fields,
        "from": min((file["from"] for file in summaries), default=None),
        "to": max((file["to"] for file in summaries), default=None),
        "exampleFiles": [
            {key: file[key] for key in ("file", "from", "to", "count")} for file in summaries[:12]
        ],
        "exampleFileCount": min(12, len(summaries)),
        "scope": "字段覆盖统计汇总全部已确认文件，文件例子仅展示前12份；未修改训练数据",
    }
