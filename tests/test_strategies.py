"""Strategy registry: read-only metadata, never changes strategy logic."""

from app.analysis.strategies import STRATEGY_A, get_strategy, list_strategies


def test_strategy_a_locked_metadata():
    s = STRATEGY_A
    assert s["id"] == "strategy_a"
    assert s["name"] == "Strategy A"
    assert s["status"] == "UNVALIDATED"
    assert s["timeframe"] == "1H"
    p = s["parameters"]
    assert tuple(p["ema_spans"]) == (8, 40, 50)
    assert p["threshold"] == 0.25
    assert p["min_confidence"] == 0.5
    assert p["sl_atr_multiple"] == 1.0
    assert p["target_atr_multiple"] == 2.0
    assert p["weights"]["trend"] == 0.4
    assert p["weights"]["structure"] == 0.0


def test_get_strategy_and_list():
    assert get_strategy("strategy_a") is STRATEGY_A
    assert [s["id"] for s in list_strategies()] == ["strategy_a"]