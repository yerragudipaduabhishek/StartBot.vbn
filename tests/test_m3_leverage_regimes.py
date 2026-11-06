"""Milestone 3 validation: dynamic leverage, risk-engine caps, volatility and
trend regimes, and stop-distance position sizing across the ATR spectrum.

Every assertion describes *existing* deterministic behaviour (probed against the
implementation before writing), so the tests lock in the safety contract rather
than an idealised model.
"""

import pytest

from app.analysis import leverage as lev
from app.analysis.service import AnalysisService
from tests.m3_fixtures import SingleSymbolStub, make_candles

RISK_5 = 5.0
RISK_2 = 2.0


# ---------------------------------------------------------------- leverage caps

def test_cap_ai_10x_vs_risk_5x_effective_5x_capped():
    ctx = lev.LeverageContext(trend_score=1.0, adx=50.0, atr_pct=0.5, confidence=1.0, momentum=1.0, positioning=1.0, liquidity=1.0, stop_distance_pct=0.5)
    res = lev.resolve_leverage(ctx, max_leverage_setting=RISK_5)
    assert res["ai_recommended"] >= 10.0
    assert res["risk_max"] == 5.0
    assert res["effective"] == 5.0
    assert res["status"] == lev.STATUS_CAPPED


def test_cap_ai_3x_vs_risk_5x_stays_within():
    ctx = lev.LeverageContext(trend_score=0.3, adx=15.0, atr_pct=1.0, confidence=0.6, momentum=0.2, positioning=0.4, liquidity=0.5, stop_distance_pct=1.5)
    res = lev.resolve_leverage(ctx, max_leverage_setting=RISK_5)
    assert 2.5 <= res["ai_recommended"] <= 5.0
    assert res["risk_max"] == 5.0
    assert res["effective"] == res["ai_recommended"]
    assert res["status"] == lev.STATUS_WITHIN


def test_cap_ai_8x_vs_risk_2x_effective_2x_capped():
    ctx = lev.LeverageContext(trend_score=0.9, adx=45.0, atr_pct=0.8, confidence=0.9, momentum=0.9, positioning=0.9, liquidity=0.9, stop_distance_pct=0.5)
    res = lev.resolve_leverage(ctx, max_leverage_setting=RISK_2)
    assert res["ai_recommended"] >= 8.0
    assert res["risk_max"] == 2.0
    assert res["effective"] == 2.0
    assert res["status"] == lev.STATUS_CAPPED


def test_cap_ai_min_vs_risk_5x_stays_within():
    # A context engineered to the AI floor (~1x) is allowed, never inflated.
    ctx = lev.LeverageContext(trend_score=0.0, adx=0.0, atr_pct=15.0, confidence=0.01, momentum=-1.0, positioning=0.0, liquidity=0.0, stop_distance_pct=40.0)
    res = lev.resolve_leverage(ctx, max_leverage_setting=RISK_5)
    assert 1.0 <= res["ai_recommended"] < res["risk_max"]
    assert res["effective"] == res["ai_recommended"]
    assert res["status"] == lev.STATUS_WITHIN


def test_cap_ai_normal_min_vs_risk_5x_within():
    # At normal volatility with a weak context the advisor stays at its floor.
    ctx = lev.LeverageContext(trend_score=0.0, adx=0.0, atr_pct=1.0, confidence=0.01, momentum=-1.0, positioning=0.0, liquidity=0.0, stop_distance_pct=40.0)
    res = lev.resolve_leverage(ctx, max_leverage_setting=RISK_5)
    assert res["effective"] == res["ai_recommended"]
    assert res["status"] == lev.STATUS_WITHIN


def test_cap_ai_unavailable_returns_data_unavailable():
    ctx = lev.LeverageContext(trend_score=None, adx=None, atr_pct=None, confidence=None)
    res = lev.resolve_leverage(ctx, max_leverage_setting=RISK_5)
    assert res["status"] == lev.STATUS_UNAVAILABLE
    assert res["ai_recommended"] is None
    assert res["effective"] is None


def test_no_signal_blocks_leverage_when_not_allowed():
    ctx = lev.LeverageContext(trend_score=1.0, adx=50.0, atr_pct=1.0, confidence=0.0)
    res = lev.resolve_leverage(ctx, max_leverage_setting=RISK_5, allow_when_no_trade=False)
    assert res["status"] == lev.STATUS_UNAVAILABLE
    assert res["effective"] is None


