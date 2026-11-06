"""One causal feature pipeline for every strategy path.

Live signals, offline backtests and the paper soak runner must compute exactly
the same features from exactly the same candle history. This module centralises
that transformation and rejects non-causal or malformed input early.
"""

from __future__ import annotations

import pandas as pd

from app.config.trading import TradingProfile
from app.indicators.technical import add_all
from app.strategy.timeframes import timeframe_seconds

FEATURE_PIPELINE_VERSION = "phase2.v2"

REQUIRED_CANDLE_COLUMNS = ("time", "open", "high", "low", "close", "volume")
REQUIRED_FEATURE_COLUMNS = (
    "ema_fast",
    "ema_slow",
    "ema_mid",
    "rsi",
    "macd_line",
    "macd_signal",
    "macd_hist",
    "atr",
    "adx",
    "plus_di",
    "minus_di",
    "vwap",
    "sma_20",
    "sma_50",
    "bb_mid",
    "bb_upper",
    "bb_lower",
    "bb_width",
    "bb_position",
    "volume_sma",
    "volume_ratio",
    "volume_zscore",
    "volatility",
    "volatility_pct",
    "atr_pct",
    "regime_trend",
    "regime_volatility",
)


class FeaturePipelineError(ValueError):
    """Raised when candles cannot be transformed into causal features."""


class FeaturePipeline:
    """Deterministic OHLCV -> indicator transformation."""

    def __init__(self, ema_spans: tuple[int, int, int] = (12, 26, 50)) -> None:
        spans = tuple(int(span) for span in ema_spans)
        if len(spans) != 3 or any(span <= 0 for span in spans) or not spans[0] < spans[1] < spans[2]:
            raise ValueError("ema_spans must be three strictly increasing positive integers")
        self.ema_spans = spans
        self.version = FEATURE_PIPELINE_VERSION

    @classmethod
    def from_profile(cls, profile: TradingProfile) -> "FeaturePipeline":
        return cls(ema_spans=profile.ema_spans)

    def transform(self, candles: list[dict] | pd.DataFrame, *, timeframe: str | None = None) -> pd.DataFrame:
        """Return a new dataframe with causal indicators attached.

        Input rows must be in ascending time order with unique timestamps. The
        output never contains a value derived from a future row; indicators are
        standard causal EMA/RMA/rolling calculations.
        """
        df = candles.copy() if isinstance(candles, pd.DataFrame) else pd.DataFrame(list(candles))
        missing = [column for column in REQUIRED_CANDLE_COLUMNS if column not in df.columns]
        if missing:
            raise FeaturePipelineError(f"candles missing required columns: {', '.join(missing)}")
        if df.empty:
            raise FeaturePipelineError("cannot build features from an empty candle frame")

        out = df.copy()
        for column in ("time", "open", "high", "low", "close", "volume"):
            out[column] = pd.to_numeric(out[column], errors="coerce")
        if out[list(REQUIRED_CANDLE_COLUMNS)].isna().any().any():
            raise FeaturePipelineError("candle frame contains non-numeric or missing OHLCV values")
        if (out[["open", "high", "low", "close"]] <= 0).any().any():
            raise FeaturePipelineError("OHLC prices must be finite and positive")
        if (out["volume"] < 0).any():
            raise FeaturePipelineError("volume must be non-negative")
        if (out["high"] < out[["open", "close", "low"]].max(axis=1)).any():
            raise FeaturePipelineError("high must be the highest price in the bar")
        if (out["low"] > out[["open", "close", "high"]].min(axis=1)).any():
            raise FeaturePipelineError("low must be the lowest price in the bar")
        out["time"] = out["time"].astype("int64")
        if out["time"].duplicated().any():
            raise FeaturePipelineError("candle frame contains duplicate timestamps")
        if not out["time"].is_monotonic_increasing:
            raise FeaturePipelineError("candle frame must be ascending by time")

        out = add_all(out, ema_spans=self.ema_spans)
        if timeframe:
            interval = timeframe_seconds(timeframe)
            out["candle_close_time"] = out["time"] + interval
            out["is_closed"] = True
        out["bar_index"] = range(len(out))
        out["feature_pipeline_version"] = self.version
        out.attrs["feature_pipeline_version"] = self.version
        out.attrs["ema_spans"] = self.ema_spans
        return out

    def prefix(self, candles: list[dict] | pd.DataFrame, end_idx: int, *, timeframe: str | None = None) -> pd.DataFrame:
        """Features computed only from rows up to and including ``end_idx``."""
        df = candles.copy() if isinstance(candles, pd.DataFrame) else pd.DataFrame(list(candles))
        if end_idx < 0 or end_idx >= len(df):
            raise IndexError(f"end_idx {end_idx} outside candle frame of length {len(df)}")
        return self.transform(df.iloc[: end_idx + 1].reset_index(drop=True), timeframe=timeframe)


def ensure_features(df: pd.DataFrame, *, ema_spans: tuple[int, int, int] = (12, 26, 50)) -> pd.DataFrame:
    """Return ``df`` unchanged if features exist, otherwise transform it."""
    if all(column in df.columns for column in REQUIRED_FEATURE_COLUMNS):
        return df
    return FeaturePipeline(ema_spans=ema_spans).transform(df)


__all__ = [
    "FEATURE_PIPELINE_VERSION",
    "FeaturePipeline",
    "FeaturePipelineError",
    "REQUIRED_CANDLE_COLUMNS",
    "REQUIRED_FEATURE_COLUMNS",
    "ensure_features",
]
