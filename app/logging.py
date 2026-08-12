from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

try:
    import resource
except ImportError:  # Windows development environment; production image is Linux.
    resource = None  # type: ignore[assignment]

import psutil


_context: ContextVar[dict[str, Any]] = ContextVar("tender_log_context", default={})


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            **_context.get(),
        }
        extra = getattr(record, "event", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())


def bind_context(**values: Any) -> None:
    _context.set({**_context.get(), **{k: v for k, v in values.items() if v is not None}})


def clear_context() -> None:
    _context.set({})


def memory_rss_mb() -> float:
    try:
        if resource is not None:
            # Linux ru_maxrss is KiB. The Docker image is Linux.
            return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 2)
        return round(psutil.Process().memory_info().rss / (1024 * 1024), 2)
    except Exception:
        return 0.0


@contextmanager
def stage(logger: logging.Logger, name: str, **details: Any) -> Iterator[None]:
    started = time.monotonic()
    logger.info("stage_started", extra={"event": {"stage": name, **details}})
    try:
        yield
    except Exception as exc:
        logger.exception(
            "stage_failed",
            extra={
                "event": {
                    "stage": name,
                    "duration_seconds": round(time.monotonic() - started, 3),
                    "memory_rss_mb": memory_rss_mb(),
                    "error": str(exc),
                }
            },
        )
        raise
    else:
        logger.info(
            "stage_completed",
            extra={
                "event": {
                    "stage": name,
                    "duration_seconds": round(time.monotonic() - started, 3),
                    "memory_rss_mb": memory_rss_mb(),
                    **details,
                }
            },
        )
