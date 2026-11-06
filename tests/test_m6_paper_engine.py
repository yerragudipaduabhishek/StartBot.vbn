"""Milestone 6 — deterministic paper engine: persistence, P&L, fees, slippage.

Validates that the paper account is:
- Deterministic: entry/exit are simulated locally off real candles with a clear
  P&L formula, fees (both sides) and adverse slippage.
- Persisted: restarting the engine (e.g. a VPS/container restart) restores the
  account balance and the open position, and NEVER auto-generates an order.
- Safe: the paper path never invokes a Delta order endpoint and never touches
  an authenticated client.

The tests use explicit fee/slippage values so every number is verifiable.
"""

import inspect
import time

import pytest
from sqlalchemy import func, select

from app.config.settings import get_settings
from app.database.database import SessionLocal
from app.database.models import PaperTrade
from app.trading.executor import Executor
from app.trading.paper_trader import PaperTrader


def _trader(**kw) -> PaperTrader:
    defaults = dict(
        starting_equity=5000.0,
        fee_pct=0.05,
        slippage_pct=0.0,
        session_factory=SessionLocal,
    )
    defaults.update(kw)
    return PaperTrader(**defaults)


def _signal(**overrides):
    sig = {
        "direction": "long",
        "entry": 50000,
        "stop_loss": 49000,
        "target": 52000,
        "accepted_size": 25,
        "risk_allowed": True,
        "confidence": 0.8,
        "score": 1.0,
        "candle_time": int(time.time()),
        "data_quality": {"status": "streaming", "price_valid": True, "source": "test", "last_candle_age_secs": 10},
        "product_id": 0,
    }
    sig.update(overrides)
    return sig


# ================================================================ persistence


def test_open_position_persists_across_restart():
    t1 = _trader()
    assert t1.open_position("long", 5, 50000, 49500, 51000)

    t2 = _trader()
    assert t2.position is not None
    assert t2.position["side"] == "long"
    assert t2.position["symbol"] == "BTCUSDT"
    assert t2.position["entry_price"] == 50000
    assert t2.position["stop_loss"] == 49500
    assert t2.position["take_profit"] == 51000
    assert t2.position["status"] == "open"


def test_restart_restores_stale_mark_not_a_fresh_one():
    t1 = _trader()
    t1.open_position("long", 5, 50000, 49500, 51000)
    t1.update_position(50100)
    t2 = _trader()
    assert t2.position["current_price"] == 50100
    assert t2.position["reason"] == "restored"


def test_restart_never_generates_an_order():
    t1 = _trader()
    t1.open_position("long", 5, 50000, 49500, 51000)
    t2 = _trader()
    with SessionLocal() as db:
        assert db.execute(select(func.count()).select_from(PaperTrade)).scalar_one() == 0
    assert t2.position is not None  # kept open, nothing auto-placed/closed


def test_closed_trade_and_account_balance_persist():
    t1 = _trader(fee_pct=0.0, slippage_pct=0.0)
    t1.open_position("long", 5, 100000, 99000, 110000)
    res = t1.close_position(105000, "signal")
    assert res["pnl"] == pytest.approx(5 * 5000 * 0.001)

    t2 = _trader(fee_pct=0.0, slippage_pct=0.0)
    assert t2.realized_pnl == pytest.approx(5 * 5000 * 0.001)
    assert t2.position is None
    assert t2.equity == pytest.approx(5000.0 + 5 * 5000 * 0.001)
    with SessionLocal() as db:
        rows = db.execute(select(PaperTrade)).scalars().all()
        assert len(rows) == 1
        assert rows[0].status == "closed"
        assert rows[0].pnl == pytest.approx(25.0)
        assert rows[0].symbol == "BTCUSDT"
        assert rows[0].leverage is not None


# ============================================================ P&L fees/margins


def test_unrealized_pnl_is_gross_minus_entry_fee():
    t = _trader(fee_pct=0.05, slippage_pct=0.0)  # 0.05% each side
    t.open_position("long", 10, 50000, 49500, 51000)
    # entry fee = 10 * 50000 * 0.001 * 0.05/100 = 0.25
    pos = t.update_position(50100)
    assert pos["unrealized_pnl"] == pytest.approx(10 * 100 * 0.001 - 0.25)
    assert t.unrealized_pnl == pytest.approx(10 * 100 * 0.001 - 0.25)
    assert t.total_equity == pytest.approx(5000.0 + 10 * 100 * 0.001 - 0.25)


def test_realized_pnl_updates_balance_and_total_equity():
    t = _trader(fee_pct=0.0, slippage_pct=0.0)
    t.open_position("long", 5, 100000, 99000, 110000)
    res = t.close_position(105000, "signal")
    assert res["pnl"] == pytest.approx(25.0)
    assert res["exit_price"] == pytest.approx(105000.0)
    assert t.realized_pnl == pytest.approx(25.0)
    assert t.balance == pytest.approx(5025.0)
    assert t.total_equity == pytest.approx(5025.0)


