"""Dynamic leverage service for the dashboard.

Effective leverage = min(AI recommendation, Risk Engine maximum).

The Risk Engine maximum is *deterministic* (volatility + settings based) and is
the final gate: the AI advisor can never suggest more than the risk engine
allows. This is not a raw "% confidence = leverage x" mapping — the advisor
blends real market context (trend strength, momentum, volatility, positioning,
liquidity, stop distance, drawdown) into an explainable recommendation.
"""

from __future__ import annotations

from dataclasses import dataclass

AI_MAX_LEVERAGE = 10.0
AI_MIN_LEVERAGE = 1.0
RISK_NORMAL_VOL_PCT = 1.0  # ATR/price % considered "normal" volatility
RISK_SCALE_FLOOR = 0.25    # worst-case leverage multiplier at extreme vol

STATUS_CAPPED = "CAPPED BY RISK ENGINE"
STATUS_WITHIN = "WITHIN RISK LIMIT"
STATUS_UNAVAILABLE = "DATA UNAVAILABLE"


@dataclass
class LeverageContext:
    trend_score: float | None     # trend agent score, -1..1
    adx: float | None             # 0..~50
    atr_pct: float | None         # ATR / price * 100
    confidence: float | None      # decision confidence 0..1
    momentum: float | None = None        # -1..1 (MACD histogram normalized), optional
    positioning: float | None = None      # 0..1 (funding + OI conviction), optional
    liquidity: float | None = None        # 0..1 (volume / OI depth), optional
    stop_distance_pct: float | None = None  # |entry - stop| / entry * 100, optional
    drawdown_pct: float = 0.0     # paper account drawdown, optional

    def has_core(self) -> bool:
        return (
            self.trend_score is not None
            and self.adx is not None
            and self.atr_pct is not None
            and self.confidence is not None
        )


def _clip(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def ai_recommended_leverage(ctx: LeverageContext) -> float | None:
    """Deterministic context-driven advisor recommendation (never beyond cap)."""
    if not ctx.has_core():
        return None
    if ctx.atr_pct <= 0 or ctx.confidence <= 0:
        return None

    trend = _clip(0.5 * abs(_clip(ctx.trend_score, -1, 1)) + 0.5 * _clip(ctx.adx / 50.0, 0, 1), 0, 1)
    vol = _clip(1.5 / max(ctx.atr_pct, 0.2), 0, 1)
    momentum = _clip((ctx.momentum + 1.0) / 2.0, 0, 1) if ctx.momentum is not None else 0.5
    positioning = _clip(ctx.positioning, 0, 1) if ctx.positioning is not None else 0.5
    liquidity = _clip(ctx.liquidity, 0, 1) if ctx.liquidity is not None else 0.5
    confidence = _clip(ctx.confidence, 0, 1)
    stop = _clip(1.0 / max(ctx.stop_distance_pct, 0.1), 0.2, 1) if ctx.stop_distance_pct else 0.5
    drawdown = _clip(1.0 - _clip(ctx.drawdown_pct / 20.0, 0, 1), 0.3, 1.0)

    weighted = (
        0.25 * trend
        + 0.15 * vol
        + 0.10 * momentum
        + 0.10 * positioning
        + 0.10 * liquidity
        + 0.20 * confidence
        + 0.10 * stop
    )
    # Quadratic curve: a *strong* composite reaches the ceiling while *average*
    # conditions suggest conservative leverage. Avoids a naive linear
    # "percentage = leverage" mapping.
    composite = _clip(weighted, 0, 1) ** 2
    rec = AI_MIN_LEVERAGE + (AI_MAX_LEVERAGE - AI_MIN_LEVERAGE) * composite
    return round(_clip(rec * drawdown, AI_MIN_LEVERAGE, AI_MAX_LEVERAGE), 1)


def risk_engine_max_leverage(atr_pct: float | None, max_leverage_setting: float = 5.0) -> float | None:
    """Deterministic hard cap: settings-max scaled down at high volatility."""
    if atr_pct is None or atr_pct <= 0:
        return None
    scale = _clip(RISK_NORMAL_VOL_PCT / max(atr_pct, 1e-3), RISK_SCALE_FLOOR, 1.0)
    return round(max(1.0, max_leverage_setting * scale), 2)


def resolve_leverage(
    ctx: LeverageContext,
    max_leverage_setting: float = 5.0,
    allow_when_no_trade: bool = False,
) -> dict:
    """Compute ai_recommended, risk_max, effective, status + factors.

    `allow_when_no_trade=False` forces DATA UNAVAILABLE when there is no
    actionable signal (the UI should not show a leverage number without one).
    """
    if not ctx.has_core():
        return {
            "ai_recommended": None,
            "risk_max": None,
            "effective": None,
            "status": STATUS_UNAVAILABLE,
            "reason": "insufficient market data for a leverage assessment",
            "factors": {},
        }

    if not allow_when_no_trade and ctx.confidence <= 0:
        return {
            "ai_recommended": None,
            "risk_max": None,
            "effective": None,
            "status": STATUS_UNAVAILABLE,
            "reason": "no actionable signal",
            "factors": {},
        }

    ai = ai_recommended_leverage(ctx)
    risk_max = risk_engine_max_leverage(ctx.atr_pct, max_leverage_setting)
    if ai is None or risk_max is None:
        return {
            "ai_recommended": ai,
            "risk_max": risk_max,
            "effective": None,
            "status": STATUS_UNAVAILABLE,
            "reason": "insufficient market data for a leverage assessment",
            "factors": {},
        }

    effective = round(min(ai, risk_max), 2)
    status = STATUS_CAPPED if ai > risk_max + 1e-9 else STATUS_WITHIN
    factors = {
        "trend_score": ctx.trend_score,
        "adx": ctx.adx,
        "atr_pct": ctx.atr_pct,
        "confidence": ctx.confidence,
        "momentum": ctx.momentum,
        "positioning": ctx.positioning,
        "liquidity": ctx.liquidity,
        "stop_distance_pct": ctx.stop_distance_pct,
        "drawdown_pct": ctx.drawdown_pct,
    }
    return {
        "ai_recommended": ai,
        "risk_max": risk_max,
        "effective": effective,
        "status": status,
        "reason": (
            f"Ai advisor proposed {ai}x, risk engine allows {risk_max}x "
            f"-> effective {effective}x ({status})."
        ),
        "factors": factors,
    }


def market_risk_label(atr_pct: float | None) -> str:
    if atr_pct is None:
        return "UNKNOWN"
    if atr_pct < 0.6:
        return "LOW"
    if atr_pct < 1.2:
        return "MODERATE"
    if atr_pct < 2.0:
        return "HIGH"
    return "VERY HIGH"


def trend_label(direction: str | None) -> str:
    d = (direction or "").lower()
    if d in ("long", "buy"):
        return "BULLISH"
    if d in ("short", "sell"):
        return "BEARISH"
    return "NEUTRAL"


def volatility_label(atr_pct: float | None) -> str:
    if atr_pct is None:
        return "UNKNOWN"
    if atr_pct < 0.6:
        return "LOW"
    if atr_pct < 1.2:
        return "NORMAL"
    if atr_pct < 2.0:
        return "HIGH"
    return "VERY HIGH"