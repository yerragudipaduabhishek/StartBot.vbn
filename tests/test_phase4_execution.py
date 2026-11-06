import pytest
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.contracts import ExecutionMode, OrderRequest, OrderResult, PositionSize, RiskApprovalStatus, RiskDecision
from app.database.database import Base
from app.database.models import Fill, Order, OrderEvent, RiskDecisionRecord
from app.execution.broker import BrokerError
from app.execution.order_manager import OrderManager
from app.execution.order_store import OrderRepository, OrderStateError
from app.portfolio.manager import PortfolioLimits, PortfolioManager


def _db():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def test_portfolio_limits_are_fail_closed():
    manager = PortfolioManager(PortfolioLimits(max_positions=1, max_gross_notional=1000))
    assert manager.approve(symbol="BTCUSDT", notional=500, open_positions=0)[0]
    assert not manager.approve(symbol="BTCUSDT", notional=1500, open_positions=0)[0]
    assert not manager.approve(symbol="BTCUSDT", notional=500, open_positions=1)[0]


def test_order_manager_rejects_duplicate_active_intent():
    manager = OrderManager(router=object())
    manager._states["order-1"] = "filled"
    request = SimpleNamespace(client_order_id="order-1")
    with pytest.raises(BrokerError, match="duplicate"):
        manager.place(request, object())


def test_order_repository_state_machine_and_idempotent_fills():
    factory = _db()
    repo = OrderRepository(factory)
    order, created = repo.create_intent(
        client_order_id="c1",
        venue="paper",
        execution_mode="paper",
        symbol="BTCUSDT",
        side="buy",
        size=2.0,
        expected_price=100.0,
    )
    assert created and order.status == "created"
    assert repo.create_intent(
        client_order_id="c1",
        venue="paper",
        execution_mode="paper",
        symbol="BTCUSDT",
        side="buy",
        size=2.0,
    )[1] is False

    repo.transition("c1", "accepted", idempotency_key="c1:accepted")
    partial = repo.record_fill("c1", fill_key="f1", price=101.0, size=1.0, fee=0.1)
    assert partial.status == "partially_filled"
    assert partial.filled_size == pytest.approx(1.0)
    again = repo.record_fill("c1", fill_key="f1", price=101.0, size=1.0, fee=0.1)
    assert again.filled_size == pytest.approx(1.0)
    filled = repo.record_fill("c1", fill_key="f2", price=103.0, size=1.0, fee=0.1)
    assert filled.status == "filled"
    assert filled.avg_fill_price == pytest.approx(102.0)
    with pytest.raises(OrderStateError):
        repo.transition("c1", "accepted")
    with pytest.raises(OrderStateError):
        repo.record_fill("c1", fill_key="f3", price=100.0, size=1.0)
    with factory() as db:
        assert db.execute(select(Order)).scalars().one().status == "filled"
        assert len(db.execute(select(OrderEvent)).scalars().all()) == 4
        assert len(db.execute(select(Fill)).scalars().all()) == 2


def test_order_manager_persists_risk_decision_order_and_fill():
    factory = _db()

    class Router:
        mode = ExecutionMode.PAPER

        def submit(self, request, decision):
            return OrderResult(
                client_order_id=request.client_order_id,
                size=request.size,
                price=request.expected_price,
                fill_price=request.expected_price + 1.0,
                expected_price=request.expected_price,
                fees=0.25,
                venue="paper",
                status="accepted",
                order_state="filled",
                symbol=request.symbol,
                timeframe=request.timeframe,
                risk_decision_key=request.risk_decision_key,
                risk_approved=True,
                execution_mode=ExecutionMode.PAPER,
            )

    manager = OrderManager(router=Router(), session_factory=factory)
    request = OrderRequest(
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
    decision = RiskDecision(
        allowed=True,
        size=PositionSize(quantities=1.0, notional_usd=500.0, margin_usd=100.0, leverage=5.0),
        approval_status=RiskApprovalStatus.APPROVED,
        signal_key="signal-1",
        symbol="BTCUSDT",
        timeframe="1h",
        strategy_id="strategy_a",
        strategy_version="v1",
        execution_mode=ExecutionMode.PAPER,
    )
    result = manager.place(request, decision)
    assert result.order_state == "filled"
    with factory() as db:
        assert db.execute(select(RiskDecisionRecord)).scalars().one().allowed is True
        order = db.execute(select(Order)).scalars().one()
        assert order.status == "filled"
        assert order.avg_fill_price == pytest.approx(50_001.0)
        assert len(db.execute(select(Fill)).scalars().all()) == 1
    with pytest.raises(BrokerError, match="duplicate"):
        manager.place(request, decision)
