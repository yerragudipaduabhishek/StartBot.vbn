"""Market regime tagging for validation across different conditions.

Labels every bar with a regime like "trend_up|high_vol" so a backtest can be
reported per regime instead of averaging away the good/bad periods. Thresholds
are data-adaptive (quantiles over the analyzed window), avoiding magic numbers.
"""

import numpy as np
import pandas as pd

TREND_LABELS = ("trend_down", "range", "trend_up")
VOL_LABELS = ("low_vol", "high_vol")


def tag_regimes(
    df: pd.DataFrame,
    trend_lookback: int = 48,
    vol_lookback: int = 48,
    warmup: int = 30,
) -> pd.DataFrame:
    """Return a copy of `df` with a `regime` column:

      trend direction:  pct change of a slow EMA over `trend_lookback` bars,
                        tercile-split into trend_down / range / trend_up.
      volatility:       realized vol (rolling std of log returns) over
                        `vol_lookback`, split at the median into low/high.
    """
    out = df.copy()
    close = out["close"]

    ema_s = close.ewm(span=trend_lookback, adjust=False).mean()
    slope = (ema_s - ema_s.shift(trend_lookback)) / ema_s.shift(trend_lookback).replace(0, np.nan)

    log_ret = np.log(close / close.shift(1))
    rv = log_ret.rolling(vol_lookback, min_periods=vol_lookback // 2).std().apply(lambda x: x) * np.sqrt(vol_lookback)

    valid = slice(warmup, None)
    slope_vals = slope.iloc[valid].dropna().to_numpy()
    rv_vals = rv.iloc[valid].dropna().to_numpy()
    if len(slope_vals) == 0 or len(rv_vals) == 0:
        out["regime"] = "range|low_vol"
        return out

    q1, q2 = np.percentile(slope_vals, [33.33, 66.67])
    vol_med = np.percentile(rv_vals, 50)

    def label(i: int) -> str:
        s = slope.iloc[i]
        v = rv.iloc[i]
        if pd.isna(s):
            trend = "range"
        elif s < q1:
            trend = "trend_down"
        elif s > q2:
            trend = "trend_up"
        else:
            trend = "range"
        vol = "high_vol" if pd.notna(v) and v > vol_med else "low_vol"
        return f"{trend}|{vol}"

    out["regime"] = [label(i) for i in range(len(out))]
    return out


def regime_summary(trades: list) -> dict[str, dict]:
    """Aggregate backtest trades by the regime they were entered into."""
    groups: dict[str, list] = {}
    for t in trades:
        groups.setdefault(t.regime or "range|low_vol", []).append(t)
    out: dict[str, dict] = {}
    for regime, ts in groups.items():
        pnl = sum(t.pnl for t in ts)
        wins = sum(1 for t in ts if t.pnl > 0)
        out[regime] = {
            "n": len(ts),
            "pnl": round(pnl, 2),
            "win_rate": round(wins / len(ts), 3),
        }
    return out