"""Phase 4 broker protocol: containment, delegation and portfolio gate."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from app.config.settings import get_settings
from app.config.trading import profile_from_settings
from app.contracts import ExecutionMode, OrderRequest, OrderResult, PositionSize, RiskDecision
from app.execution.broker import (
    BrokerError,
    BrokerNotConfigured,
    DeltaLiveBroker,
    DeltaTestnetBroker,
    LiveBrokerDisabled,
    PaperBroker,
    broker_for_profile,
)
from app.execution.guard import ExecutionGuardError, TradeGuard
from app.portfolio.manager import PortfolioLimits, PortfolioManager
from app.risk.risk_manager import RiskManager


def _paper_profile():
    return profile_from_settings(get_settings(), mode="paper")


def _testnet_profile():
    settings = get_settings().model_copy(update={"live_mode": False, "testnet_mode": True})
    return profile_from_settings(settings, mode="testnet")


def _request():
    return OrderRequest(
        client_order_id="paper:signal-1",
        symbol="BTCUSDT",
        timeframe="1h",
        strategy_id="strategy_a",
        strategy_version="v1",
        side="buy",
        size=1.0,
        expected_price=50_000.0,
        stop_loss=49_000.0,
        take_profit=52_000.0,
        execution_mode=ExecutionMode.PAPER,
        risk_decision_key="signal-1",
        risk_approved=True,
    )


def _decision():
    return RiskDecision(
        allowed=True,
        size=PositionSize(quantities=1.0, notional_usd=50_000.0, margin_usd=10_000.0, leverage=5.0),
        signal_key="signal-1",
        symbol="BTCUSDT",
        timeframe="1h",
        strategy_id="strategy_a",
        strategy_version="v1",
        execution_mode=ExecutionMode.PAPER,
        approval_status="approved",
    )


def _result(request: OrderRequest) -> OrderResult:
    return OrderResult(
        client_order_id=request.client_order_id,
        size=request.size,
        price=request.expected_price,
        fill_price=request.expected_price,
        expected_price=request.expected_price,
        venue="paper",
        status="accepted",
        order_state="filled",
        symbol=request.symbol,
        timeframe=request.timeframe,
        risk_decision_key=request.risk_decision_key,
        risk_approved=True,
        execution_mode=ExecutionMode.PAPER,
    )


def test_paper_broker_delegates_without_exchange_client():
    paper = SimpleNamespace(
        position={"symbol": "BTCUSDT", "side": "long", "size": 1.0, "entry_price": 50_000.0},
        balance=5_000.0,
    )
    calls = []

    class Adapter:
        paper = None

        def submit(self, request, decision):
            calls.append((request, decision))
            return _result(request)

    Adapter.paper = paper
    broker = PaperBroker(adapter=Adapter())
    request, decision = _request(), _decision()
    result = broker.place_order(request, decision)
    assert result.order_state == "filled"
    assert calls == [(request, decision)]
    assert broker.capabilities.simulated is True
    assert broker.cancel_order("x") is None
    assert broker.get_order("x") is None
    assert broker.get_positions()[0]["symbol"] == "BTCUSDT"
    assert broker.get_balances()[0]["balance"] == pytest.approx(5_000.0)


def test_paper_broker_requires_a_profile_or_adapter():
    with pytest.raises(BrokerNotConfigured):
        PaperBroker()


def test_testnet_broker_refuses_live_or_non_testnet_settings():
    profile = _testnet_profile()
    with pytest.raises(BrokerError, match="live_mode"):
        DeltaTestnetBroker(profile, SimpleNamespace(live_mode=True, testnet_mode=True))
    with pytest.raises(BrokerError, match="testnet_mode"):
        DeltaTestnetBroker(profile, SimpleNamespace(live_mode=False, testnet_mode=False))
    with pytest.raises(BrokerError, match="testnet profile"):
        DeltaTestnetBroker(_paper_profile(), SimpleNamespace(live_mode=False, testnet_mode=True, delta_api_key="key", delta_api_secret="secret"))


def test_testnet_broker_delegates_and_requires_an_adapter():
    request = _request().model_copy(update={"execution_mode": ExecutionMode.TESTNET})
    decision = _decision().model_copy(update={"execution_mode": ExecutionMode.TESTNET})

    class Client:
        def cancel_order(self, order_id):
            return {"id": order_id, "status": "cancelled"}

        def fetch_order(self, order_id):
            return {"id": order_id}

        def fetch_positions(self):
            return [{"symbol": "BTCUSDT", "size": 1.0}]

        def fetch_balances(self):
            return [{"asset": "INR", "balance": 10_000.0}]

    class Adapter:
        def submit(self, request, decision):
            return _result(request).model_copy(update={"execution_mode": ExecutionMode.TESTNET})

        client = Client()

    broker = DeltaTestnetBroker(
        _testnet_profile(),
        SimpleNamespace(live_mode=False, testnet_mode=True, delta_api_key="key", delta_api_secret="secret"),
        adapter=Adapter(),
    )
    assert broker.place_order(request, decision).venue == "paper"
    assert broker.cancel_order("7")["status"] == "cancelled"
    assert broker.get_order("7") == {"id": 7}
    assert broker.get_positions()[0]["symbol"] == "BTCUSDT"
    assert broker.get_balances()[0]["balance"] == pytest.approx(10_000.0)
    bare = DeltaTestnetBroker(
        None,
        SimpleNamespace(live_mode=False, testnet_mode=True, delta_api_key="key", delta_api_secret="secret"),
    )
    with pytest.raises(BrokerNotConfigured):
        bare.place_order(request, decision)


def test_live_broker_is_unconstructable():
    with pytest.raises(LiveBrokerDisabled):
        DeltaLiveBroker()
    with pytest.raises(LiveBrokerDisabled):
        broker_for_profile(SimpleNamespace(mode="live"))


def test_broker_factory_selects_only_paper_or_testnet():
    assert isinstance(broker_for_profile(_paper_profile()), PaperBroker)
    assert isinstance(
        broker_for_profile(
            _testnet_profile(),
            settings=SimpleNamespace(live_mode=False, testnet_mode=True, delta_api_key="key", delta_api_secret="secret"),
        ),
        DeltaTestnetBroker,
    )


def _signal():
    now = int(time.time())
    return {
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


def test_portfolio_gate_can_veto_an_otherwise_approved_order():
    profile = _paper_profile()
    risk = RiskManager(starting_equity=10_000.0, settings=profile.risk)
    guard = TradeGuard(
        profile,
        risk,
        portfolio_manager=PortfolioManager(PortfolioLimits(max_positions=0)),
    )
    with pytest.raises(ExecutionGuardError, match="portfolio: maximum open positions"):
        guard.check(_signal())


def test_default_portfolio_gate_allows_a_valid_order():
    profile = _paper_profile()
    risk = RiskManager(starting_equity=10_000.0, settings=profile.risk)
    guarded = TradeGuard(profile, risk).check(_signal())
    assert guarded.size == pytest.approx(50.0)


