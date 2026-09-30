"""写入类请求的操作日志中间件（管理配置、知识库、工作流、智能体、提示词、治理任务、会话删除）。"""

from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from api.services import audit as audit_svc
from common.logging import get_logger

logger = get_logger(__name__)


class OperationLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        if request.method == "OPTIONS":
            return await call_next(request)

        target = audit_svc.match_write_target(request.method, request.url.path)
        if target is None:
            return await call_next(request)

        module, action, resource_id = target
        started = time.perf_counter()
        ip = audit_svc.client_ip(request)
        ua = audit_svc.user_agent(request)
        method = request.method.upper()
        path = request.url.path
        request_id = audit_svc.request_id_of(request)

        try:
            response = await call_next(request)
        except Exception:
            await _persist(
                module=module,
                action=action,
                method=method,
                path=path,
                resource_id=resource_id,
                request=request,
                ip=ip,
                ua=ua,
                status_code=500,
                error_msg="unhandled exception",
                duration_ms=int((time.perf_counter() - started) * 1000),
                request_id=request_id,
            )
            raise

        status_code = int(getattr(response, "status_code", 500) or 500)
        error_msg = None
        if status_code >= 400:
            error_msg = audit_svc.error_msg_from_body(
                getattr(response, "body", None), status_code
            )
        await _persist(
            module=module,
            action=action,
            method=method,
            path=path,
            resource_id=resource_id,
            request=request,
            ip=ip,
            ua=ua,
            status_code=status_code,
            error_msg=error_msg,
            duration_ms=int((time.perf_counter() - started) * 1000),
            request_id=request_id or audit_svc.request_id_of(request),
        )
        return response


async def _persist(
    *,
    module: str,
    action: str,
    method: str,
    path: str,
    resource_id: str | None,
    request: Request,
    ip: str,
    ua: str,
    status_code: int,
    error_msg: str | None,
    duration_ms: int,
    request_id: str = "",
) -> None:
    operator = getattr(request.state, "jwt_sub", None)
    operator_username = str(operator) if operator else None
    try:
        await audit_svc.record_operation(
            module=module,
            action=action,
            method=method,
            path=path,
            resource_id=resource_id,
            operator_username=operator_username,
            success=status_code < 400,
            status_code=status_code,
            ip=ip,
            user_agent_str=ua,
            error_msg=error_msg,
            duration_ms=duration_ms,
            request_id=request_id,
            detail=audit_svc.build_operation_detail(
                module=module,
                action=action,
                resource_id=resource_id,
                status_code=status_code,
            ),
        )
    except Exception:  # noqa: BLE001
        logger.exception("operation log persist failed path=%s", path)
