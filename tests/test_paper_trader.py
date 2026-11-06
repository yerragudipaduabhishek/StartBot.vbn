import time

import pytest

from app.config.settings import get_settings
from app.config.trading import profile_from_settings
from app.risk.risk_manager import RiskManager
from app.trading.paper_trader import PaperTrader


def _paper():
    profile = profile_from_settings(get_settings(), mode="paper")
    risk = RiskManager(starting_equity=5000.0, settings=profile.risk)
    return PaperTrader(fee_pct=0.0, profile=profile, risk_manager=risk)


def _streaming():
    return {"status": "streaming", "price_valid": True, "source": "test", "last_candle_age_secs": 10}


def test_open_and_target_profit():
    pt = PaperTrader(starting_equity=10000, fee_pct=0.0)
    assert pt.open_position("long", 10, 50000, 49800, 50200)
    assert pt.position is not None
    res = pt.on_candle({"high": 50250, "low": 50050, "close": 50200})
    assert res["reason"] == "target"
    assert res["pnl"] > 0
    assert pt.position is None


def test_stop_loss_hit():
    pt = PaperTrader(starting_equity=10000, fee_pct=0.0)
    pt.open_position("short", 10, 50000, 50200, 49800)
    res = pt.on_candle({"high": 50300, "low": 50100, "close": 50250})
    assert res["reason"] == "stop"
    assert res["pnl"] < 0


def test_fees_reduce_pnl():
    pt = PaperTrader(starting_equity=10000, fee_pct=5.0)  # 5% each side
    pt.open_position("long", 10, 50000, 49800, 50200)
    res = pt.on_candle({"high": 50250, "low": 50050})
    # gross = 10 * 200 * 0.001 = 2; fees entry(25) + exit(25.1) on notionals
    assert res["pnl"] == pytest.approx(2.0 - 25.0 - 25.1, rel=1e-6)


def test_single_position_at_a_time():
    pt = PaperTrader()
    assert pt.open_position("long", 1, 100, 90, 110)
    assert not pt.open_position("long", 1, 100, 90, 110)
    assert pt.position is not None


def test_on_signal_opens_and_closes():
    pt = _paper()
    now = int(time.time())
    sig = {
        "direction": "long",
        "risk_allowed": True,
        "accepted_size": 125,
        "entry": 50000,
        "stop_loss": 49800,
        "target": 50200,
        "candle_time": now,
        "confidence": 0.8,
        "score": 1.0,
        "data_quality": _streaming(),
    }
    assert pt.on_signal(sig)["action"] == "open"
    assert pt.position is not None
    sig2 = {
        "direction": "none",
        "risk_allowed": False,
        "entry": 50000,
        "candle_time": now,
        "data_quality": _streaming(),
    }
    res = pt.on_signal(sig2)
    assert res["pnl"] == pytest.approx(0.0)  # closed at entry price, no fees
    assert pt.position is None


def test_equity_after_trades():
    pt = PaperTrader(starting_equity=10000, fee_pct=0.0)
    pt.open_position("long", 5, 50000, 49000, 52000)
    pt.on_candle({"high": 52000, "low": 51000})
    assert pt.equity == pytest.approx(10000 + 5 * 2000 * 0.001)