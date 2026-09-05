from __future__ import annotations

import asyncio
import json
from typing import Any, Literal

from fastapi import APIRouter, Request
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
from api.services.mcp.runtime import run_chat_with_mcp
from common.config import get_settings
from common.errors import AppError, ErrorCode
from common.logging import get_logger
from common.redis_tools import (
    check_sliding_rate_limit,
    force_release_session_lock,
    session_lock,
)
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
    # 可选系统提示词 publicId；未传/空 = 不注入管理提示词基座
    promptId: str | None = Field(default=None, max_length=32)
    # 场景智能体：若传有效 id，则覆盖 mode / promptId / knowledgeBaseIds / 工具策略
    agentId: str | None = Field(default=None, max_length=32)
    # 指定模型配置 publicId；未传则用快速/深度默认绑定，再回退到任一已启用对话模型
    modelId: str | None = Field(default=None, max_length=32)


class RelatedRequest(BaseModel):
    question: str
    answer: str = ""


class CreateSessionRequest(BaseModel):
    title: str | None = Field(default=None, max_length=128)
    mode: Literal["fast", "deep"] = "fast"


class UpdateSessionRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=128)
    mode: Literal["fast", "deep"] | None = None


def _normalize_kb_ids(raw: list[str] | None) -> list[str]:
    return [str(x).strip() for x in (raw or []) if str(x).strip()][:32]


async def _resolve_chat_bindings(db: DbSession, body: ChatRequest) -> dict[str, Any]:
    """解析本轮对话绑定：有 agentId 时以智能体配置为准，否则用请求体字段。"""
    agent_id = (body.agentId or "").strip() or None
    if not agent_id:
        return {
            "agentId": None,
            "agentName": None,
            "mode": body.mode,
            "promptId": (body.promptId or "").strip() or None,
            "knowledgeBaseIds": _normalize_kb_ids(body.knowledgeBaseIds),
            "toolsEnabled": True,
            "allowedToolIds": None,
            "modelId": (body.modelId or "").strip() or None,
        }

    from api.services.agents import configs as agent_configs

    bundle = await agent_configs.get_enabled_bundle(db, agent_id)
    mode = bundle.get("mode") if bundle.get("mode") in ("fast", "deep") else "fast"
    tools_enabled = bool(bundle.get("toolsEnabled", True))
    mcp_ids = [
        str(x).strip()
        for x in (bundle.get("mcpToolIds") or [])
        if str(x).strip()
    ][:64]
    # toolsEnabled=False → 无工具；白名单非空 → 过滤；白名单空 → 不额外限制
    if not tools_enabled:
        allowed: list[str] | None = []
    elif mcp_ids:
        allowed = mcp_ids
    else:
        allowed = None

    return {
        "agentId": bundle["id"],
        "agentName": bundle.get("name"),
        "mode": mode,
        "promptId": (bundle.get("promptId") or "").strip() or None,
        "knowledgeBaseIds": _normalize_kb_ids(bundle.get("knowledgeBaseIds") or []),
        "toolsEnabled": tools_enabled,
        "allowedToolIds": allowed,
        "modelId": (body.modelId or "").strip() or None,
    }

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
    await force_release_session_lock(session_id)
    return ok({"deleted": True})


