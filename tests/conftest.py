import os
import tempfile

import pytest

# Point the app at an isolated SQLite DB before any app imports happen,
# so tests never touch the real data/trading.db.
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(tempfile.gettempdir(), 'btc_ai_test.db')}"
os.environ["RESEARCH_DATABASE_URL"] = (
    f"sqlite:///{os.path.join(tempfile.gettempdir(), 'btc_ai_test_research.db')}"
)


@pytest.fixture(autouse=True)
def _clean_db():
    from sqlalchemy import delete

    from app.database.database import SessionLocal, init_db
    from app.database.models import (
        ExecutionIntent,
        Alert,
        AuditEvent,
        EquitySnapshot,
        Fill,
        KillSwitchState,
        Order,
        OrderEvent,
        PaperAccount,
        PaperPosition,
        PaperTrade,
        RiskDecisionRecord,
        RiskEvent,
        RiskState,
        RunnerState,
        Signal,
        StrategyVersion,
        TestnetExecution,
        SystemEvent,
        WatchlistItem,
    )

    init_db()
    with SessionLocal() as db:
        db.execute(delete(ExecutionIntent))
        db.execute(delete(Fill))
        db.execute(delete(OrderEvent))
        db.execute(delete(Order))
        db.execute(delete(RiskDecisionRecord))
        db.execute(delete(StrategyVersion))
        db.execute(delete(RiskEvent))
        db.execute(delete(RiskState))
        db.execute(delete(KillSwitchState))
        db.execute(delete(RunnerState))
        db.execute(delete(Signal))
        db.execute(delete(PaperTrade))
        db.execute(delete(PaperPosition))
        db.execute(delete(PaperAccount))
        db.execute(delete(TestnetExecution))
        db.execute(delete(Alert))
        db.execute(delete(AuditEvent))
        db.execute(delete(EquitySnapshot))
        db.execute(delete(SystemEvent))
        db.execute(delete(WatchlistItem))
        db.commit()
    yield
