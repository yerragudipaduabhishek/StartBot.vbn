import pytest

from app.risk.config import RiskSettings
from app.risk.position_sizer import size_position
from app.risk.risk_manager import RiskManager


def make_signal(direction="long", entry=50000.0, stop=49800.0, target=50200.0, confidence=0.8, prev_close=50000.0):
    return {"direction": direction, "entry": entry, "stop_loss": stop, "target": target, "confidence": confidence, "prev_close": prev_close}


def test_size_position_basic():
    s = size_position(equity=10000, entry=50000, stop=49800, risk_pct=1.0, contract_value=0.001, max_leverage=5)
    assert s is not None
    assert s.quantities == pytest.approx(500.0)  # $100 risk / ($200 * 0.001) = 500 contracts


def test_size_position_leverage_cap():
    s = size_position(equity=10000, entry=50000, stop=49900, risk_pct=10.0, contract_value=0.001, max_leverage=5)
    notional = s.quantities * 0.001 * 50000
    assert notional <= 10000 * 5


def test_size_position_invalid():
    assert size_position(0, 50000, 49800, 1.0, 0.001, 5) is None
    assert size_position(10000, 50000, 50000, 1.0, 0.001, 5) is None


def test_risk_manager_allows_good_signal():
    rm = RiskManager(starting_equity=10000)
    ok, size, reason = rm.evaluate(make_signal(), equity=10000)
    assert ok
    assert size is not None and size.quantities > 0


def test_risk_manager_rejects_neutral():
    rm = RiskManager(starting_equity=10000)
    ok, size, reason = rm.evaluate(make_signal(direction="none"), equity=10000)
    assert not ok
    assert "direction" in reason


def test_risk_manager_rejects_low_confidence():
    rm = RiskManager(starting_equity=10000, settings=RiskSettings(min_confidence=0.7))
    ok, _, reason = rm.evaluate(make_signal(confidence=0.5), equity=10000)
    assert not ok
    assert "confidence" in reason


def test_risk_manager_daily_loss_halts():
    rm = RiskManager(starting_equity=10000)
    rm.record_trade(pnl=-600)  # -6% > 5% cap
    ok, _, reason = rm.evaluate(make_signal(), equity=9400)
    assert not ok
    assert "daily loss" in reason


def test_risk_manager_drawdown_halts():
    rm = RiskManager(starting_equity=10000)
    rm.record_trade(pnl=-2600)  # -26% > 20% cap
    ok, _, reason = rm.evaluate(make_signal(), equity=7400)
    assert not ok
    assert "drawdown" in reason


def test_risk_manager_volatility_skip():
    rm = RiskManager(starting_equity=10000)
    ok, _, reason = rm.evaluate(make_signal(entry=50000, stop=45000, prev_close=50000), equity=10000)
    assert not ok
    assert "volatility" in reason


def test_risk_manager_max_positions():
    rm = RiskManager(starting_equity=10000)
    rm.open_positions = 1
    ok, _, reason = rm.evaluate(make_signal(), equity=10000)
    assert not ok
    assert "positions" in reason