import pandas as pd

from app.data.resample import _resample


def _candle_df():
    t0 = 1700000000 - (1700000000 % 3600)  # round down to a 1h boundary
    return pd.DataFrame(
        {
            "time": [t0 + i * 300 for i in range(24)],  # 2 hours of 5m candles
            "open": [100.0 + i for i in range(24)],
            "high": [101.0 + i for i in range(24)],
            "low": [99.0 + i for i in range(24)],
            "close": [100.5 + i for i in range(24)],
            "volume": [1.0] * 24,
        }
    )


def test_resample_to_1h_aggregates_ohlcv():
    out = _resample(_candle_df(), 60)
    assert len(out) == 2  # 2 one-hour bars
    assert out["open"].iloc[0] == 100.0  # first open of first hour
    assert out["high"].iloc[0] == 112.0  # max high of first 12 candles
    assert out["low"].iloc[0] == 99.0
    assert out["close"].iloc[0] == 111.5  # last close of first 12 candles
    assert out["volume"].iloc[0] == 12.0
    assert out["time"].iloc[0] == 1699999200  # first hour boundary


def test_resample_15m_returns_8_bars():
    out = _resample(_candle_df(), 15)
    assert len(out) == 8
    assert out["high"].iloc[0] == 103.0  # max of first three 5m highs