"""独立常驻 Worker：Stream 消费 + APScheduler TTL 兜底。

启动：
  poetry run zhongji-chat-worker
  或：poetry run python workers/stream_consumer_main.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages"))
sys.path.insert(0, str(ROOT / "apps"))
sys.path.insert(0, str(ROOT))


def main() -> None:
    asyncio.run(_async_main())


async def _async_main() -> None:
    from datetime import datetime

    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    from common.config import get_settings
    from common.logging import get_logger, setup_logging
    from workers.audit_purge import purge_expired_audit_logs
    from workers.chat_stream_consumer import ChatStreamConsumer
    from workers.ttl_flush import scan_and_flush_expiring_sessions
    from common.redis_client import close_redis
    import signal

    settings = get_settings()
    setup_logging("DEBUG" if settings.debug else "INFO")
    logger = get_logger(__name__)

    consumer = ChatStreamConsumer()
    scheduler = AsyncIOScheduler()

    # cron 如 "0 3 * * *" → 每天 03:00
    cron = (settings.chat_ttl_scan_cron or "0 3 * * *").split()
    if len(cron) == 5:
        trigger = CronTrigger(
            minute=cron[0],
            hour=cron[1],
            day=cron[2],
            month=cron[3],
            day_of_week=cron[4],
        )
    else:
        trigger = CronTrigger(hour=3, minute=0)
    scheduler.add_job(
        scan_and_flush_expiring_sessions,
        trigger=trigger,
        id="chat_ttl_flush",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        purge_expired_audit_logs,
        trigger=IntervalTrigger(hours=1),
        id="audit_log_purge",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(),
    )
    scheduler.start()
    logger.info(
        "APScheduler started ttl_cron=%s audit_purge=hourly retention_days=%s",
        settings.chat_ttl_scan_cron,
        settings.audit_log_retention_days,
    )

    loop = asyncio.get_running_loop()

    def _stop() -> None:
        logger.info("shutting down worker…")
        consumer.request_stop()
        scheduler.shutdown(wait=False)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            signal.signal(sig, lambda *_: _stop())

    try:
        await consumer.run_forever()
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
        await close_redis()


if __name__ == "__main__":
    main()
