from __future__ import annotations

from datetime import date, datetime, timezone

import pandas as pd

from app.agents.base import Vote
from app.backtest.engine import run_backtest
from app.risk.config import RiskSettings
from app.risk.risk_manager import RiskManager
from app.strategy.signal_engine import SignalEngine


def _hourly(days: int = 5) -> list[dict]:
    start = int(datetime(2023, 1, 1, tzinfo=timezone.utc).timestamp())
    rows = []
    for i in range(days * 24):
        rows.append(
            {
                "time": start + i * 3600,
                "open": 100.0,
                "high": 100.8,
                "low": 99.2,
                "close": 100.0,
                "volume": 1_000.0,
            }
        )
    return rows


class _ReplayEngine:
    def __init__(self, risk_manager: RiskManager) -> None:
        self.risk_manager = risk_manager

    def analyze_candle(self, df, ticker=None, headlines=None, end_idx=None, equity=None, now=None):
        idx = len(df) - 1 if end_idx is None else int(end_idx)
        row = df.iloc[idx]
        candle_time = int(row["time"])
        close = float(row["close"])
        signal = {
            "direction": "long",
            "entry": close,
            "stop_loss": close - 0.5,
            "target": close + 0.5,
            "confidence": 1.0,
            "candle_time": candle_time,
            "timeframe": "1h",
            "data_quality": {"status": "streaming", "price_valid": True},
        }
        allowed, size, _ = self.risk_manager.evaluate(
            signal,
            equity=float(equity if equity is not None else 10_000.0),
            contract_value=0.001,
            now=now,
            timeframe="1h",
        )
        return {
            "direction": "long",
            "confidence": 1.0,
            "entry": close,
            "stop_loss": close - 0.5,
            "target": close + 0.5,
            "accepted_size": size.quantities if allowed and size else None,
            "risk_allowed": bool(allowed and size),
        }


class _CaptureRisk:
    def __init__(self) -> None:
        self.settings = RiskSettings()
        self.signal: dict | None = None
        self.now: datetime | None = None

    def evaluate(self, signal, equity, contract_value=0.001, *, now=None):
        self.signal = dict(signal)
        self.now = now
        return False, None, "capture only"


class _ForcedLong:
    def decide(self, votes, threshold=0.25) -> Vote:
        return Vote(
            agent="decision",
            direction="long",
            score=1.0,
            confidence=1.0,
            explanation="forced",
            details={},
        )


def test_daily_risk_resets_on_simulated_bar_time() -> None:
    settings = RiskSettings().model_copy(
        update={
            "max_daily_trades": 2,
            "max_daily_loss_pct": 100.0,
            "max_drawdown_pct": 100.0,
            "max_positions": 1,
            "cooldown_bars": 0,
            "min_confidence": 0.0,
            "require_streaming_data": False,
            "min_equity": 0.0,
            "risk_per_trade_pct": 0.1,
        }
    )
    risk = RiskManager(settings=settings, starting_equity=10_000.0)
    result = run_backtest(
        _hourly(5),
        _ReplayEngine(risk),
        risk,
        initial_equity=10_000.0,
        warmup_bars=1,
        commission_pct=0.0,
        slippage_bps=0.0,
    )
    assert len(result.trades) == 10, "two entries per UTC day for five days"
    per_day: dict[date, int] = {}
    for trade in result.trades:
        day = datetime.fromtimestamp(trade.entry_time, tz=timezone.utc).date()
        per_day[day] = per_day.get(day, 0) + 1
    assert per_day == {date(2023, 1, day): 2 for day in range(1, 6)}


def test_daily_reset_runs_inside_risk_checks() -> None:
    settings = RiskSettings().model_copy(
        update={
            "max_daily_trades": 10,
            "max_daily_loss_pct": 5.0,
            "max_drawdown_pct": 100.0,
            "cooldown_bars": 0,
            "min_confidence": 0.0,
            "require_streaming_data": False,
            "min_equity": 0.0,
        }
    )
    risk = RiskManager(settings=settings, starting_equity=10_000.0)
    risk.day = date(2023, 1, 1)
    risk.day_pnl = -1_000.0
    risk.trades_today = 10
    allowed, _, reason = risk.evaluate(
        {
            "direction": "long",
            "entry": 100.0,
            "stop_loss": 99.0,
            "target": 102.0,
            "confidence": 1.0,
        },
        equity=10_000.0,
        now=datetime(2023, 1, 2, 0, 5, tzinfo=timezone.utc),
    )
    assert allowed, reason
    assert risk.day == date(2023, 1, 2)
    assert risk.day_pnl == 0.0
    assert risk.trades_today == 0


def test_signal_engine_passes_candle_time_and_timeframe_to_risk() -> None:
    rows = []
    start = int(datetime(2023, 1, 1, tzinfo=timezone.utc).timestamp())
    for i in range(180):
        rows.append(
            {
                "time": start + i * 3600,
                "open": 100.0 + i * 0.1,
                "high": 101.0 + i * 0.1,
                "low": 99.0 + i * 0.1,
                "close": 100.5 + i * 0.1,
                "volume": 100.0,
            }
        )
    df = pd.DataFrame(rows)
    risk = _CaptureRisk()
    engine = SignalEngine(risk_manager=risk, decision_agent=_ForcedLong())
    now = datetime.fromtimestamp(int(df["time"].iloc[-1]), tz=timezone.utc)
    engine.analyze_candle(df, equity=10_000.0, now=now)
    assert risk.signal is not None
    assert risk.signal["candle_time"] == int(df["time"].iloc[-1])
    assert risk.signal["timeframe"] == "1h"
    assert risk.now == now
