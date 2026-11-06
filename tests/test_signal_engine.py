from unittest.mock import MagicMock

from app.strategy.signal_engine import SignalEngine


def _market(candles):
    md = MagicMock()
    md.candles = candles
    md.ticker = {"funding_rate": 0.0, "funding_rate_8h": 0.0, "oi_contracts": "100", "oi_change_usd_6h": 0.0, "close": candles[-1]["close"], "oi_value_usd": "10000000"}
    return md


def test_signal_engine_insufficient_data():
    md = _market([{"time": 1, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}] * 10)
    out = SignalEngine().generate(md)
    assert out["direction"] == "none"
    assert "insufficient" in out["reason"]


def test_signal_engine_full_block():
    candles = []
    base = 50000.0
    for i in range(120):
        c = base + i * 25
        candles.append({"time": 1700000000 + i * 300, "open": c, "high": c + 40, "low": c - 40, "close": c, "volume": 10.0})
    out = SignalEngine().generate(_market(candles))
    assert out["direction"] in ("long", "short", "none")
    assert out["entry"] is not None
    assert out["explanation"].startswith("  Trend:")
    assert "Decision:" in out["explanation"]
    assert len(out["votes"]) == 4
    if out["direction"] == "long":
        assert out["stop_loss"] < out["entry"] < out["target"]
    elif out["direction"] == "short":
        assert out["stop_loss"] > out["entry"] > out["target"]


def test_signal_engine_returns_explanation():
    candles = []
    for i in range(60):
        candles.append({"time": 1700000000 + i * 300, "open": 60000.0, "high": 60050.0, "low": 59950.0, "close": 60000.0, "volume": 10.0})
    out = SignalEngine().generate(_market(candles))
    assert "Reason:" in out["explanation"]


def test_signal_engine_with_risk_manager():
    from app.risk.config import RiskSettings
    from app.risk.risk_manager import RiskManager

    candles = []
    base = 50000.0
    for i in range(120):
        c = base + i * 25
        candles.append({"time": 1700000000 + i * 300, "open": c, "high": c + 40, "low": c - 40, "close": c, "volume": 10.0})
    rm = RiskManager(starting_equity=10000, settings=RiskSettings(min_confidence=0.0))
    engine = SignalEngine(risk_manager=rm)
    out = engine.generate(_market(candles))
    assert "Risk:" in out["explanation"]
    assert "accepted_size" in out