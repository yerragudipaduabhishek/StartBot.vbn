import numpy as np
import pandas as pd


def rma(series: pd.Series, length: int) -> pd.Series:
    """Wilder's moving average (RMA)."""
    return series.ewm(alpha=1.0 / length, adjust=False).mean()


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def rsi(close: pd.Series, length: int = 14) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0)
    down = -1 * delta.clip(upper=0)
    avg_up = rma(up, length)
    avg_down = rma(down, length)
    rs = avg_up / avg_down.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    ema_fast = ema(close, fast)
    ema_slow = ema(close, slow)
    line = ema_fast - ema_slow
    sig = line.ewm(span=signal, adjust=False).mean()
    return pd.DataFrame({"macd_line": line, "macd_signal": sig, "macd_hist": line - sig})


def atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    high_low = high - low
    high_close_prev = (high - close.shift(1)).abs()
    low_close_prev = (low - close.shift(1)).abs()
    tr = pd.concat([high_low, high_close_prev, low_close_prev], axis=1).max(axis=1)
    return rma(tr, length)


def adx(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> tuple[pd.Series, pd.Series, pd.Series]:
    up_move = high - high.shift(1)
    down_move = low.shift(1) - low
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    high_low = high - low
    high_close_prev = (high - close.shift(1)).abs()
    low_close_prev = (low - close.shift(1)).abs()
    tr = pd.concat([high_low, high_close_prev, low_close_prev], axis=1).max(axis=1)

    tr_rma = rma(tr, length)
    plus_rma = rma(pd.Series(plus_dm, index=high.index), length)
    minus_rma = rma(pd.Series(minus_dm, index=high.index), length)

    plus_di = 100 * (plus_rma / tr_rma.replace(0, np.nan))
    minus_di = 100 * (minus_rma / tr_rma.replace(0, np.nan))
    dx = 100 * (np.abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, np.nan))
    return (rma(dx.fillna(0), length).fillna(0), plus_di.fillna(0), minus_di.fillna(0))


def rolling_vwap(df: pd.DataFrame, window: int = 14) -> pd.Series:
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    vol = df["volume"].fillna(0)
    denom = vol.rolling(window=window, min_periods=1).sum().replace(0, np.nan)
    return ((typical * vol).rolling(window=window, min_periods=1).sum() / denom).fillna(df["close"])


def sma(series: pd.Series, length: int = 20) -> pd.Series:
    return series.rolling(window=length, min_periods=1).mean()


def bollinger(close: pd.Series, length: int = 20, num_std: float = 2.0) -> pd.DataFrame:
    """Causal Bollinger bands and position within the band."""
    mid = sma(close, length)
    deviation = close.rolling(window=length, min_periods=1).std(ddof=0).fillna(0.0)
    upper = mid + num_std * deviation
    lower = mid - num_std * deviation
    width = (upper - lower) / mid.replace(0, np.nan)
    position = (close - lower) / (upper - lower).replace(0, np.nan)
    return pd.DataFrame(
        {
            "bb_mid": mid,
            "bb_upper": upper,
            "bb_lower": lower,
            "bb_width": width.fillna(0.0),
            "bb_position": position.fillna(0.5),
        }
    )


def volume_features(df: pd.DataFrame, length: int = 20) -> pd.DataFrame:
    volume = df["volume"].astype(float)
    average = volume.rolling(window=length, min_periods=1).mean()
    deviation = volume.rolling(window=length, min_periods=1).std(ddof=0).replace(0, np.nan)
    return pd.DataFrame(
        {
            "volume_sma": average,
            "volume_ratio": (volume / average.replace(0, np.nan)).fillna(1.0),
            "volume_zscore": ((volume - average) / deviation).fillna(0.0),
        }
    )


def volatility_features(df: pd.DataFrame, length: int = 20) -> pd.DataFrame:
    close = df["close"].astype(float)
    returns = np.log(close / close.shift(1))
    realized = returns.rolling(window=length, min_periods=2).std(ddof=0).fillna(0.0)
    atr_pct = (atr(df["high"], df["low"], close) / close.replace(0, np.nan)).fillna(0.0)
    return pd.DataFrame(
        {
            "volatility": realized,
            "volatility_pct": realized * 100.0,
            "atr_pct": atr_pct,
        }
    )


def regime_features(df: pd.DataFrame, adx_threshold: float = 25.0, vol_lookback: int = 96) -> pd.DataFrame:
    """Point-in-time regime labels derived only from current/past rows."""
    trend = np.where(
        (df["adx"] >= adx_threshold) & (df["ema_fast"] > df["ema_slow"]),
        1,
        np.where((df["adx"] >= adx_threshold) & (df["ema_fast"] < df["ema_slow"]), -1, 0),
    )
    volatility = df["volatility"]
    baseline = volatility.rolling(window=vol_lookback, min_periods=5).median()
    high = (volatility > baseline).fillna(False)
    return pd.DataFrame(
        {
            "regime_trend": pd.Series(trend, index=df.index).astype(int),
            "regime_volatility": high.astype(int),
        }
    )


def add_all(df: pd.DataFrame, ema_spans: tuple[int, int, int] = (12, 26, 50)) -> pd.DataFrame:
    """Attach common technical indicators to an OHLCV dataframe (mutates copy).

    Expects columns: open, high, low, close, volume.
    `ema_spans` configures the fast/slow/mid EMA trio consumed by the trend agent.
    """
    out = df.copy()
    fast, slow, mid = ema_spans
    out["ema_fast"] = ema(out["close"], fast)
    out["ema_slow"] = ema(out["close"], slow)
    out["ema_mid"] = ema(out["close"], mid)
    out["rsi"] = rsi(out["close"])
    m = macd(out["close"])
    out["macd_line"] = m["macd_line"]
    out["macd_signal"] = m["macd_signal"]
    out["macd_hist"] = m["macd_hist"]
    out["atr"] = atr(out["high"], out["low"], out["close"])
    out["adx"], out["plus_di"], out["minus_di"] = adx(out["high"], out["low"], out["close"])
    out["vwap"] = rolling_vwap(out)
    out["sma_20"] = sma(out["close"], 20)
    out["sma_50"] = sma(out["close"], 50)
    for frame in (bollinger(out["close"]), volume_features(out), volatility_features(out)):
        for column in frame.columns:
            out[column] = frame[column]
    regimes = regime_features(out)
    for column in regimes.columns:
        out[column] = regimes[column]
    return out