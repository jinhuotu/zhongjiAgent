"""滚动删除超过保留期的操作/登录日志。"""

from __future__ import annotations

from api.services import audit as audit_svc
from common.logging import get_logger

logger = get_logger(__name__)


async def purge_expired_audit_logs() -> dict[str, int]:
    try:
        result = await audit_svc.purge_expired_logs()
        logger.info(
            "audit log purge deleted operations=%s logins=%s knowledge=%s queries=%s hotConfigs=%s chats=%s "
            "ops_days=%s trace_days=%s",
            result.get("operations", 0),
            result.get("logins", 0),
            result.get("knowledge", 0),
            result.get("queries", 0),
            result.get("hotConfigs", 0),
            result.get("chats", 0),
            audit_svc.retention_days(),
            audit_svc.trace_retention_days(),
        )
        return result
    except Exception:  # noqa: BLE001
        logger.exception("audit log purge failed")
        return {"operations": 0, "logins": 0}
