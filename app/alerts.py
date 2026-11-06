"""Alert adapter layer.

Alerts are persisted first (deduplicated), then delivered to one or more
best-effort sinks. A sink failure is logged but never blocks a trading cycle.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol

from app.database.models import Alert
from app.observability import raise_alert

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AlertMessage:
    dedupe_key: str
    title: str
    message: str
    severity: str = "warning"
    metadata: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "dedupe_key": self.dedupe_key,
            "title": self.title,
            "message": self.message,
            "severity": self.severity,
            "metadata": self.metadata,
        }


class AlertSink(Protocol):
    def send(self, message: AlertMessage) -> None: ...


class LogSink:
    def send(self, message: AlertMessage) -> None:
        logger.warning("alert[%s] %s: %s", message.severity, message.title, message.message)


class DryRunSink:
    def __init__(self) -> None:
        self.sent: list[AlertMessage] = []

    def send(self, message: AlertMessage) -> None:
        self.sent.append(message)


class WebhookSink:
    """Minimal webhook adapter using the standard library only."""

    def __init__(self, url: str, *, timeout: float = 5.0) -> None:
        if not str(url or "").startswith(("https://", "http://127.0.0.1", "http://localhost")):
            raise ValueError("webhook URL must use HTTPS or loopback HTTP")
        self.url = url
        self.timeout = timeout

    def send(self, message: AlertMessage) -> None:
        body = json.dumps(message.as_dict(), sort_keys=True).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": "btc-ai-trading/0.1"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            if response.status >= 400:
                raise RuntimeError(f"webhook returned {response.status}")


class AlertDispatcher:
    def __init__(self, sinks: list[AlertSink] | None = None) -> None:
        self.sinks: list[AlertSink] = list(sinks or [LogSink()])

    def send(self, message: AlertMessage) -> None:
        for sink in self.sinks:
            try:
                sink.send(message)
            except Exception:
                logger.exception("alert delivery failed via %s", type(sink).__name__)


def dispatcher_from_env() -> AlertDispatcher:
    url = os.environ.get("ALERT_WEBHOOK_URL", "").strip()
    sinks: list[AlertSink] = [LogSink()]
    if url:
        sinks.append(WebhookSink(url))
    return AlertDispatcher(sinks)


def notify(
    db,
    *,
    dedupe_key: str,
    title: str,
    message: str,
    severity: str = "warning",
    dispatcher: AlertDispatcher | None = None,
    metadata: dict | None = None,
) -> Alert:
    row = raise_alert(db, dedupe_key=dedupe_key, title=title, message=message, severity=severity)
    (dispatcher or dispatcher_from_env()).send(
        AlertMessage(
            dedupe_key=dedupe_key,
            title=title,
            message=message,
            severity=severity,
            metadata=metadata or {},
        )
    )
    return row


def drawdown_breach(db, *, drawdown_pct: float, limit_pct: float, dispatcher=None) -> Alert:
    return notify(
        db,
        dedupe_key="risk:drawdown",
        title="Drawdown breach",
        message=f"drawdown {drawdown_pct:.2f}% exceeds limit {limit_pct:.2f}%",
        severity="critical",
        dispatcher=dispatcher,
    )


def daily_loss_breach(db, *, loss_pct: float, limit_pct: float, dispatcher=None) -> Alert:
    return notify(
        db,
        dedupe_key="risk:daily-loss",
        title="Daily loss breach",
        message=f"daily loss {loss_pct:.2f}% exceeds limit {limit_pct:.2f}%",
        severity="critical",
        dispatcher=dispatcher,
    )


def stale_feed(db, *, age_seconds: float, limit_seconds: float, source: str = "market", dispatcher=None) -> Alert:
    return notify(
        db,
        dedupe_key=f"feed:{source}",
        title="Stale market data",
        message=f"{source} data age {age_seconds:.0f}s exceeds {limit_seconds:.0f}s",
        severity="critical",
        dispatcher=dispatcher,
    )


def reconciliation_mismatch(db, *, mismatches: list[str], dispatcher=None) -> Alert:
    return notify(
        db,
        dedupe_key="reconciliation",
        title="Reconciliation mismatch",
        message="; ".join(mismatches)[:1000],
        severity="critical",
        dispatcher=dispatcher,
    )


def repeated_rejections(db, *, count: int, window_seconds: int, reason: str = "", dispatcher=None) -> Alert:
    return notify(
        db,
        dedupe_key="orders:repeated-rejections",
        title="Repeated order rejections",
        message=f"{count} rejections in {window_seconds}s: {reason}",
        severity="warning",
        dispatcher=dispatcher,
    )


def kill_switch_engaged(db, *, reason: str = "", dispatcher=None) -> Alert:
    return notify(
        db,
        dedupe_key="kill-switch",
        title="Kill switch engaged",
        message=reason or "new entries are halted by the kill switch",
        severity="critical",
        dispatcher=dispatcher,
    )


def heartbeat_missing(db, *, seconds: float, dispatcher=None) -> Alert:
    return notify(
        db,
        dedupe_key="runner:heartbeat",
        title="Runner heartbeat missing",
        message=f"no runner heartbeat for {seconds:.0f}s",
        severity="critical",
        dispatcher=dispatcher,
    )


def api_error(db, *, endpoint: str, message: str, dispatcher=None) -> Alert:
    return notify(
        db,
        dedupe_key=f"api:{endpoint}",
        title="Market/API error",
        message=message[:1000],
        severity="warning",
        dispatcher=dispatcher,
    )


__all__ = [
    "AlertDispatcher",
    "AlertMessage",
    "AlertSink",
    "DryRunSink",
    "LogSink",
    "WebhookSink",
    "api_error",
    "daily_loss_breach",
    "dispatcher_from_env",
    "drawdown_breach",
    "heartbeat_missing",
    "kill_switch_engaged",
    "notify",
    "reconciliation_mismatch",
    "repeated_rejections",
    "stale_feed",
]
