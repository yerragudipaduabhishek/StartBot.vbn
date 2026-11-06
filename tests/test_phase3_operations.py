"""Phase 3 alert delivery, structured logs and order API truth."""

from __future__ import annotations

import io
import json
import logging

from sqlalchemy import select

from app.alerts import AlertDispatcher, DryRunSink, notify
from app.database.database import SessionLocal
from app.database.models import Alert
from app.logging_config import JsonFormatter, bind_correlation, clear_correlation


class _FailingSink:
    def send(self, message):
        raise RuntimeError("sink unavailable")


def test_alert_dispatch_is_deduplicated_and_delivery_failure_does_not_raise():
    dry = DryRunSink()
    dispatcher = AlertDispatcher([dry, _FailingSink()])
    with SessionLocal() as db:
        first = notify(db, dedupe_key="feed:test", title="stale", message="old", dispatcher=dispatcher)
        second = notify(db, dedupe_key="feed:test", title="stale", message="still old", dispatcher=dispatcher)
        assert first.id == second.id
        assert db.execute(select(Alert)).scalars().all().__len__() == 1
    assert len(dry.sent) == 2
    assert dry.sent[-1].message == "still old"


def test_json_formatter_includes_correlation_and_is_valid_json():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("phase3.test")
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    bind_correlation(cycle_id="cycle-9", signal_key="signal-1")
    try:
        logger.info("cycle complete")
    finally:
        clear_correlation()
        logger.handlers = []
    payload = json.loads(stream.getvalue().strip())
    assert payload["message"] == "cycle complete"
    assert payload["cycle_id"] == "cycle-9"
    assert payload["signal_key"] == "signal-1"
    assert payload["level"] == "INFO"
