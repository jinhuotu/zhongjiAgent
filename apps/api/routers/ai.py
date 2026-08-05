from __future__ import annotations

import asyncio
import json
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from api.deps import CurrentUser, DbSession
from api.services.ai import memory as memory_svc
from api.services.ai import sessions as sessions_svc
from api.services.ai.long_memory import recall_long_memory
from api.services.ai.prompts import build_system_prompt
from api.services.governance import search_for_chat as search_governance
from api.services.knowledge.ingest import search_chunks
from api.services.models.runtime import build_llm_client
from common.config import get_settings
from common.errors import AppError, ErrorCode
from common.logging import get_logger
from common.redis_tools import check_sliding_rate_limit, session_lock
from common.response import ok

router = APIRouter(prefix="/ai", tags=["ai"])
logger = get_logger(__name__)


class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "system", "tool"] = "user"
    content: str


class ChatRequest(BaseModel):
    """优先 content + sessionId；messages 仅兼容旧客户端（取最后一条 user）。"""

    content: str | None = Field(default=None, max_length=20000)
    messages: list[ChatMessage] | None = None
    mode: Literal["fast", "deep"] = "fast"
    sessionId: str = Field(min_length=1, max_length=32)
    # 兼容旧字段；实际以 knowledgeBaseIds 为准：未选 = 不检索
    useKnowledge: bool = False
    knowledgeBaseIds: list[str] = Field(default_factory=list, max_length=32)


class RelatedRequest(BaseModel):
    question: str
    answer: str = ""


class CreateSessionRequest(BaseModel):
    title: str | None = Field(default=None, max_length=128)
    mode: Literal["fast", "deep"] = "fast"


class UpdateSessionRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=128)
    mode: Literal["fast", "deep"] | None = None


@router.get("/status")
async def ai_status(db: DbSession, user: CurrentUser) -> dict:
    _ = user
    from api.services.models import configs as model_configs

    runtime = await model_configs.runtime_status(db)
    return ok(
        {
            "llm_configured": runtime["llm_configured"],
            "embedding_configured": runtime["embedding_configured"],
            "llm_fast": runtime["llm_fast"],
            "llm_deep": runtime["llm_deep"],
            "embedding": runtime["embedding"],
            "source": "model_configs",
        }
    )


@router.get("/sessions")
async def sessions_list(db: DbSession, user: CurrentUser) -> dict:
    items = await sessions_svc.list_sessions(db, user_id=user.id)
    return ok({"items": items})


@router.post("/sessions")
async def sessions_create(body: CreateSessionRequest, db: DbSession, user: CurrentUser) -> dict:
    item = await sessions_svc.create_session(
        db,
        user_id=user.id,
        title=body.title,
        mode=body.mode,
    )
    return ok({"item": item})


@router.get("/sessions/{session_id}")
async def sessions_get(session_id: str, db: DbSession, user: CurrentUser) -> dict:
    item = await sessions_svc.get_session_item_merged(
        db, public_id=session_id, user_id=user.id
    )
    return ok({"item": item})


