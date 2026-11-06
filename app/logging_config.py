"""Structured JSON logging with correlation context."""

from __future__ import annotations

import json
import logging
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

_correlation: ContextVar[dict[str, Any]] = ContextVar("correlation", default={})


def bind_correlation(**values: Any) -> None:
    current = dict(_correlation.get())
    current.update({str(key): value for key, value in values.items() if value is not None})
    _correlation.set(current)


def clear_correlation() -> None:
    _correlation.set({})


@contextmanager
def correlation_context(**values: Any):
    token = _correlation.set({**dict(_correlation.get()), **{k: v for k, v in values.items() if v is not None}})
    try:
        yield
    finally:
        _correlation.reset(token)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            **dict(_correlation.get()),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for key in ("cycle_id", "signal_key", "order_id", "correlation_id"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        return json.dumps(payload, default=str, sort_keys=True)


def setup_logging(
    level: str | None = None,
    *,
    stream=None,
    log_file: str | None = None,
    max_bytes: int = 10_000_000,
    backup_count: int = 5,
) -> None:
    formatter = JsonFormatter()
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        rotating = RotatingFileHandler(
            path,
            maxBytes=max(1, int(max_bytes)),
            backupCount=max(1, int(backup_count)),
            encoding="utf-8",
        )
        rotating.setFormatter(formatter)
        root.addHandler(rotating)
    root.setLevel(getattr(logging, str(level or "INFO").upper(), logging.INFO))


__all__ = [
    "JsonFormatter",
    "bind_correlation",
    "clear_correlation",
    "correlation_context",
    "setup_logging",
]
