"""Phase 1 regression tests: validation, guard, persistence and kill switch."""

import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config.settings import get_settings
from app.config.trading import profile_from_settings
from app.database.database import SessionLocal
from app.database.models import RunnerState
from app.execution.guard import ExecutionGuardError, TradeGuard
from app.risk.kill_switch import KillSwitch
from app.risk.risk_manager import RiskManager
from app.strategy.validation import SignalValidationError, validate_signal
from app.trading.executor import Executor
from app.trading.paper_trader import PaperTrader
from app.trading.runner import TradingRunner


def _profile():
    return profile_from_settings(get_settings(), mode="paper")


def _signal(**overrides):
    now = int(time.time())
    sig = {
        "direction": "long",
        "entry": 50000.0,
        "stop_loss": 49000.0,
        "target": 52000.0,
        "accepted_size": 50.0,
        "risk_allowed": True,
        "confidence": 0.8,
        "score": 1.0,
        "candle_time": now,
        "data_quality": {"status": "streaming", "price_valid": True, "source": "test", "last_candle_age_secs": 10},
        "product_id": 0,
    }
    sig.update(overrides)
    return sig


def _candles(n=140, age_secs=60):
    last = int(time.time()) - age_secs
    return [
        {
            "time": last - (n - 1 - i) * 3600,
            "open": 50000.0 + i,
            "high": 50040.0 + i,
            "low": 49960.0 + i,
            "close": 50000.0 + i,
            "volume": 10.0,
        }
        for i in range(n)
    ]


# ------------------------------------------------------------------ validation


def test_validate_signal_rejects_stale_and_future_candles():
    profile = _profile()
    now = int(time.time())
    stale = _signal(candle_time=now - profile.max_candle_age_secs - 1)
    with pytest.raises(SignalValidationError, match="stale"):
        validate_signal(stale, profile=profile)

    future = _signal(candle_time=now + 3 * 3600)
    with pytest.raises(SignalValidationError, match="future"):
        validate_signal(future, profile=profile)


def test_validate_signal_requires_streaming_quality_for_entries():
    profile = _profile()
    sig = _signal()
    sig.pop("data_quality")
    with pytest.raises(SignalValidationError, match="streaming"):
        validate_signal(sig, profile=profile)


def test_validate_signal_rejects_unapproved_and_size_mismatch_at_guard():
    profile = _profile()
    risk = RiskManager(starting_equity=10000.0, settings=profile.risk)
    guard = TradeGuard(profile, risk)

    with pytest.raises(ExecutionGuardError, match="risk_allowed"):
        guard.check(_signal(risk_allowed=False))

    with pytest.raises(ExecutionGuardError, match="accepted_size mismatch"):
        guard.check(_signal(accepted_size=1.0))


# ---------------------------------------------------------------------- guard


def test_executor_uses_risk_size_and_rejects_caller_override():
    profile = _profile()
    settings = get_settings().model_copy(update={"live_mode": False, "delta_api_key": "", "delta_api_secret": ""})
    paper = PaperTrader(starting_equity=10000.0, profile=profile)
    ex = Executor(settings=settings, paper=paper, profile=profile)

    assert ex.execute(_signal(), size=1.0, price=50000.0) is None
    assert "caller size mismatch" in ex.last_guard_error
    assert paper.position is None

    result = ex.execute(_signal(), price=50000.0)
    assert result is not None and result.venue == "paper"
    assert paper.position is not None
    assert paper.position["size"] == pytest.approx(50.0)


def test_validation_preserves_execution_extras():
    validated = validate_signal(_signal(product_id=77), profile=_profile())
    assert validated.model_extra["product_id"] == 77


def test_guard_reservation_is_exactly_once():
    profile = _profile()
    risk = RiskManager(starting_equity=10000.0, settings=profile.risk)
    guard = TradeGuard(profile, risk)
    guarded = guard.check(_signal())
    guard.reserve(guarded.signal, "paper")
    with pytest.raises(ExecutionGuardError, match="duplicate"):
        guard.reserve(guarded.signal, "paper")


# ----------------------------------------------- persistence and kill switch


def test_risk_state_survives_restart_and_close_events_are_idempotent():
    profile = _profile()
    risk = RiskManager(
        starting_equity=10000.0,
        settings=profile.risk,
        session_factory=SessionLocal,
        account_key="regression-risk",
    )
    assert risk.record_trade(25.0, event_id="close-1") is True
    assert risk.record_trade(25.0, event_id="close-1") is False

    restored = RiskManager(
        starting_equity=10000.0,
        settings=profile.risk,
        session_factory=SessionLocal,
        account_key="regression-risk",
    )
    assert restored.realized_pnl == pytest.approx(25.0)
    assert restored.record_trade(25.0, event_id="close-1") is False


def test_kill_switch_persists_and_resumes():
    ks = KillSwitch(SessionLocal, account_key="regression-switch")
    ks.resume(actor="test")
    ks.halt("regression halt", actor="test")

    restarted = KillSwitch(SessionLocal, account_key="regression-switch")
    assert restarted.is_halted() is True
    assert restarted.status()["reason"] == "regression halt"

    restarted.resume(actor="test")
    assert KillSwitch(SessionLocal, account_key="regression-switch").is_halted() is False


# --------------------------------------------------------------------- runner


class _FakeEngine:
    def generate(self, view, headlines=None):
        return dict(_signal())


def test_runner_seeds_on_startup_and_never_auto_enters():
    profile = _profile()
    risk = RiskManager(starting_equity=10000.0, settings=profile.risk)
    paper = PaperTrader(starting_equity=10000.0, profile=profile, risk_manager=risk)
    market = SimpleNamespace(
        candles=_candles(),
        ticker={"close": 50000.0},
        snapshot=lambda: {
            "data_status": "streaming",
            "price_valid": True,
            "data_source": "test",
            "last_candle_age_secs": 60,
        },
    )
    runner = TradingRunner(
        market=market,
        engine=_FakeEngine(),
        risk_manager=risk,
        paper_trader=paper,
        news_getter=lambda: [],
        session_factory=SessionLocal,
        seed_on_startup=True,
    )

    runner._process_cycle()
    assert runner.last_signal is None
    assert paper.position is None
    with SessionLocal() as db:
        assert db.execute(select(func.count()).select_from(RunnerState)).scalar_one() == 1


# ------------------------------------------------------------------- API auth


def test_watchlist_mutation_requires_admin_token(monkeypatch):
    import app.main as main

    monkeypatch.setattr(main.settings, "admin_token", "test-token")
    client = TestClient(main.app)
    response = client.post("/watchlist", json={"symbol": "RELIANCE"})
    assert response.status_code == 403
