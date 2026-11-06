from sqlalchemy import select

from app.database.database import SessionLocal, init_db
from app.database.models import Alert, AuditEvent, EquitySnapshot, SystemEvent
from app.observability import raise_alert, record_audit, record_system_event, snapshot_equity, resolve_alert


def test_phase3_observability_records_audit_system_and_equity():
    init_db()
    with SessionLocal() as db:
        audit = record_audit(db, "test_event", actor="pytest", payload={"ok": True})
        event = record_system_event(db, "test_health", "healthy", payload={"latency_ms": 2})
        equity = snapshot_equity(
            db,
            account_key="paper",
            equity=980.0,
            balance=1_000.0,
            realized_pnl=-20.0,
            peak_equity=1_000.0,
        )
        assert audit.id and event.id and equity.id
        assert equity.drawdown_pct == 2.0
        assert db.execute(select(AuditEvent)).scalars().first() is not None
        assert db.execute(select(SystemEvent)).scalars().first() is not None
        assert db.execute(select(EquitySnapshot)).scalars().first() is not None


def test_phase3_alerts_are_deduplicated_and_resolvable():
    init_db()
    with SessionLocal() as db:
        first = raise_alert(db, dedupe_key="feed:BTCUSDT", title="Feed stale", message="stale")
        second = raise_alert(db, dedupe_key="feed:BTCUSDT", title="Feed still stale", message="still stale")
        assert first.id == second.id
        assert db.execute(select(Alert)).scalars().all().__len__() == 1
        assert resolve_alert(db, "feed:BTCUSDT") is True
        assert db.execute(select(Alert)).scalar_one().active is False
