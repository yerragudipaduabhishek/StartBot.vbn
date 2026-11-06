import numpy as np

from app.backtest.engine import run_backtest
from app.risk.config import RiskSettings
from app.risk.risk_manager import RiskManager
from app.strategy.signal_engine import SignalEngine


def _trend_day(direction: str, n: int = 300) -> list[dict]:
    rng = np.random.default_rng(3)
    base = 60000 if direction == "up" else 52000
    step = 40 if direction == "up" else -40
    close = base + np.arange(n) * step + rng.normal(0, 60, n)
    return [
        {
            "time": 1700000000 + i * 300,
            "open": close[i],
            "high": close[i] + abs(rng.normal(0, 80)),
            "low": close[i] - abs(rng.normal(0, 80)),
            "close": close[i],
            "volume": 10.0 + abs(rng.normal(0, 3)),
        }
        for i in range(n)
    ]


def _engine():
    settings = RiskSettings(min_confidence=0.0)
    rm = RiskManager(starting_equity=10000, settings=settings)
    return SignalEngine(risk_manager=rm, contract_value=0.001), rm


def test_backtest_runs_on_trend_day():
    candles = _trend_day("up")
    engine, rm = _engine()
    result = run_backtest(candles, engine, rm, initial_equity=10000, warmup_bars=60)
    assert len(result.trades) > 0
    assert result.signals > 0
    assert result.max_drawdown_pct <= 0.0  # worst DD is negative or zero
    assert len(result.equity_curve) > 0


def test_backtest_trades_have_pnl():
    candles = _trend_day("down")
    engine, rm = _engine()
    result = run_backtest(candles, engine, rm, initial_equity=10000, warmup_bars=60)
    assert all(t.bars_held >= 0 for t in result.trades)
    assert all(t.reason in ("stop", "target", "bar_close", "end_of_data") for t in result.trades)
    assert result.win_rate >= 0.0