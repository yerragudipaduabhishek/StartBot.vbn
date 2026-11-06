"""Small, dependency-free observability services for the paper runtime."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.database.models import Alert, AuditEvent, EquitySnapshot, RiskDecisionRecord, SystemEvent

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json(value: Any) -> str:
    return json.dumps(value or {}, default=str, sort_keys=True, separators=(",", ":"))


def record_audit(db, event_type: str, *, actor: str = "system", severity: str = "info",
                 correlation_id: str | None = None, payload: dict | None = None) -> AuditEvent:
    row = AuditEvent(event_type=event_type, actor=actor, severity=severity,
                     correlation_id=correlation_id, payload=_json(payload))
    db.add(row)
    db.commit()
    return row


def record_system_event(db, event_type: str, message: str, *, severity: str = "info",
                        payload: dict | None = None) -> SystemEvent:
    row = SystemEvent(event_type=event_type, severity=severity, message=message, payload=_json(payload))
    db.add(row)
    db.commit()
    return row


def snapshot_equity(db, *, account_key: str, equity: float, balance: float,
                    realized_pnl: float = 0.0, unrealized_pnl: float = 0.0,
                    peak_equity: float | None = None, source: str = "paper") -> EquitySnapshot:
    peak = max(float(peak_equity or equity), float(equity))
    drawdown = max(0.0, (peak - float(equity)) / peak * 100.0) if peak else 0.0
    row = EquitySnapshot(account_key=account_key, equity=float(equity), balance=float(balance),
                         realized_pnl=float(realized_pnl), unrealized_pnl=float(unrealized_pnl),
                         drawdown_pct=drawdown, source=source)
    db.add(row)
    db.commit()
    return row


def raise_alert(db, *, dedupe_key: str, title: str, message: str,
                severity: str = "warning") -> Alert:
    """Create or reactivate an alert without making delivery part of trading."""
    row = db.execute(select(Alert).where(Alert.dedupe_key == dedupe_key)).scalar_one_or_none()
    if row is None:
        row = Alert(dedupe_key=dedupe_key, title=title, message=message, severity=severity)
        db.add(row)
    else:
        row.title = title
        row.message = message
        row.severity = severity
        row.active = True
        row.updated_at = _now()
    db.commit()
    logger.warning("alert[%s] %s: %s", severity, title, message)
    return row


def record_risk_decision(
    db,
    decision,
    *,
    execution_mode: str = "paper",
    signal_key: str | None = None,
    equity: float | None = None,
    payload: dict | None = None,
) -> RiskDecisionRecord:
    """Persist the risk decision before any order is created."""
    data = decision.model_dump() if hasattr(decision, "model_dump") else dict(decision or {})
    size = data.get("size")
    if isinstance(size, dict):
        size = size.get("quantities")
    row = RiskDecisionRecord(
        signal_key=str(signal_key or data.get("signal_key") or ""),
        execution_mode=str(execution_mode or data.get("execution_mode") or "paper"),
        allowed=bool(data.get("allowed", False)),
        reason=str(data.get("reason") or ""),
        size=None if size is None else float(size),
        equity=equity,
        payload=_json({"decision": data, **(payload or {})}),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def resolve_alert(db, dedupe_key: str) -> bool:
    row = db.execute(select(Alert).where(Alert.dedupe_key == dedupe_key)).scalar_one_or_none()
    if row is None:
        return False
    row.active = False
    row.updated_at = _now()
    db.commit()
    return True
