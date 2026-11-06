"""Structure agent: swing-pivot market structure (HH/HL vs LH/LL)."""

import pandas as pd

from app.agents.base import Vote


def _pivots(df: pd.DataFrame, window: int = 3) -> tuple[list[int], list[int]]:
    """Indices of swing highs/lows. Vectorized rolling-max/min comparison:
    a bar is a pivot when its high/low equals the max/min of the surrounding
    2*window+1 bars (edges excluded, matching slice semantics)."""
    n = len(df)
    size = 2 * window + 1
    if n < size:
        return [], []
    h_max = df["high"].rolling(size, center=True).max()
    l_min = df["low"].rolling(size, center=True).min()
    hi_vals = df["high"].to_numpy()
    lo_vals = df["low"].to_numpy()
    highs = [i for i in range(window, n - window) if hi_vals[i] == h_max.iloc[i]]
    lows = [i for i in range(window, n - window) if lo_vals[i] == l_min.iloc[i]]
    return highs, lows


class StructureAgent:
    name = "structure"

    def analyze(self, df: pd.DataFrame, window: int = 3) -> Vote:
        highs, lows = _pivots(df, window)
        close = df["close"].iloc[-1]

        # Trend of pivot highs and pivot lows.
        ph = [df["high"].iloc[i] for i in highs]
        pl = [df["low"].iloc[i] for i in lows]

        high_still_rising = len(ph) >= 2 and ph[-1] > ph[len(ph) // 2]
        low_still_rising = len(pl) >= 2 and pl[-1] > pl[len(pl) // 2]
        high_falling = len(ph) >= 2 and ph[-1] < ph[len(ph) // 2]
        low_falling = len(pl) >= 2 and pl[-1] < pl[len(pl) // 2]

        if high_still_rising and low_still_rising:
            score, label = 1.0, "bullish structure: higher highs and higher lows"
        elif high_falling and low_falling:
            score, label = -1.0, "bearish structure: lower highs and lower lows"
        elif high_still_rising and low_falling:
            score, label = 0.3, "mixed structure: higher highs but lower lows"
        elif high_falling and low_still_rising:
            score, label = 0.3, "mixed structure: lower highs but higher lows (range)"
        else:
            score, label = 0.0, "insufficient pivots to judge structure"

        # Range position adds a mild lean when no clear structure.
        if score == 0.0 and len(df) >= 30:
            lo = df["low"].iloc[-30:].min()
            hi = df["high"].iloc[-30:].max()
            pos = (close - lo) / (hi - lo) if hi > lo else 0.5
            score = round((pos - 0.5) * 0.5, 3)
            label += f" | price at {pos:.0%} of 30-bar range"

        confidence = 0.8 if abs(score) >= 0.9 else (0.5 if abs(score) >= 0.3 else 0.3)

        return Vote(
            agent=self.name,
            direction="long" if score > 0.25 else ("short" if score < -0.25 else "neutral"),
            score=round(score, 3),
            confidence=round(confidence, 3),
            explanation=label,
            details={"pivot_highs": [round(v, 1) for v in ph], "pivot_lows": [round(v, 1) for v in pl]},
        )