@router.patch("/sessions/{session_id}")
async def sessions_update(
    session_id: str,
    body: UpdateSessionRequest,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    item = await sessions_svc.update_session(
        db,
        public_id=session_id,
        user_id=user.id,
        title=body.title,
        mode=body.mode,
    )
    return ok({"item": item})


@router.delete("/sessions/{session_id}")
async def sessions_delete(session_id: str, db: DbSession, user: CurrentUser) -> dict:
    await sessions_svc.delete_session(db, public_id=session_id, user_id=user.id)
    return ok({"deleted": True})


@router.post("/sessions/{session_id}/summarize-title")
async def sessions_summarize_title(session_id: str, db: DbSession, user: CurrentUser) -> dict:
    item = await sessions_svc.summarize_session_title(
        db,
        public_id=session_id,
        user_id=user.id,
        force=True,
    )
    return ok({"item": item})


def _extract_user_content(body: ChatRequest) -> str:
    if body.content and body.content.strip():
        return body.content.strip()
    if body.messages:
        last_user = next((m for m in reversed(body.messages) if m.role == "user"), None)
        if last_user and last_user.content.strip():
            return last_user.content.strip()
    raise AppError(ErrorCode.VALIDATION, "content（本轮用户消息）不能为空", status_code=422)


@router.post("/chat")
async def ai_chat(body: ChatRequest, db: DbSession, user: CurrentUser) -> EventSourceResponse:
    """SSE 对话：Redis 热窗口 + 限流 + 会话锁 + 滚动裁剪 + 长期记忆召回。"""
    settings = get_settings()
    await check_sliding_rate_limit(scope="chat", subject=str(user.id))
    await build_llm_client(db, body.mode)
    user_text = _extract_user_content(body)

    session = await sessions_svc.get_session_for_user(
        db,
        public_id=body.sessionId,
        user_id=user.id,
        with_messages=False,
    )

    async def event_generator():  # noqa: ANN202
        async with session_lock(session.public_id, wait_seconds=20.0):
            await memory_svc.ensure_hot_context(db, session=session)

            user_hot = memory_svc.build_hot_message(
                role="user",
                content=user_text,
                mode=body.mode,
                knowledge_base_ids=[
                    str(x).strip()
                    for x in (body.knowledgeBaseIds or [])
                    if str(x).strip()
                ][:32],
            )
            await memory_svc.append_hot_and_enqueue(session=session, message=user_hot)

            chunks: list[dict[str, Any]] = []
            kb_ids = [
                str(x).strip()
                for x in (body.knowledgeBaseIds or [])
                if str(x).strip()
            ][:32]
            await memory_svc.bump_session_meta(
                db,
                session=session,
                mode=body.mode,
                knowledge_base_ids=kb_ids,
            )
            # 未选知识库 = 不检索；不再支持「开开关却全库扫描」
            use_knowledge = len(kb_ids) > 0
            if use_knowledge:
                try:
                    chunks = await search_chunks(
                        db,
                        query=user_text,
                        top_k=settings.kb_search_top_k,
                        min_score=settings.kb_search_min_score,
                        kb_ids=kb_ids,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("rag search failed: %s", exc)
                    chunks = []

            gov_refs: list[dict[str, Any]] = []
            try:
                gov_refs = await search_governance(db, user_text, top_k=3)
            except Exception as exc:  # noqa: BLE001
                logger.warning("governance search failed: %s", exc)
                gov_refs = []

            # 长期记忆依赖 Embedding；未选知识库时跳过，避免每次对话多等一轮远程向量
            long_mem: list[str] = []
            if use_knowledge:
                try:
                    long_mem = await asyncio.wait_for(
                        recall_long_memory(
                            db,
                            user_id=user.id,
                            query=user_text,
                            session_id=session.public_id,
                            top_k=3,
                        ),
                        timeout=3.0,
                    )
                except asyncio.TimeoutError:
                    logger.warning("long memory recall timed out (>3s), skip")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("long memory recall failed: %s", exc)

            yield {
                "event": "refs",
                "data": json.dumps(
                    {
                        "mode": body.mode,
                        "chunks": chunks,
                        "governance": gov_refs,
                        "useKnowledge": use_knowledge,
                        "knowledgeBaseIds": kb_ids,
                    },
                    ensure_ascii=False,
                ),
            }

            hot_msgs = await memory_svc.load_hot_messages(session.public_id)
            system_prompt = await build_system_prompt(
                db,
                chunks,
                long_memory=long_mem,
                use_knowledge=use_knowledge,
                governance_refs=gov_refs,
            )
            llm_messages = [
                {"role": "system", "content": system_prompt},
                *memory_svc.hot_messages_for_llm(hot_msgs),
            ]

            accumulated = ""
            try:
                client = await build_llm_client(db, body.mode)
                async for text in client.stream_chat(llm_messages, mode=body.mode):
                    accumulated += text
                    yield {
                        "event": "delta",
                        "data": json.dumps({"text": text}, ensure_ascii=False),
                    }

                title: str | None = None
                if accumulated.strip():
                    assistant_hot = memory_svc.build_hot_message(
                        role="assistant",
                        content=accumulated,
                        mode=body.mode,
                        refs=chunks,
                        knowledge_base_ids=kb_ids,
                        model_name=getattr(client, "fixed_model", None),
                    )
                    await memory_svc.append_hot_and_enqueue(
                        session=session, message=assistant_hot
                    )
                    await memory_svc.bump_session_meta(
                        db,
                        session=session,
                        mode=body.mode,
                        knowledge_base_ids=kb_ids,
                    )
                    try:
                        await memory_svc.maybe_roll_trim(db, session=session)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("roll trim failed: %s", exc)
                    title = await sessions_svc.maybe_auto_title(db, session=session)

                done_payload: dict[str, Any] = {
                    "ok": True,
                    "sessionId": session.public_id,
                }
                if title:
                    done_payload["title"] = title
                yield {"event": "done", "data": json.dumps(done_payload, ensure_ascii=False)}
            except AppError as exc:
                yield {
                    "event": "error",
                    "data": json.dumps({"msg": exc.msg}, ensure_ascii=False),
                }
            except Exception as exc:  # noqa: BLE001
                logger.exception("ai chat failed")
                yield {
                    "event": "error",
                    "data": json.dumps({"msg": str(exc)}, ensure_ascii=False),
                }

    return EventSourceResponse(event_generator())


@router.post("/chat/related")
async def ai_chat_related(body: RelatedRequest, db: DbSession, user: CurrentUser) -> dict:
    _ = user
    prompt = (
        "你是工业燃气车式窑（车底炉）领域专家。基于用户的「原问题」和「AI 回答」，"
        "生成 4 条用户可能进一步追问的相关问题。要求：\n"
        "1. 每条不超过 30 字；\n"
        "2. 紧扣窑炉/能碳/燃烧控制/工艺；\n"
        "3. 不要重复原问题；\n"
        "4. 只输出 JSON 数组字符串，例如 [\"问题1\",\"问题2\",\"问题3\",\"问题4\"]。"
    )
    messages = [
        {"role": "system", "content": prompt},
        {
            "role": "user",
            "content": f"原问题：{body.question}\n\nAI 回答：{body.answer[:2000]}",
        },
    ]
    client = await build_llm_client(db, "fast")
    raw = await client.complete(messages, mode="fast")
    questions: list[str] = []
    try:
        start = raw.find("[")
        end = raw.rfind("]")
        if start >= 0 and end > start:
            parsed = json.loads(raw[start : end + 1])
            if isinstance(parsed, list):
                questions = [str(x).strip() for x in parsed if str(x).strip()][:4]
    except json.JSONDecodeError:
        questions = []
    return {"code": 0, "msg": "ok", "data": {"questions": questions}, "questions": questions}


# ---------- AI 智能报告 ----------


class GenerateReportRequest(BaseModel):
    type: Literal["fault", "forecast", "efficiency", "carbon"]
    furnaceId: str = Field(min_length=1, max_length=32)
    mode: Literal["fast", "deep"] = "deep"


@router.get("/reports/types")
async def reports_types(user: CurrentUser) -> dict:
    _ = user
    from api.services.ai import reports as reports_svc

    return ok({"items": reports_svc.list_report_types()})


@router.get("/reports")
async def reports_list(
    db: DbSession,
    user: CurrentUser,
    type: str | None = None,
    limit: int = 50,
) -> dict:
    from api.services.ai import reports as reports_svc

    items = await reports_svc.list_reports(
        db,
        user_id=user.id,
        report_type=type,
        limit=limit,
    )
    return ok({"items": items})


@router.get("/reports/{report_id}")
async def reports_get(report_id: str, db: DbSession, user: CurrentUser) -> dict:
    from api.services.ai import reports as reports_svc

    item = await reports_svc.get_report(db, public_id=report_id, user_id=user.id)
    return ok({"item": item})


@router.delete("/reports/{report_id}")
async def reports_delete(report_id: str, db: DbSession, user: CurrentUser) -> dict:
    from api.services.ai import reports as reports_svc

    await reports_svc.delete_report(db, public_id=report_id, user_id=user.id)
    return ok({"deleted": True})


@router.post("/reports/generate")
async def reports_generate(
    body: GenerateReportRequest,
    db: DbSession,
    user: CurrentUser,
) -> EventSourceResponse:
    from api.services.ai import reports as reports_svc

    if body.type not in reports_svc.REPORT_TYPES:
        raise AppError(ErrorCode.VALIDATION, "invalid report type", status_code=422)

    # 预检 LLM
    await build_llm_client(db, body.mode)

    ctx = await reports_svc.build_context(db, kiln_code=body.furnaceId.strip())
    furnace = ctx["furnace"]
    kiln_name = str(furnace.get("name") or body.furnaceId)
    type_name = reports_svc.REPORT_TYPES[body.type]["name"]
    title = f"{type_name} · {kiln_name}"

    draft = await reports_svc.create_draft(
        db,
        user_id=user.id,
        report_type=body.type,
        kiln_code=body.furnaceId.strip(),
        kiln_name=kiln_name,
        mode=body.mode,
        title=title,
        context_summary=ctx["contextText"][:4000],
    )

    async def event_generator():  # noqa: ANN202
        accumulated = ""
        chunks: list[dict[str, Any]] = []
        try:
            chunks = await reports_svc.search_report_chunks(
                db,
                report_type=body.type,
                kiln_name=kiln_name,
            )
            yield {
                "event": "meta",
                "data": json.dumps(
                    {
                        "reportId": draft.public_id,
                        "type": body.type,
                        "furnaceId": body.furnaceId,
                        "furnaceName": kiln_name,
                        "mode": body.mode,
                        "refs": len(chunks),
                        "hasSamples": bool(ctx.get("hasSamples")),
                    },
                    ensure_ascii=False,
                ),
            }

            messages = reports_svc.build_prompt(
                report_type=body.type,
                context_text=ctx["contextText"],
                chunks=chunks,
            )
            client = await build_llm_client(db, body.mode)
            async for text in client.stream_chat(messages, mode=body.mode):
                accumulated += text
                yield {
                    "event": "delta",
                    "data": json.dumps({"text": text}, ensure_ascii=False),
                }

            await reports_svc.finalize_success(
                db,
                report=draft,
                content=accumulated,
                refs=chunks,
            )
            yield {
                "event": "done",
                "data": json.dumps(
                    {
                        "ok": True,
                        "reportId": draft.public_id,
                        "title": title,
                        "charCount": len(accumulated),
                    },
                    ensure_ascii=False,
                ),
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception("ai report generate failed")
            try:
                await reports_svc.finalize_error(
                    db,
                    report=draft,
                    error_msg=str(exc),
                    content=accumulated,
                )
            except Exception:  # noqa: BLE001
                logger.exception("ai report finalize_error failed")
            yield {
                "event": "error",
                "data": json.dumps({"msg": str(exc)}, ensure_ascii=False),
            }

    return EventSourceResponse(event_generator())