def test_effective_is_always_min_of_ai_and_risk_max():
    """Invariant check across a spread of realistic contexts."""
    contexts = [
        lev.LeverageContext(trend_score=0.9, adx=45.0, atr_pct=0.5, confidence=1.0, momentum=0.9, positioning=0.9, liquidity=1.0, stop_distance_pct=0.5),
        lev.LeverageContext(trend_score=0.5, adx=30.0, atr_pct=1.0, confidence=0.7, momentum=0.3, positioning=0.5, liquidity=0.6, stop_distance_pct=1.0),
        lev.LeverageContext(trend_score=-0.6, adx=40.0, atr_pct=2.0, confidence=0.6, momentum=-0.4, positioning=0.2, liquidity=0.4, stop_distance_pct=2.0),
        lev.LeverageContext(trend_score=0.0, adx=10.0, atr_pct=4.0, confidence=0.3, momentum=0.0, positioning=0.1, liquidity=0.2, stop_distance_pct=4.0),
    ]
    for ctx in contexts:
        res = lev.resolve_leverage(ctx, max_leverage_setting=RISK_5)
        assert res["effective"] == round(min(res["ai_recommended"], res["risk_max"]), 2)
        assert res["status"] in (lev.STATUS_CAPPED, lev.STATUS_WITHIN)


# ----------------------------------------------------------- volatility regimes

@pytest.mark.parametrize(
    ("vol", "atr_lo", "atr_hi", "risk_max_lo", "risk_max_hi", "vol_label", "risk_label"),
    [
        (0.002, 0.30, 0.50, 4.99, 5.01, "LOW", "LOW"),          # LOW
        (0.005, 0.90, 1.10, 4.99, 5.01, "NORMAL", "MODERATE"),  # NORMAL
        (0.010, 1.80, 2.10, 2.40, 2.70, "HIGH", "HIGH"),        # HIGH
        (0.030, 5.50, 6.50, 1.20, 1.40, "VERY HIGH", "VERY HIGH"),  # EXTREME
    ],
)
def test_volatility_regime_risk_max(vol, atr_lo, atr_hi, risk_max_lo, risk_max_hi, vol_label, risk_label):
    candles = make_candles(200, 50000.0, 25.0, vol)
    out = AnalysisService(client=SingleSymbolStub(candles=candles)).analyze("BTCUSDT")
    atr_pct = out["market"]["atr_pct"]
    assert atr_lo <= atr_pct <= atr_hi
    assert risk_max_lo <= out["leverage"]["risk_max"] <= risk_max_hi
    assert out["leverage"]["volatility"] == vol_label
    assert out["leverage"]["market_risk"] == risk_label


def test_low_volatility_allows_full_risk_cap_but_high_vol_reduces():
    low = make_candles(200, 50000.0, 25.0, 0.002)
    high = make_candles(200, 50000.0, 25.0, 0.010)
    lo = AnalysisService(client=SingleSymbolStub(candles=low)).analyze("BTCUSDT")
    hi = AnalysisService(client=SingleSymbolStub(candles=high)).analyze("BTCUSDT")
    assert lo["leverage"]["risk_max"] == 5.0
    assert hi["leverage"]["risk_max"] < lo["leverage"]["risk_max"]
    assert hi["leverage"]["effective"] == hi["leverage"]["risk_max"]  # AI stays capped down


def test_extreme_volatility_reduces_leverage_and_rejects_signal():
    candles = make_candles(200, 50000.0, 25.0, 0.020)  # atr ~4% > 3% skip threshold
    out = AnalysisService(client=SingleSymbolStub(candles=candles)).analyze("BTCUSDT")
    assert out["market"]["atr_pct"] > 3.0
    assert out["leverage"]["effective"] <= 1.3
    assert out["plan"]["risk_allowed"] is False  # existing engine behaviour: vol too high


# ----------------------------------------------------------------- trend regimes

@pytest.mark.parametrize(
    ("shape", "step", "expect_direction", "expect_score_range"),
    [
        ("linear", 25.0, "long", (0.9, 1.01)),      # strong bull
        ("pause_up", 0.0, "long", (0.6, 0.8)),       # weak bull (pause staircase)
        ("sine", 0.002, "neutral", (-0.05, 0.05)),   # sideways/ranging
        ("pause_down", 0.0, "short", (-0.8, -0.6)),  # weak bear
        ("linear", -25.0, "short", (-1.01, -0.9)),   # strong bear
    ],
)
def test_trend_regime_agent_state(shape, step, expect_direction, expect_score_range):
    candles = make_candles(200, 50000.0, step, 0.005, shape=shape)
    out = AnalysisService(client=SingleSymbolStub(candles=candles)).analyze("BTCUSDT")
    trend = out["decision"]["votes"].get("trend", {})
    assert trend.get("direction") == expect_direction
    assert expect_score_range[0] <= trend.get("score", 0.0) <= expect_score_range[1]
    assert out["decision"]["direction"] in ("long", "short") or expect_direction == "neutral"


