"""滚动删除超过保留期的操作/登录日志。"""

from __future__ import annotations

from api.services import audit as audit_svc
from common.logging import get_logger

logger = get_logger(__name__)


async def purge_expired_audit_logs() -> dict[str, int]:
    try:
        result = await audit_svc.purge_expired_logs()
        logger.info(
            "audit log purge deleted operations=%s logins=%s retention_days=%s",
            result.get("operations", 0),
            result.get("logins", 0),
            audit_svc.retention_days(),
        )
        return result
    except Exception:  # noqa: BLE001
        logger.exception("audit log purge failed")
        return {"operations": 0, "logins": 0}
