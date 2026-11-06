from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config.trading import TradingProfile, normalize_execution_mode, profile_from_settings
from app.contracts import ExecutionMode, OrderRequest, RiskDecision
from app.execution.adapters import (
    ExecutionModeError,
    ExecutionRouter,
    ExecutionValidationError,
    LiveExecutionAdapter,
)
from app.execution.modes import (
    LiveTradingDisabled,
    ResearchBoundaryError,
    assert_mode_can_persist,
    assert_mode_can_submit,
    mode_allows_orders,
    mode_allows_persistence,
)
from app.risk.config import RiskSettings
from app.risk.risk_manager import RiskManager
from app.strategy.validation import validate_signal
from app.trading.paper_trader import PaperTrader


def _profile(mode="paper"):
    return profile_from_settings(mode=mode)


def _signal_payload(profile, **overrides):
    payload = {
        "strategy_id": profile.strategy_id,
        "strategy_version": profile.strategy_version,
        "symbol": profile.symbol,
        "timeframe": profile.timeframe,
        "candle_time": 1_700_000_000,
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
    signal = validate_signal(
        _signal_payload(profile),
        profile=profile,
        check_freshness=False,
    )
    risk = RiskManager(
        settings=profile.risk,
        starting_equity=10_000.0,
        execution_mode=profile.mode,
    )
    decision = risk.evaluate_decision(
        signal.model_dump(),
        equity=10_000.0,
        contract_value=profile.contract_value,
        timeframe=profile.timeframe,
    )
    return signal, decision


def test_execution_mode_contract_is_complete():
    assert {mode.value for mode in ExecutionMode} == {
        "research",
        "backtest",
        "paper",
        "testnet",
        "live",
    }
    assert normalize_execution_mode("dev") == ExecutionMode.PAPER
    assert normalize_execution_mode("staging") == ExecutionMode.PAPER


def test_profile_from_settings_supports_research_modes_and_rejects_live():
    research = profile_from_settings(mode="research")
    backtest = profile_from_settings(mode="backtest")
    assert research.mode == ExecutionMode.RESEARCH
    assert backtest.mode == ExecutionMode.BACKTEST
    assert research.is_research and backtest.is_research
    assert research.ema_spans == (8, 40, 50)
    assert research.contract_value == pytest.approx(0.001)
    with pytest.raises(ValueError, match="live"):
        profile_from_settings(mode="live")


def test_profile_rejects_live_mode_directly_and_locked_strategy_drift():
    with pytest.raises(ValidationError, match="live"):
        TradingProfile(mode="live", symbol="BTCUSDT", timeframe="1h")
    with pytest.raises(ValidationError, match="locked"):
        TradingProfile(mode="paper", symbol="ETHUSDT", timeframe="1h")


def test_mode_policy_blocks_research_and_live_orders():
    assert mode_allows_orders(ExecutionMode.PAPER)
    assert mode_allows_orders(ExecutionMode.TESTNET)
    assert not mode_allows_orders(ExecutionMode.RESEARCH)
    assert not mode_allows_orders(ExecutionMode.BACKTEST)
    assert not mode_allows_orders(ExecutionMode.LIVE)
    assert mode_allows_persistence(ExecutionMode.PAPER)
    assert not mode_allows_persistence(ExecutionMode.RESEARCH)
    assert not mode_allows_persistence(ExecutionMode.BACKTEST)
    with pytest.raises(ExecutionModeError):
        assert_mode_can_submit(ExecutionMode.RESEARCH)
    with pytest.raises(LiveTradingDisabled):
        assert_mode_can_submit(ExecutionMode.LIVE)
    with pytest.raises(ResearchBoundaryError):
        assert_mode_can_persist(ExecutionMode.BACKTEST)


def test_research_risk_manager_never_binds_persistence():
    from app.database.database import SessionLocal

    risk = RiskManager(
        settings=RiskSettings(),
        starting_equity=10_000.0,
        session_factory=SessionLocal,
        execution_mode="research",
    )
    assert risk.persistence_allowed is False
    assert risk.session_factory is None
    with pytest.raises(ResearchBoundaryError):
        risk.bind_persistence(SessionLocal)


def test_live_risk_manager_is_rejected():
    with pytest.raises(LiveTradingDisabled):
        RiskManager(execution_mode="live")


def test_paper_router_requires_risk_and_opens_simulation_only():
    profile = _profile("paper")
    signal, decision = _approved_signal_and_decision(profile)
    assert decision.allowed and decision.size is not None
    paper = PaperTrader(
        starting_equity=10_000.0,
        fee_pct=profile.paper_fee_pct,
        slippage_pct=profile.paper_slippage_pct,
        profile=profile,
    )
    router = ExecutionRouter(profile, paper=paper)
    result = router.submit_signal(signal, decision, price=signal.entry)
    assert result.venue == "paper"
    assert result.execution_mode == ExecutionMode.PAPER
    assert paper.position is not None
    assert paper.position["size"] == pytest.approx(decision.size.quantities)


def test_router_rejects_unapproved_or_mismatched_decisions():
    profile = _profile("paper")
    signal, decision = _approved_signal_and_decision(profile)
    router = ExecutionRouter(profile, paper=PaperTrader(starting_equity=10_000.0))
    rejected = RiskDecision(
        allowed=False,
        size=None,
        reason="not approved",
        checks=("not approved",),
        signal_key=decision.signal_key,
    )
    with pytest.raises(ExecutionValidationError, match="not approved|risk decision"):
        router.submit_signal(signal, rejected)
    tampered = decision.model_copy(
        update={"size": decision.size.model_copy(update={"quantities": 1.0})}
    )
    with pytest.raises(ExecutionValidationError, match="size"):
        router.submit_signal(signal, tampered)


def test_trade_guard_cannot_be_constructed_for_research():
    from app.execution.guard import TradeGuard

    profile = _profile("research")
    risk = RiskManager(settings=profile.risk, execution_mode=profile.mode)
    with pytest.raises(ExecutionModeError):
        TradeGuard(profile, risk)


def test_research_router_cannot_submit():
    profile = _profile("research")
    router = ExecutionRouter(profile)
    assert router.adapter is None
    with pytest.raises(ExecutionModeError):
        router.submit_signal({}, RiskDecision(allowed=False, size=None, reason="no"))


def test_live_adapter_and_live_order_request_fail_closed():
    with pytest.raises(LiveTradingDisabled):
        LiveExecutionAdapter().submit()
    with pytest.raises(ValidationError, match="live"):
        OrderRequest(
            client_order_id="x",
            symbol="BTCUSDT",
            timeframe="1h",
            strategy_id="strategy_a",
            strategy_version="1.0.0",
            side="buy",
            size=1,
            expected_price=50_000.0,
            execution_mode="live",
            risk_decision_key="k",
            risk_approved=True,
        )


def test_paper_adapter_has_no_exchange_order_or_private_account_imports():
    source = Path("app/trading/paper_trader.py").read_text(encoding="utf-8")
    for forbidden in ("place_order", "cancel_order", "/v2/orders", "/v2/wallet"):
        assert forbidden not in source
    assert "PaperExecutionAdapter" in Path("app/execution/adapters.py").read_text(encoding="utf-8")


def test_strategy_paper_and_analysis_do_not_import_execution_client():
    for path in (
        "app/strategy/signal_engine.py",
        "app/trading/paper_trader.py",
        "app/analysis/service.py",
    ):
        source = Path(path).read_text(encoding="utf-8")
        assert "app.execution.client" not in source
        assert "TestnetExecutionClient" not in source


def test_backtest_uses_research_copy_and_does_not_persist_risk_state():
    from app.backtest.engine import run_backtest
    from app.database.database import SessionLocal
    from app.database.models import RiskEvent, RiskState
    from app.strategy.signal_engine import SignalEngine
    from tests.m3_fixtures import make_candles

    profile = profile_from_settings(mode="backtest")
    risk = RiskManager(
        settings=profile.risk,
        starting_equity=10_000.0,
        session_factory=SessionLocal,
    )
    engine = SignalEngine(risk_manager=risk, profile=profile)
    result = run_backtest(
        make_candles(n=300, step=40.0),
        engine,
        risk,
        initial_equity=10_000.0,
        warmup_bars=60,
        profile=profile,
    )
    assert result is not None
    with SessionLocal() as db:
        assert db.query(RiskEvent).count() == 0
        assert db.query(RiskState).count() == 0


def test_soak_runner_is_paper_only():
    from app.soak.runner import SoakRunner

    with pytest.raises(ExecutionModeError, match="paper"):
        SoakRunner(profile=profile_from_settings(mode="testnet"), enable_ws=False)
