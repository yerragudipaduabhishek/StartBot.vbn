import numpy as np
import pandas as pd
import pytest

from app.indicators.technical import add_all, adx, atr, ema, macd, rsi, rolling_vwap


@pytest.fixture
def ohlcv() -> pd.DataFrame:
    rng = np.random.default_rng(42)
    n = 200
    close = 60000 + np.cumsum(rng.normal(0, 200, n))
    df = pd.DataFrame(
        {
            "time": np.arange(n),
            "open": close,
            "high": close + rng.uniform(0, 300, n),
            "low": close - rng.uniform(0, 300, n),
            "close": close,
            "volume": rng.uniform(1, 50, n),
        }
    )
    return df


def test_rsi_bounds(ohlcv):
    out = rsi(ohlcv["close"])
    assert out.dropna().between(0, 100).all()


def test_ema_monotonic_follow():
    close = pd.Series(np.arange(1, 101, dtype=float))
    e = ema(close, 10)
    assert (e.diff().dropna() > 0).all()


def test_macd_columns(ohlcv):
    m = macd(ohlcv["close"])
    assert list(m.columns) == ["macd_line", "macd_signal", "macd_hist"]
    assert m["macd_line"].notna().sum() > 0


def test_atr_positive(ohlcv):
    a = atr(ohlcv["high"], ohlcv["low"], ohlcv["close"])
    assert (a.dropna() > 0).all()


def test_adx_range(ohlcv):
    a, pdi, mdi = adx(ohlcv["high"], ohlcv["low"], ohlcv["close"])
    assert a.dropna().between(0, 100).all()
    assert pdi.dropna().between(0, 100).all()


def test_vwap_between_hl(ohlcv):
    v = rolling_vwap(ohlcv)
    assert (v.dropna() >= ohlcv["low"].rolling(14, min_periods=1).min() - 1e-6).all()
    assert (v.dropna() <= ohlcv["high"].rolling(14, min_periods=1).max() + 1e-6).all()


def test_add_all_columns(ohlcv):
    out = add_all(ohlcv)
    expected = {"ema_fast", "ema_slow", "ema_mid", "rsi", "macd_line", "macd_signal", "macd_hist", "atr", "adx", "plus_di", "minus_di", "vwap"}
    assert expected.issubset(out.columns)


def test_strategy_a_ema_spans_are_exact(ohlcv):
    """The soak runner's 8/40/50 profile must reach the indicator layer intact."""
    spans = (8, 40, 50)
    out = add_all(ohlcv, ema_spans=spans)
    for column, span in zip(("ema_fast", "ema_slow", "ema_mid"), spans):
        expected = ohlcv["close"].ewm(span=span, adjust=False).mean()
        pd.testing.assert_series_equal(out[column], expected, check_names=False)