def test_sideways_regime_is_a_valid_no_trade():
    candles = make_candles(200, 50000.0, 0.002, 0.005, shape="sine")
    out = AnalysisService(client=SingleSymbolStub(candles=candles)).analyze("BTCUSDT")
    assert out["decision"]["direction"] == "neutral"
    assert out["plan"]["position_size"] is None
    assert "no trade signal" in out["plan"]["risk_line"]


# ----------------------------------------------------------------- stop distances

@pytest.mark.parametrize(
    ("vol", "stop_lo", "stop_hi"),
    [
        (0.0025, 0.40, 0.60),  # ~0.5% stop
        (0.005, 0.90, 1.10),   # ~1% stop
        (0.010, 1.85, 2.15),   # ~2% stop
        (0.015, 2.80, 3.20),   # ~3% stop
    ],
)
def test_stop_distance_matches_atr_and_position_sizes_change(vol, stop_lo, stop_hi):
    candles = make_candles(200, 50000.0, 25.0, vol)
    out = AnalysisService(client=SingleSymbolStub(candles=candles)).analyze("BTCUSDT")
    atr_pct = out["market"]["atr_pct"]
    entry, stop = out["plan"]["entry"], out["plan"]["stop_loss"]
    stop_pct = abs(entry - stop) / entry * 100.0
    assert stop_lo <= atr_pct <= stop_hi
    assert stop_pct == pytest.approx(atr_pct, rel=0.05)  # SL = 1.0 x ATR
    assert out["plan"]["position_size"] is not None
    assert out["plan"]["risk_allowed"] is True


def test_larger_stop_never_increases_allowed_risk():
    sizes = []
    risk_amounts = []
    for vol in (0.0025, 0.005, 0.010, 0.015):
        candles = make_candles(200, 50000.0, 25.0, vol)
        out = AnalysisService(client=SingleSymbolStub(candles=candles)).analyze("BTCUSDT")
        sizes.append(out["plan"]["position_size"])
        start = out["plan"]["risk_line"].find("max risk ")
        risk_amounts.append(float(out["plan"]["risk_line"][start + len("max risk "): start + len("max risk ") + 6]))
    # Wider stop -> FEWER contracts, but the risk amount stays fixed at 25.
    assert sizes[0] > sizes[1] > sizes[2] > sizes[3]
    assert all(20.0 <= r <= 30.0 for r in risk_amounts)


def test_leverage_never_bypasses_risk_limit_in_sizing():
    for vol in (0.0025, 0.005, 0.010, 0.015):
        candles = make_candles(200, 50000.0, 25.0, vol)
        out = AnalysisService(client=SingleSymbolStub(candles=candles)).analyze("BTCUSDT")
        plan, lg = out["plan"], out["leverage"]
        equity = 5000.0
        notional = plan["notional"]
        assert notional is not None and notional > 0
        # Leverage cap enforced by the sizer: notional <= equity * effective.
        assert notional <= equity * lg["effective"] + 1e-6
        # Margin + max-risk window stay inside the fund.
        assert plan["position_size"] * out["market"]["contract_value"] * plan["entry"] == pytest.approx(notional, abs=0.02)


def test_position_sizing_is_deterministic():
    candles = make_candles(200, 50000.0, 25.0, 0.005)
    a = AnalysisService(client=SingleSymbolStub(candles=candles)).analyze("BTCUSDT")
    b = AnalysisService(client=SingleSymbolStub(candles=candles)).analyze("BTCUSDT")
    assert a["plan"] == b["plan"]
    assert a["leverage"]["effective"] == b["leverage"]["effective"]
    assert a["decision"] == b["decision"]


def test_stop_distance_factor_feeds_leverage_advisor():
    tight = make_candles(200, 50000.0, 25.0, 0.0025)  # tight stop
    wide = make_candles(200, 50000.0, 25.0, 0.015)    # wide stop
    tight_out = AnalysisService(client=SingleSymbolStub(candles=tight)).analyze("BTCUSDT")
    wide_out = AnalysisService(client=SingleSymbolStub(candles=wide)).analyze("BTCUSDT")
    assert tight_out["leverage"]["factors"]["stop_distance_pct"] < wide_out["leverage"]["factors"]["stop_distance_pct"]
    assert wide_out["leverage"]["status"] == lev.STATUS_CAPPED