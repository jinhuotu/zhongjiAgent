from __future__ import annotations

import re

from langchain_text_splitters import RecursiveCharacterTextSplitter

from common.config import get_settings

# 手册常见小节标题：8.3 厂区突发停电 / 第九章 安全操作禁令 / 6.2 冷却管控
_SECTION_HEAD_RE = re.compile(
    r"(?m)^(?:"
    r"第[一二三四五六七八九十百千零〇两\d]+[章节篇部]"
    r"|\d+(?:\.\d+){1,3}"
    r")[、.\s　].+$"
)


def _normalize(text: str) -> str:
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.strip() for line in cleaned.split("\n")]
    return "\n".join(line for line in lines if line)


def _split_by_sections(text: str) -> list[str]:
    """按章节标题切开，标题保留在对应段落开头，避免「停电」标题与正文拆散。"""
    matches = list(_SECTION_HEAD_RE.finditer(text))
    if len(matches) < 2:
        return [text]

    parts: list[str] = []
    # 文首到第一个标题
    if matches[0].start() > 0:
        head = text[: matches[0].start()].strip()
        if head:
            parts.append(head)

    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[m.start() : end].strip()
        if block:
            parts.append(block)
    return parts or [text]


def _split_oversized(block: str, *, chunk_size: int, chunk_overlap: int) -> list[str]:
    if len(block) <= chunk_size:
        return [block]
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=["\n\n", "\n", "。", "！", "？", "；", " ", ""],
    )
    # 超长块：尽量把首行标题带到每个子块
    first_line, _, rest = block.partition("\n")
    title = first_line.strip() if _SECTION_HEAD_RE.match(first_line.strip()) else ""
    pieces = [c.strip() for c in splitter.split_text(block) if c.strip()]
    if not title:
        return pieces
    out: list[str] = []
    for p in pieces:
        if p.startswith(title):
            out.append(p)
        else:
            out.append(f"{title}\n{p}")
    return out


def split_text(text: str) -> list[str]:
    settings = get_settings()
    cleaned = _normalize(text)
    if not cleaned.strip():
        return []

    chunk_size = settings.kb_chunk_size
    chunk_overlap = settings.kb_chunk_overlap
    sections = _split_by_sections(cleaned)

    chunks: list[str] = []
    for block in sections:
        chunks.extend(
            _split_oversized(block, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
        )

    return chunks or [cleaned[:chunk_size]]