def test_fees_apply_on_entry_and_exit():
    t = _trader(fee_pct=0.05, slippage_pct=0.0)
    t.open_position("long", 10, 50000, 49500, 51000)
    res = t.on_candle({"high": 51050, "low": 50500, "close": 51000})
    assert res["reason"] == "target"
    # entry fee = 10*50000*0.001*0.05/100 = 0.25
    # exit fee = 10*51000*0.001*0.05/100 = 0.255
    # gross = 10*1000*0.001 = 10.0
    assert res["fees"] == pytest.approx(0.25 + 0.255)
    assert res["pnl"] == pytest.approx(10.0 - 0.25 - 0.255)


def test_slippage_worsens_long_entry_and_exit():
    t = _trader(slippage_pct=1.0)  # 1% adverse both sides
    t.open_position("long", 10, 50000, 47500, 55000)
    assert t.position["entry_price"] == pytest.approx(50500.0)  # bought higher
    assert t.position["slippage"] == 100  # recorded in bps
    res = t.close_position(51000, "signal")
    assert res["exit_price"] == pytest.approx(50490.0)  # sold lower
    assert res["slippage"] == 100


def test_slippage_worsens_short_entry_and_exit():
    t = _trader(slippage_pct=0.5)
    t.open_position("short", 5, 50000, 50500, 49000)
    assert t.position["entry_price"] == pytest.approx(49750.0)  # sold lower
    assert t.position["slippage"] == 50
    res = t.close_position(49800, "signal")
    assert res["exit_price"] == pytest.approx(50049.0)  # buy back higher
    assert res["slippage"] == 50


def test_loss_trade_reduces_balance():
    t = _trader(fee_pct=0.0, slippage_pct=0.0)
    t.open_position("long", 5, 100000, 99000, 100001)
    res = t.on_candle({"high": 100000, "low": 98900, "close": 99000})
    assert res["reason"] == "stop"
    # stopped at 99000, loss = 5 * (-1000) * 0.001 = -5
    assert res["pnl"] == pytest.approx(-5.0)
    assert t.balance == pytest.approx(4995.0)


def test_position_model_has_full_field_set():
    t = _trader()
    t.open_position("long", 5, 50000, 49500, 51000)
    pos = t.position
    for key in (
        "symbol", "side", "direction", "size", "quantity", "entry_price",
        "current_price", "notional", "leverage", "stop_loss", "target",
        "take_profit", "unrealized_pnl", "realized_pnl", "fees", "slippage",
        "opened_at", "updated_at", "status", "reason",
    ):
        assert key in pos, f"position missing {key}"
    assert pos["notional"] == pytest.approx(5 * 50000 * 0.001)


def test_extended_snapshot_contract():
    t = _trader(fee_pct=0.05, slippage_pct=0.02)
    snap = t.extended_snapshot()
    for key in (
        "balance", "starting_equity", "realized_pnl", "unrealized_pnl",
        "total_equity", "currency", "fee_pct", "slippage_pct",
        "fund_separate", "open_position", "position_count", "trades_count",
    ):
        assert key in snap, f"extended snapshot missing {key}"
    assert snap["starting_equity"] == 5000.0
    assert snap["currency"] == "INR"
    assert snap["fund_separate"] is True


# ===================================================================== safety


def test_paper_trader_never_references_delta_order_code():
    src = inspect.getsource(PaperTrader)
    assert "DeltaClient" not in src
    assert "delta_client" not in src
    assert "/v2/orders" not in src
    assert "place_order" not in src


def test_executor_paper_path_never_calls_order_client():
    class SpyDelta:
        def __init__(self):
            self.calls: list[tuple] = []

        def place_order(self, *a, **k):
            self.calls.append(("place_order", a, k))
            return {"result": {"id": "FAKE"}}

        def cancel_order(self, *a, **k):
            self.calls.append(("cancel_order", a, k))
            return {}

    spy = SpyDelta()
    settings = get_settings().model_copy(update={"live_mode": False, "testnet_mode": False, "delta_api_key": "", "delta_api_secret": ""})
    ex = Executor(settings=settings, paper=_trader(session_factory=None), order_client=spy)
    res = ex.execute(_signal(), price=50000, confirm_real_orders=True)
    assert res.venue == "paper"
    assert spy.calls == []


def test_executor_live_flags_but_no_keys_still_paper():
    class SpyDelta:
        def __init__(self):
            self.calls = []

        def place_order(self, *a, **k):
            self.calls.append(a)
            return {"result": {"id": "FAKE"}}

    spy = SpyDelta()
    settings = get_settings().model_copy(update={"live_mode": True, "testnet_mode": False, "delta_api_key": "", "delta_api_secret": ""})
    ex = Executor(settings=settings, paper=_trader(session_factory=None), order_client=spy)
    res = ex.execute(_signal(), price=50000, confirm_real_orders=True)
    assert res.venue == "paper"
    assert spy.calls == []


def test_safety_flags_remain_unchanged():
    s = get_settings()
    assert s.live_mode is False
    assert s.testnet_mode is True
    assert s.real_order_confirmation is False