@router.post("/sessions/{session_id}/cancel")
async def sessions_cancel(session_id: str, db: DbSession, user: CurrentUser) -> dict:
    """前端点「停止」时调用：强制释放会话生成锁，便于立即重问。"""
    await sessions_svc.get_session_for_user(
        db,
        public_id=session_id,
        user_id=user.id,
        with_messages=False,
    )
    released = await force_release_session_lock(session_id)
    return ok({"cancelled": True, "lockReleased": released})


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
async def ai_chat(
    body: ChatRequest,
    request: Request,
    db: DbSession,
    user: CurrentUser,
) -> EventSourceResponse:
    """SSE 对话：Redis 热窗口 + 限流 + 会话锁 + 滚动裁剪 + 长期记忆召回。"""
    settings = get_settings()
    await check_sliding_rate_limit(scope="chat", subject=str(user.id))
    bindings = await _resolve_chat_bindings(db, body)
    chat_mode: Literal["fast", "deep"] = bindings["mode"]
    prompt_id: str | None = bindings["promptId"]
    kb_ids_bind: list[str] = list(bindings["knowledgeBaseIds"])
    tools_enabled: bool = bool(bindings["toolsEnabled"])
    allowed_tool_ids: list[str] | None = bindings["allowedToolIds"]
    agent_id: str | None = bindings["agentId"]
    agent_name: str | None = bindings["agentName"]
    model_id: str | None = bindings.get("modelId")

    await build_llm_client(db, chat_mode, model_id=model_id)
    user_text = _extract_user_content(body)

    session = await sessions_svc.get_session_for_user(
        db,
        public_id=body.sessionId,
        user_id=user.id,
        with_messages=False,
    )

    async def event_generator():  # noqa: ANN202
        async def _renew_loop(lock: Any) -> None:
            interval = max(15.0, float(getattr(lock, "ttl", 300) or 300) / 3.0)
            try:
                while True:
                    await asyncio.sleep(interval)
                    ok = await lock.renew()
                    if not ok:
                        logger.warning(
                            "chat session lock renew failed session=%s",
                            session.public_id,
                        )
                        return
            except asyncio.CancelledError:
                raise

        async def _watch_disconnect() -> None:
            """客户端点停止/关页时尽快强释锁，避免后续 Failed to fetch / 锁冲突。"""
            try:
                while True:
                    if await request.is_disconnected():
                        logger.info(
                            "chat client disconnected, force release lock session=%s",
                            session.public_id,
                        )
                        await force_release_session_lock(session.public_id)
                        return
                    await asyncio.sleep(0.4)
            except asyncio.CancelledError:
                raise

        disconnect_task: asyncio.Task[None] | None = None
        try:
            async with session_lock(session.public_id, wait_seconds=1.0) as lock:
                renew_task = asyncio.create_task(_renew_loop(lock))
                disconnect_task = asyncio.create_task(_watch_disconnect())
                try:
                    await memory_svc.ensure_hot_context(db, session=session)

                    user_hot = memory_svc.build_hot_message(
                        role="user",
                        content=user_text,
                        mode=chat_mode,
                        knowledge_base_ids=list(kb_ids_bind),
                    )
                    await memory_svc.append_hot_and_enqueue(
                        session=session, message=user_hot
                    )

                    chunks: list[dict[str, Any]] = []
                    kb_ids = list(kb_ids_bind)
                    await memory_svc.bump_session_meta(
                        db,
                        session=session,
                        mode=chat_mode,
                        knowledge_base_ids=kb_ids,
                    )
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

                    refs_payload: dict[str, Any] = {
                        "mode": chat_mode,
                        "chunks": chunks,
                        "governance": gov_refs,
                        "useKnowledge": use_knowledge,
                        "knowledgeBaseIds": kb_ids,
                    }
                    if agent_id:
                        refs_payload["agentId"] = agent_id
                        refs_payload["agentName"] = agent_name
                    yield {
                        "event": "refs",
                        "data": json.dumps(refs_payload, ensure_ascii=False),
                    }

                    hot_msgs = await memory_svc.load_hot_messages(session.public_id)
                    base_prompt: str | None = None
                    if prompt_id:
                        from api.services.prompts import configs as prompt_configs

                        base_prompt = await prompt_configs.get_enabled_content(
                            db, prompt_id
                        )
                    system_prompt = await build_system_prompt(
                        db,
                        chunks,
                        base_prompt=base_prompt,
                        long_memory=long_mem,
                        use_knowledge=use_knowledge,
                        governance_refs=gov_refs,
                    )
                    llm_messages: list[dict[str, Any]] = []
                    if system_prompt:
                        llm_messages.append(
                            {"role": "system", "content": system_prompt}
                        )
                    llm_messages.extend(memory_svc.hot_messages_for_llm(hot_msgs))

                    accumulated = ""
                    try:
                        client = await build_llm_client(db, chat_mode, model_id=model_id)
                        async for ev in run_chat_with_mcp(
                            db,
                            client=client,
                            llm_messages=llm_messages,
                            mode=chat_mode,
                            session=session,
                            user_id=user.id,
                            kb_ids=kb_ids,
                            chunks=chunks,
                            tools_enabled=tools_enabled,
                            allowed_tool_ids=allowed_tool_ids,
                        ):
                            if await request.is_disconnected():
                                logger.info(
                                    "chat aborted mid-stream session=%s",
                                    session.public_id,
                                )
                                await force_release_session_lock(session.public_id)
                                return
                            if ev.get("event") == "__final__":
                                try:
                                    final = json.loads(ev.get("data") or "{}")
                                    accumulated = str(
                                        final.get("content") or accumulated
                                    )
                                except json.JSONDecodeError:
                                    pass
                                continue
                            yield ev

                        title: str | None = None
                        if accumulated.strip():
                            assistant_hot = memory_svc.build_hot_message(
                                role="assistant",
                                content=accumulated,
                                mode=chat_mode,
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
                                mode=chat_mode,
                                knowledge_base_ids=kb_ids,
                            )
                            try:
                                await memory_svc.maybe_roll_trim(db, session=session)
                            except Exception as exc:  # noqa: BLE001
                                logger.warning("roll trim failed: %s", exc)
                            title = await sessions_svc.maybe_auto_title(
                                db, session=session
                            )

                        done_payload: dict[str, Any] = {
                            "ok": True,
                            "sessionId": session.public_id,
                        }
                        if title:
                            done_payload["title"] = title
                        if agent_id:
                            done_payload["agentId"] = agent_id
                        yield {
                            "event": "done",
                            "data": json.dumps(done_payload, ensure_ascii=False),
                        }
                    except AppError as exc:
                        yield {
                            "event": "error",
                            "data": json.dumps({"msg": exc.msg}, ensure_ascii=False),
                        }
                    except asyncio.CancelledError:
                        await force_release_session_lock(session.public_id)
                        raise
                    except Exception as exc:  # noqa: BLE001
                        logger.exception("ai chat failed")
                        yield {
                            "event": "error",
                            "data": json.dumps({"msg": str(exc)}, ensure_ascii=False),
                        }
                finally:
                    renew_task.cancel()
                    try:
                        await renew_task
                    except asyncio.CancelledError:
                        pass
                    except Exception:  # noqa: BLE001
                        pass
        except AppError as exc:
            yield {
                "event": "error",
                "data": json.dumps({"msg": exc.msg}, ensure_ascii=False),
            }
        except asyncio.CancelledError:
            await force_release_session_lock(session.public_id)
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("ai chat event_generator failed")
            yield {
                "event": "error",
                "data": json.dumps({"msg": str(exc)}, ensure_ascii=False),
            }
        finally:
            if disconnect_task is not None:
                disconnect_task.cancel()
                try:
                    await disconnect_task
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001
                    pass

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
    # 可选：绑定已发布工作流；空则走内置 RAG+LLM 流水线
    workflowId: str | None = Field(default=None, max_length=32)


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
    from api.services.workflows import crud as wf_crud
    from api.services.workflows import runner as wf_runner

    if body.type not in reports_svc.REPORT_TYPES:
        raise AppError(ErrorCode.VALIDATION, "invalid report type", status_code=422)

    workflow_id = (body.workflowId or "").strip() or None
    if workflow_id:
        wf = await wf_crud.get_by_public_id(db, workflow_id)
        if not wf.enabled:
            raise AppError(ErrorCode.VALIDATION, "workflow is disabled", status_code=422)
        await wf_crud.resolve_run_version(db, wf, use_draft=False)
    else:
        # 内置流水线预检 LLM
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
        workflow_public_id=workflow_id,
    )

    async def event_generator():  # noqa: ANN202
        accumulated = ""
        chunks: list[dict[str, Any]] = []
        run_id: str | None = None
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
                        "workflowId": workflow_id,
                    },
                    ensure_ascii=False,
                ),
            }

            if workflow_id:
                wf_input = reports_svc.build_workflow_input(
                    report_type=body.type,
                    kiln_code=body.furnaceId.strip(),
                    kiln_name=kiln_name,
                    context_text=ctx["contextText"],
                    chunks=chunks,
                )
                async for ev in wf_runner.run_workflow(
                    db,
                    workflow_public_id=workflow_id,
                    input_data=wf_input,
                    use_draft=False,
                    created_by=user.id,
                    trigger="ai_report",
                ):
                    event_name = ev.get("event") or "message"
                    # 原样转发步骤事件，供前端展示轨迹
                    if event_name in {"step_start", "step_end"}:
                        yield ev
                        try:
                            payload = json.loads(ev.get("data") or "{}")
                            if not run_id and payload.get("runId"):
                                run_id = str(payload["runId"])
                                await reports_svc.attach_workflow_run(
                                    db, report=draft, workflow_run_id=run_id
                                )
                        except Exception:  # noqa: BLE001
                            pass
                        continue
                    if event_name == "error":
                        payload = json.loads(ev.get("data") or "{}")
                        raise AppError(
                            ErrorCode.INTERNAL,
                            str(payload.get("msg") or "workflow failed"),
                            status_code=500,
                        )
                    if event_name == "done":
                        payload = json.loads(ev.get("data") or "{}")
                        run_id = str(payload.get("runId") or run_id or "") or None
                        out = payload.get("output")
                        if isinstance(out, dict):
                            accumulated = str(
                                out.get("text") or out.get("output") or ""
                            )
                            if not accumulated and out.get("output") is not None:
                                accumulated = str(out.get("output"))
                            refs_from_wf = out.get("refs")
                            if isinstance(refs_from_wf, list) and refs_from_wf:
                                chunks = refs_from_wf
                        elif out is not None:
                            accumulated = str(out)
                        if not accumulated.strip():
                            raise AppError(
                                ErrorCode.INTERNAL,
                                "workflow finished without output text",
                                status_code=500,
                            )
                        # 一次性推送正文（工作流节点为非流式 complete）
                        yield {
                            "event": "delta",
                            "data": json.dumps(
                                {"text": accumulated}, ensure_ascii=False
                            ),
                        }
            else:
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
                workflow_run_id=run_id,
            )
            yield {
                "event": "done",
                "data": json.dumps(
                    {
                        "ok": True,
                        "reportId": draft.public_id,
                        "title": title,
                        "charCount": len(accumulated),
                        "workflowId": workflow_id,
                        "workflowRunId": run_id,
                    },
                    ensure_ascii=False,
                ),
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception("ai report generate failed")
            msg = str(exc)
            if isinstance(exc, AppError):
                msg = exc.msg
            try:
                await reports_svc.finalize_error(
                    db,
                    report=draft,
                    error_msg=msg,
                    content=accumulated,
                )
            except Exception:  # noqa: BLE001
                logger.exception("ai report finalize_error failed")
            yield {
                "event": "error",
                "data": json.dumps({"msg": msg}, ensure_ascii=False),
            }

    return EventSourceResponse(event_generator())

