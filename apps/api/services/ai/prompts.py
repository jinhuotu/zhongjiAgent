from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


async def build_system_prompt(
    db: AsyncSession,
    chunks: list[dict[str, Any]],
    *,
    base_prompt: str | None = None,
    long_memory: list[str] | None = None,
    use_knowledge: bool = True,
    governance_refs: list[dict[str, Any]] | None = None,
) -> str | None:
    """构建系统提示。

    - base_prompt：对话页选中的提示词全文；未选则为空，不再回退热配置/默认文案。
    - 可按需拼接长期记忆 / 治理 Excel / 知识库片段。
    - 最终无可发送内容时返回 None（调用方不向 LLM 传 system）。
    """
    _ = db
    base = (base_prompt or "").strip()

    memory_block = ""
    if long_memory:
        lines = [f"- {s}" for s in long_memory if s.strip()]
        if lines:
            memory_block = "\n\n【本会话长期记忆摘要】\n" + "\n".join(lines)

    gov_block = ""
    if governance_refs:
        parts: list[str] = []
        for i, g in enumerate(governance_refs[:3], start=1):
            parts.append(
                f"[治理#{i} 任务={g.get('taskName') or ''} 文件={g.get('fileName') or ''}]\n"
                f"{g.get('content') or ''}"
            )
        gov_block = (
            "\n\n【数据治理已导入 Excel 参考】\n"
            + "\n\n---\n\n".join(parts)
            + "\n\n引用治理表格时请说明来自哪个治理任务/文件；不要编造表中不存在的数值。"
        )

    if not use_knowledge:
        if not base and not memory_block and not gov_block:
            return None
        if gov_block:
            text = (
                f"{base}{memory_block}{gov_block}\n\n"
                "【说明】\n"
                "企业知识库开关已关闭，但上面「数据治理已导入 Excel 参考」仍然有效。"
                "请优先根据这些表格内容回答用户关于导入数据/窑烧成/点位数值的问题；"
                "不要说无法连接数据库或看不到导入数据。"
                "若表中没有足够行，可说明仅基于预览样例行作答。"
            ).strip()
            return text or None
        text = f"{base}{memory_block}".strip()
        return text or None

    if not chunks:
        if not base and not memory_block and not gov_block:
            return None
        if gov_block:
            text = (
                f"{base}{memory_block}{gov_block}\n\n"
                "【说明】知识库未命中片段，但已提供数据治理 Excel 预览。"
                "请优先用治理表格回答；不要声称无法查看导入数据。"
            ).strip()
            return text or None
        text = (
            f"{base}{memory_block}{gov_block}\n\n【知识库参考片段】\n"
            "（已开启知识库，但本次未检索到足够相关的片段。"
            "请先明确告知「知识库暂未命中相关内容」，再基于通用经验作答，"
            "并标注「⚠️ 该结论非来自知识库」。）"
        ).strip()
        return text or None

    refs: list[str] = []
    for i, c in enumerate(chunks[:5], start=1):
        score = float(c.get("score") or 0.0)
        content = str(c.get("content") or "").strip()
        name = str(c.get("name") or "").strip() or "未命名资料"
        chunk_index = c.get("chunk_index")
        idx_part = f" 块序={chunk_index}" if chunk_index is not None else ""
        refs.append(f"[#{i} 资料={name}{idx_part} 相似度={score:.3f}]\n{content}")
    joined = "\n\n---\n\n".join(refs)
    text = (
        f"{base}{memory_block}{gov_block}\n\n"
        f"【知识库参考片段（按相似度排序）】\n{joined}\n\n"
        "【本轮引用要求】\n"
        "优先使用以上片段回答当前问题；引用时标明片段编号（如参考片段 #1）；"
        "不要编造片段中不存在的条款、数值或步骤；无关片段请忽略。"
    ).strip()
    return text or None
