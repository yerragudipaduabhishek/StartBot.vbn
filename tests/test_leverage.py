"""Tests for the dynamic-leverage service (AI advisor vs deterministic risk cap)."""

from app.analysis import leverage as lev


def test_ai_10x_vs_risk_5x_capped_to_5x():
    ctx = lev.LeverageContext(
        trend_score=1.0,
        adx=50.0,
        atr_pct=0.5,
        confidence=1.0,
        momentum=1.0,
        positioning=1.0,
        liquidity=1.0,
        stop_distance_pct=0.5,
        drawdown_pct=0.0,
    )
    # Drive the advisor to its ceiling.
    assert ctx.has_core()
    ai = lev.ai_recommended_leverage(ctx)
    assert ai is not None and ai >= 10.0
    risk_max = lev.risk_engine_max_leverage(ctx.atr_pct, max_leverage_setting=5.0)
    assert risk_max == 5.0
    res = lev.resolve_leverage(ctx, max_leverage_setting=5.0)
    assert res["effective"] == 5.0
    assert res["status"] == lev.STATUS_CAPPED


def test_ai_3x_vs_risk_5x_stays_at_3x():
    ctx = lev.LeverageContext(
        trend_score=0.3,
        adx=15.0,
        atr_pct=1.0,
        confidence=0.6,
        momentum=0.2,
        positioning=0.4,
        liquidity=0.5,
        stop_distance_pct=1.5,
        drawdown_pct=0.0,
    )
    res = lev.resolve_leverage(ctx, max_leverage_setting=5.0)
    assert res["ai_recommended"] < 5.0
    assert res["risk_max"] == 5.0
    assert res["effective"] == res["ai_recommended"]
    assert res["status"] == lev.STATUS_WITHIN
    # A modest recommendation around ~3x as a sanity band.
    assert res["ai_recommended"] >= 1.0


def test_no_data_returns_unavailable():
    ctx = lev.LeverageContext(trend_score=None, adx=None, atr_pct=None, confidence=None)
    assert not ctx.has_core()
    res = lev.resolve_leverage(ctx, max_leverage_setting=5.0)
    assert res["status"] == lev.STATUS_UNAVAILABLE
    assert res["ai_recommended"] is None
    assert res["effective"] is None


def test_high_volatility_reduces_risk_engine_max():
    risk_max = lev.risk_engine_max_leverage(3.0, max_leverage_setting=5.0)
    assert risk_max < 5.0
    assert risk_max >= 1.0
    # Normal volatility keeps the full cap.
    assert lev.risk_engine_max_leverage(1.0, max_leverage_setting=5.0) == 5.0


def test_effective_is_min_of_ai_and_risk_max():
    ctx = lev.LeverageContext(
        trend_score=1.0,
        adx=50.0,
        atr_pct=1.0,
        confidence=1.0,
        momentum=1.0,
        positioning=1.0,
        liquidity=1.0,
        stop_distance_pct=0.5,
        drawdown_pct=0.0,
    )
    res = lev.resolve_leverage(ctx, max_leverage_setting=3.0)
    assert res["effective"] == 3.0
    assert res["status"] == lev.STATUS_CAPPED


def test_no_signal_blocks_leverage():
    ctx = lev.LeverageContext(
        trend_score=1.0,
        adx=50.0,
        atr_pct=1.0,
        confidence=0.0,
    )
    res = lev.resolve_leverage(ctx, max_leverage_setting=5.0)
    assert res["status"] == lev.STATUS_UNAVAILABLE
    assert res["effective"] is None


def test_advisor_is_not_linear_percentage_mapping():
    # Two contexts differing only in trend strength should not map confidence
    # 1:1 to leverage; the composite blends several factors.
    strong = lev.LeverageContext(trend_score=1.0, adx=50.0, atr_pct=0.5, confidence=0.7, momentum=1.0, positioning=1.0, liquidity=1.0, stop_distance_pct=0.5)
    weak = lev.LeverageContext(trend_score=0.1, adx=8.0, atr_pct=3.5, confidence=0.7, momentum=-1.0, positioning=0.0, liquidity=0.0, stop_distance_pct=4.0)
    a = lev.ai_recommended_leverage(strong)
    b = lev.ai_recommended_leverage(weak)
    assert a is not None and b is not None
    assert a > b
    assert a <= lev.AI_MAX_LEVERAGE
    assert b >= lev.AI_MIN_LEVERAGE


def test_labels():
    assert lev.market_risk_label(0.8) == "MODERATE"
    assert lev.trend_label("long") == "BULLISH"
    assert lev.volatility_label(1.0) == "NORMAL"
    assert lev.market_risk_label(None) == "UNKNOWN"