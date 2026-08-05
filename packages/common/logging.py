import logging
import sys
from typing import Any


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def bind_request_id(logger: logging.Logger, request_id: str) -> logging.LoggerAdapter[Any]:
    return logging.LoggerAdapter(logger, {"request_id": request_id})
