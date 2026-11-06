"""Tests for the validation stack: engine metrics, regimes, study split, deep backfill."""

import math

import numpy as np
import pandas as pd

from app.backtest.engine import run_backtest
from app.backtest.regimes import regime_summary, tag_regimes
from app.indicators.technical import add_all
from app.risk.config import RiskSettings
from app.risk.risk_manager import RiskManager
from app.strategy.signal_engine import SignalEngine


def _trend_data(n: int = 700, direction: str = "up", seed: int = 5) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    base = 60000 if direction == "up" else 52000
    step = 40 if direction == "up" else -40
    close = base + np.arange(n) * step
    close = close + rng.normal(0, 60, n)
    df = pd.DataFrame(
        {
            "time": 1700000000 + np.arange(n) * 300,
            "open": close,
            "high": close + np.abs(rng.normal(0, 80, n)),
            "low": close - np.abs(rng.normal(0, 80, n)),
            "close": close,
            "volume": 10.0 + np.abs(rng.normal(0, 3, n)),
        }
    )
    return add_all(df)


def _engine():
    settings = RiskSettings(min_confidence=0.0)
    rm = RiskManager(starting_equity=10000, settings=settings)
    return SignalEngine(risk_manager=rm, contract_value=0.001), rm


def test_metrics_report_complete():
    engine, rm = _engine()
    res = run_backtest(_trend_data(700), engine, rm, warmup_bars=60, slippage_bps=0.5)
    m = res.metrics()
    for key in ("trades", "return_pct", "win_rate", "profit_factor", "avg_win", "avg_loss",
                "max_drawdown_pct", "sharpe", "sortino", "fees_usd", "slippage_usd"):
        assert key in m
        assert math.isfinite(m[key])
    assert m["trades"] > 0
    assert res.slippage_total > 0  # slippage cost recorded


def test_fees_and_slippage_reduce_pnl():
    engine, rm = _engine()
    r0 = run_backtest(_trend_data(700), engine, rm, warmup_bars=60, commission_pct=0.0, slippage_bps=0.0)
    engine2, rm2 = _engine()
    r2 = run_backtest(_trend_data(700), engine2, rm2, warmup_bars=60, commission_pct=0.05, slippage_bps=0.0)
    assert r2.final_equity <= r0.final_equity
    assert r0.fees_total == 0.0
    assert r2.fees_total > 0.0


def test_slippage_degrades_exits():
    engine, rm = _engine()
    r0 = run_backtest(_trend_data(700), engine, rm, warmup_bars=60, commission_pct=0.0, slippage_bps=0.0)
    engine2, rm2 = _engine()
    r2 = run_backtest(_trend_data(700), engine2, rm2, warmup_bars=60, commission_pct=0.0, slippage_bps=2.0)
    assert r2.final_equity <= r0.final_equity
    assert r2.slippage_total > 0.0


def test_side_stats_both_sides_present():
    engine, rm = _engine()
    res = run_backtest(_trend_data(700, "down"), engine, rm, warmup_bars=60)
    stats = res.side_stats()
    assert set(stats) == {"long", "short"}
    assert stats["short"]["pnl"] >= stats["long"]["pnl"]  # short trend favours shorts


def test_analyze_parity_slice_vs_end_idx():
    """end_idx path must match the old slice-based path bar for bar."""
    df = _trend_data(400)
    engine, rm = _engine()
    for i in (150, 200, 350):
        a = engine.analyze_candle(df.iloc[: i + 1])
        b = engine.analyze_candle(df, end_idx=i)
        assert a["direction"] == b["direction"]
        assert a["score"] == b["score"]
        assert a["confidence"] == b["confidence"]


def test_tag_regimes_adds_nonempty_column():
    df = _trend_data(600)
    out = tag_regimes(df)
    assert "regime" in out.columns
    assert out["regime"].nunique() >= 1
    assert any("trend_up" in r for r in set(out["regime"]))  # clean up-trend data


def test_regime_summary_aggregates():
    from app.backtest.engine import BacktestTrade

    t1 = BacktestTrade(0, 1, "long", 1, 100, 90, 110, 110, 10.0, "target", 1, regime="trend_up|low_vol")
    t2 = BacktestTrade(0, 1, "short", 1, 100, 110, 90, 90, -5.0, "stop", 1, regime="trend_up|low_vol")
    t3 = BacktestTrade(0, 1, "long", 1, 100, 90, 110, 95, -5.0, "stop", 1, regime="range|high_vol")
    s = regime_summary([t1, t2, t3])
    assert s["trend_up|low_vol"]["n"] == 2
    assert s["trend_up|low_vol"]["pnl"] == 5.0
    assert s["range|high_vol"]["n"] == 1


def test_study_baseline_engine_construction():
    from app.backtest.study import BASELINE, build_engine, evaluate

    df = _trend_data(700)
    df = tag_regimes(df)
    engine, rm = build_engine(BASELINE)
    assert engine.decision_threshold == BASELINE["threshold"]
    res = evaluate(df.iloc[:400].reset_index(drop=True), BASELINE)
    assert isinstance(res.return_pct, float)


def test_backfill_deep_paginates_and_dedupes():
    import time

    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session

    from app.data.historical import backfill_deep
    from app.database.database import Base
    from app.database.models import Candle

    engine_db = create_engine("sqlite://")  # isolated in-memory db for this test
    Base.metadata.create_all(engine_db)


    now = int(time.time())
    page_times = {
        0: list(range(now, now - 5000 * 300, -300)),
        1: list(range(now - 5000 * 300, now - 10000 * 300, -300)),
    }
    state = {"call": 0}

    class Stub:
        def get_candles(self, symbol, resolution, start, end):
            idx = state["call"]
            state["call"] += 1
            if idx not in page_times:
                return []
            return [
                {"time": t, "open": 100., "high": 101., "low": 99., "close": 100., "volume": 1.}
                for t in page_times[idx]
            ][:4000]  # API caps the response at 4000 rows per call

    def sweep():
        with Session(engine_db) as s:
            res = backfill_deep("BTCUSDT", "5m", days=45, chunk_days=14, client=Stub(), session=s)
            s.commit()
        return res

    res1 = sweep()
    assert res1["fetched"] == 8000      # two populated pages, 4000 each
    assert res1["chunks"] >= 3          # walked two populated pages then hit empty

    res2 = sweep()
    assert res2["fetched"] == 0         # idempotent: nothing new

    with Session(engine_db) as s:
        n = len(s.execute(select(Candle.time)).scalars().all())
    assert n == 8000                    # no duplicates accumulated