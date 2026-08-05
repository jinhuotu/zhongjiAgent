from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from api.services.hot_config import DEFAULT_PROMPT_SYSTEM_BASE, get_system_prompt_base


async def build_system_prompt(
    db: AsyncSession,
    chunks: list[dict[str, Any]],
    *,
    long_memory: list[str] | None = None,
    use_knowledge: bool = True,
    governance_refs: list[dict[str, Any]] | None = None,
) -> str:
    """构建系统提示：热配置 Prompt + 可选长期记忆摘要 + RAG 片段 + 治理 Excel。"""
    base = await get_system_prompt_base(db)
    if not (base or "").strip():
        base = DEFAULT_PROMPT_SYSTEM_BASE

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
        if gov_block:
            return (
                f"{base}{memory_block}{gov_block}\n\n"
                "【说明】\n"
                "企业知识库开关已关闭，但上面「数据治理已导入 Excel 参考」仍然有效。"
                "请优先根据这些表格内容回答用户关于导入数据/窑烧成/点位数值的问题；"
                "不要说无法连接数据库或看不到导入数据。"
                "若表中没有足够行，可说明仅基于预览样例行作答。"
            )
        return (
            f"{base}{memory_block}\n\n【知识库】\n"
            "（本次用户关闭了知识库检索。请勿声称引用了企业知识库；"
            "仅基于工业窑炉通用工程经验与对话上下文作答。"
            "简短寒暄用 2～4 句即可，勿展开长文。）"
        )

    if not chunks:
        if gov_block:
            return (
                f"{base}{memory_block}{gov_block}\n\n"
                "【说明】知识库未命中片段，但已提供数据治理 Excel 预览。"
                "请优先用治理表格回答；不要声称无法查看导入数据。"
            )
        return (
            f"{base}{memory_block}{gov_block}\n\n【知识库参考片段】\n"
            "（已开启知识库，但本次未检索到足够相关的片段。"
            "请先明确告知「知识库暂未命中相关内容」，再基于通用经验作答，"
            "并标注「⚠️ 该结论非来自知识库」。）"
        )
    refs: list[str] = []
    for i, c in enumerate(chunks[:5], start=1):
        score = float(c.get("score") or 0.0)
        content = str(c.get("content") or "").strip()
        name = str(c.get("name") or "").strip() or "未命名资料"
        chunk_index = c.get("chunk_index")
        idx_part = f" 块序={chunk_index}" if chunk_index is not None else ""
        refs.append(f"[#{i} 资料={name}{idx_part} 相似度={score:.3f}]\n{content}")
    joined = "\n\n---\n\n".join(refs)
    return (
        f"{base}{memory_block}{gov_block}\n\n"
        f"【知识库参考片段（按相似度排序）】\n{joined}\n\n"
        "【本轮引用要求】\n"
        "优先使用以上片段回答当前问题；引用时标明片段编号（如参考片段 #1）；"
        "不要编造片段中不存在的条款、数值或步骤；无关片段请忽略。"
    )
