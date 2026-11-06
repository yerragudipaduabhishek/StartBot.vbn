"""Timeframe helpers shared by the profile, feeds, risk and execution layers."""

from __future__ import annotations

SECONDS_PER_YEAR = 365 * 24 * 60 * 60

TIMEFRAME_SECONDS: dict[str, int] = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "6h": 21600,
    "12h": 43200,
    "1d": 86400,
}


def normalize_timeframe(timeframe: str) -> str:
    value = str(timeframe or "").strip().lower()
    if value not in TIMEFRAME_SECONDS:
        raise ValueError(f"unsupported timeframe: {timeframe!r}")
    return value


def timeframe_seconds(timeframe: str) -> int:
    return TIMEFRAME_SECONDS[normalize_timeframe(timeframe)]


def max_candle_age_seconds(timeframe: str, bars: float = 2.0) -> int:
    return int(timeframe_seconds(timeframe) * max(1.0, float(bars)))


def bars_per_year(timeframe: str) -> int:
    """Whole bars per 365-day year for annualizing per-bar returns."""
    return max(1, SECONDS_PER_YEAR // timeframe_seconds(timeframe))


def bars_per_year_for_seconds(seconds: float) -> float:
    interval = float(seconds)
    if not interval > 0:
        raise ValueError("bar interval must be positive")
    return SECONDS_PER_YEAR / interval


def infer_timeframe(seconds: float) -> str | None:
    """Map an observed bar interval back to a supported timeframe label."""
    interval = int(round(float(seconds)))
    for label, value in TIMEFRAME_SECONDS.items():
        if value == interval:
            return label
    return None


__all__ = [
    "SECONDS_PER_YEAR",
    "TIMEFRAME_SECONDS",
    "bars_per_year",
    "bars_per_year_for_seconds",
    "infer_timeframe",
    "max_candle_age_seconds",
    "normalize_timeframe",
    "timeframe_seconds",
]
