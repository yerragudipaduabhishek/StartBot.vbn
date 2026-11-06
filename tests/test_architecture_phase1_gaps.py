"""Additional Phase 1 architecture regressions found during verification.

These close the gaps not covered by the original Phase 1 architecture suite:
failed persistence, local/exchange position mismatch, malformed signals, the
paper path being unable to reach an exchange client, and live always being off.
"""

from __future__ import annotations

import time

import pytest

from app.config.settings import get_settings
from app.config.trading import profile_from_settings
from app.execution.adapters import ExecutionRouter, PaperExecutionAdapter
from app.execution.recovery import compare_positions
from app.risk.risk_manager import RiskManager
from app.strategy.validation import SignalValidationError, validate_signal
from app.trading.executor import Executor
from app.trading.paper_trader import PaperTrader


def _profile(mode: str = "paper"):
    return profile_from_settings(get_settings(), mode=mode)


def _signal(profile, **overrides):
    payload = {
        "strategy_id": profile.strategy_id,
        "strategy_version": profile.strategy_version,
        "symbol": profile.symbol,
        "timeframe": profile.timeframe,
        "candle_time": int(time.time()) - 60,
        "direction": "long",
        "score": 1.0,
        "confidence": 0.8,
        "entry": 50_000.0,
        "stop_loss": 49_000.0,
        "target": 52_000.0,
        "accepted_size": 50.0,
        "risk_allowed": True,
        "risk_reason": "approved",
        "data_quality": {
            "status": "streaming",
            "price_valid": True,
            "source": "test",
            "last_candle_age_secs": 10,
        },
    }
    payload.update(overrides)
    return payload


def _approved_signal_and_decision(profile):
    signal = validate_signal(_signal(profile), profile=profile, check_freshness=False)
    risk = RiskManager(settings=profile.risk, starting_equity=10_000.0, execution_mode=profile.mode)
    decision = risk.evaluate_decision(
        signal.model_dump(),
        equity=10_000.0,
        contract_value=profile.contract_value,
        timeframe=profile.timeframe,
    )
    return signal, decision


def test_failed_persistence_halts_future_entries():
    class BrokenSession:
        def __enter__(self):
            raise RuntimeError("database unavailable")

        def __exit__(self, *args):
            return False

    risk = RiskManager(
        settings=_profile().risk,
        starting_equity=10_000.0,
        session_factory=lambda: BrokenSession(),
        execution_mode="paper",
        restore=False,
    )
    risk.record_trade(25.0, event_id="persistence-gap")
    assert risk.trading_halted is True
    assert "persistence failure" in risk.halt_reason
    assert "database unavailable" in risk.persistence_error

    decision = risk.evaluate_decision(
        _signal(_profile()),
        equity=10_000.0,
        contract_value=0.001,
        timeframe="1h",
        now=None,
    )
    assert decision.allowed is False
    assert "halted" in decision.reason.lower()


def test_local_exchange_position_mismatch_is_reported_not_repaired():
    local = [{"symbol": "BTCUSDT", "side": "long", "size": 2.0}]
    exchange = [{"symbol": "BTCUSDT", "side": "long", "size": 1.0}]
    report = compare_positions(local, exchange)
    assert report["ok"] is False
    assert report["mismatches"] == ["BTCUSDT long: local=2 exchange=1"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"stop_loss": 51_000.0},  # long stop above entry
        {"target": 48_000.0},  # long target below entry
        {"accepted_size": 0.0},
        {"accepted_size": None},
        {"risk_allowed": False},
        {"direction": "sideways"},
    ],
)
def test_malformed_or_unapproved_signals_are_rejected(overrides):
    profile = _profile()
    with pytest.raises(SignalValidationError):
        validate_signal(_signal(profile, **overrides), profile=profile, check_freshness=False)


def test_paper_router_has_no_exchange_client_and_never_calls_one():
    class SpyExchange:
        def __init__(self):
            self.calls = []

        def place_order(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return {"result": {"id": "forbidden"}}

    profile = _profile("paper")
    signal, decision = _approved_signal_and_decision(profile)
    paper = PaperTrader(starting_equity=10_000.0, profile=profile)
    spy = SpyExchange()
    router = ExecutionRouter(profile, paper=paper, execution_client=spy, testnet_client=spy)
    assert isinstance(router.adapter, PaperExecutionAdapter)
    result = router.submit_signal(signal, decision, price=signal.entry)
    assert result.venue == "paper"
    assert spy.calls == []


def test_paper_executor_does_not_construct_an_exchange_client(monkeypatch):
    import app.trading.executor as executor_module

    constructed = []

    class SpyDelta:
        def __init__(self, *args, **kwargs):
            constructed.append((args, kwargs))

    monkeypatch.setattr(executor_module, "DeltaClient", SpyDelta)
    settings = get_settings().model_copy(
        update={"delta_api_key": "key", "delta_api_secret": "secret", "testnet_mode": True, "live_mode": False}
    )
    profile = _profile("paper")
    paper = PaperTrader(starting_equity=10_000.0, profile=profile)
    ex = Executor(settings=settings, paper=paper, profile=profile)
    assert ex.order_client is None
    assert constructed == []
    assert ex.route_mode.value == "paper"


def test_live_is_off_even_with_every_legacy_flag_set():
    settings = get_settings().model_copy(
        update={
            "delta_api_key": "key",
            "delta_api_secret": "secret",
            "live_mode": True,
            "testnet_mode": False,
            "real_order_confirmation": True,
        }
    )
    profile = _profile("paper")
    ex = Executor(settings=settings, paper=PaperTrader(starting_equity=10_000.0, profile=profile), profile=profile)
    assert ex.can_trade_live is False
    assert ex.can_trade_real is False
    assert ex.route_mode.value == "paper"
