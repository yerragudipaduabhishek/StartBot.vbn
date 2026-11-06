"""Backtesting engine with causal execution and complete accounting.

Execution assumptions are deliberately conservative:

* a signal produced from a closed bar is filled at the *next* bar open;
* stops/targets are evaluated against the next bar's high/low, with a gap
  through the level filled at the (worse) open;
* entry and exit fees are charged once on their respective notional;
* funding is charged at 8h boundaries while a position is open;
* the final open position is closed and charged at the last close.

The result keeps the historical ``BacktestResult`` surface and adds explicit
cost, exposure and benchmark fields so reports cannot silently omit them.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd

from app.config.trading import TradingProfile
from app.features.pipeline import FeaturePipeline
from app.risk.risk_manager import RiskManager
from app.strategy.signal_engine import SignalEngine
from app.strategy.strategy_a import STRATEGY_A_CONTRACT_VALUE
from app.strategy.timeframes import (
    bars_per_year as timeframe_bars_per_year,
    bars_per_year_for_seconds,
    infer_timeframe,
    normalize_timeframe,
)

logger = logging.getLogger(__name__)

CONTRACT_VALUE = STRATEGY_A_CONTRACT_VALUE
BARS_PER_YEAR_5M = timeframe_bars_per_year("5m")
FUNDING_INTERVAL_SECONDS = 8 * 60 * 60


@dataclass
class BacktestTrade:
    entry_time: int
    exit_time: int
    side: str
    size: float
    entry_price: float
    stop_loss: float
    target: float
    exit_price: float
    pnl: float
    reason: str
    bars_held: int
    entry_bar: int = 0
    regime: str | None = None
    entry_fee: float = 0.0
    exit_fee: float = 0.0
    funding: float = 0.0
    gross_pnl: float = 0.0
    entry_slippage: float = 0.0
    exit_slippage: float = 0.0
    requested_size: float = 0.0


@dataclass
class BacktestResult:
    initial_equity: float
    final_equity: float
    trades: list[BacktestTrade] = field(default_factory=list)
    equity_curve: list[tuple[int, float]] = field(default_factory=list)
    signals: int = 0
    fees_total: float = 0.0
    slippage_total: float = 0.0
    dataset_hash: str | None = None
    dataset_metadata: dict | None = None
    feature_pipeline_version: str | None = None
    timeframe: str | None = None
    bars_per_year: float = float(BARS_PER_YEAR_5M)
    funding_total: float = 0.0
    benchmark_start_price: float | None = None
    benchmark_end_price: float | None = None
    exposure_bars: int = 0
    total_bars: int = 0
    turnover_notional: float = 0.0
    rejected_orders: int = 0

    @property
    def return_pct(self) -> float:
        return (self.final_equity / self.initial_equity - 1.0) * 100.0 if self.initial_equity else 0.0

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        return sum(1 for t in self.trades if t.pnl > 0) / len(self.trades)

    @property
    def profit_factor(self) -> float:
        gross_win = sum(t.pnl for t in self.trades if t.pnl > 0)
        gross_loss = -sum(t.pnl for t in self.trades if t.pnl < 0)
        if gross_loss:
            return gross_win / gross_loss
        return 999.0 if gross_win > 0 else 0.0

    @property
    def avg_win(self) -> float:
        wins = [t.pnl for t in self.trades if t.pnl > 0]
        return sum(wins) / len(wins) if wins else 0.0

    @property
    def avg_loss(self) -> float:
        losses = [t.pnl for t in self.trades if t.pnl < 0]
        return sum(losses) / len(losses) if losses else 0.0

    @property
    def expectancy(self) -> float:
        return sum(t.pnl for t in self.trades) / len(self.trades) if self.trades else 0.0

    @property
    def max_drawdown_pct(self) -> float:
        peak = self.initial_equity
        worst = 0.0
        for _, eq in self.equity_curve:
            peak = max(peak, eq)
            if peak > 0:
                worst = min(worst, (eq - peak) / peak)
        return worst * 100.0

    @property
    def exposure_pct(self) -> float:
        return self.exposure_bars / self.total_bars * 100.0 if self.total_bars else 0.0

    @property
    def avg_holding_bars(self) -> float:
        return sum(t.bars_held for t in self.trades) / len(self.trades) if self.trades else 0.0

    @property
    def turnover(self) -> float:
        return self.turnover_notional / self.initial_equity if self.initial_equity else 0.0

    @property
    def benchmark_return_pct(self) -> float:
        if not self.benchmark_start_price or self.benchmark_end_price is None:
            return 0.0
        return (self.benchmark_end_price / self.benchmark_start_price - 1.0) * 100.0

    @property
    def cagr(self) -> float:
        bars = len(self.equity_curve)
        if bars <= 0 or self.initial_equity <= 0 or self.final_equity <= 0:
            return 0.0
        years = bars / float(self.bars_per_year or BARS_PER_YEAR_5M)
        if years <= 0:
            return 0.0
        return ((self.final_equity / self.initial_equity) ** (1.0 / years) - 1.0) * 100.0

    @property
    def costs_total(self) -> float:
        return self.fees_total + self.slippage_total + self.funding_total

    def _bar_returns(self) -> list[float]:
        rets: list[float] = []
        prev = self.initial_equity
        for _, eq in self.equity_curve:
            if prev > 0:
                rets.append((eq - prev) / prev)
            prev = eq
        return rets

    def sharpe(self, bars_per_year: float | None = None) -> float:
        rets = self._bar_returns()
        if len(rets) < 2:
            return 0.0
        mean = sum(rets) / len(rets)
        sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))
        if sd == 0.0:
            return 0.0
        return mean / sd * math.sqrt(float(bars_per_year or self.bars_per_year))

    def sortino(self, bars_per_year: float | None = None) -> float:
        rets = self._bar_returns()
        downs = [r for r in rets if r < 0]
        if len(rets) < 2 or not downs:
            return 0.0
        mean = sum(rets) / len(rets)
        dd = math.sqrt(sum(r * r for r in downs) / len(downs))
        if dd == 0.0:
            return 0.0
        return mean / dd * math.sqrt(float(bars_per_year or self.bars_per_year))

    def side_stats(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for side in ("long", "short"):
            ts = [t for t in self.trades if t.side == side]
            pnl = sum(t.pnl for t in ts)
            out[side] = {
                "n": len(ts),
                "pnl": round(pnl, 2),
                "win_rate": round(sum(1 for t in ts if t.pnl > 0) / len(ts), 3) if ts else 0.0,
            }
        return out

    def cost_sensitivity(self, multipliers: tuple[float, ...] = (0.0, 1.0, 2.0)) -> list[dict[str, float]]:
        """Approximate return under scaled trading costs (fees+slippage+funding)."""
        costs = self.fees_total + self.slippage_total + max(0.0, self.funding_total)
        out: list[dict[str, float]] = []
        for multiplier in multipliers:
            adjusted = self.final_equity - (float(multiplier) - 1.0) * costs
            ret = (adjusted / self.initial_equity - 1.0) * 100.0 if self.initial_equity else 0.0
            out.append({"cost_multiplier": float(multiplier), "return_pct": round(ret, 3)})
        return out

    def metrics(self) -> dict[str, float]:
        return {
            "trades": len(self.trades),
            "return_pct": round(self.return_pct, 3),
            "win_rate": round(self.win_rate, 3),
            "profit_factor": round(self.profit_factor, 3),
            "avg_win": round(self.avg_win, 2),
            "avg_loss": round(self.avg_loss, 2),
            "expectancy": round(self.expectancy, 2),
            "max_drawdown_pct": round(self.max_drawdown_pct, 3),
            "sharpe": round(self.sharpe(), 3),
            "sortino": round(self.sortino(), 3),
            "cagr": round(self.cagr, 3),
            "exposure_pct": round(self.exposure_pct, 3),
            "turnover": round(self.turnover, 4),
            "avg_holding_bars": round(self.avg_holding_bars, 2),
            "benchmark_return_pct": round(self.benchmark_return_pct, 3),
            "fees_usd": round(self.fees_total, 2),
            "slippage_usd": round(self.slippage_total, 2),
            "funding_usd": round(self.funding_total, 2),
            "costs_usd": round(self.costs_total, 2),
        }

    def summary(self) -> str:
        m = self.metrics()
        return (
            f"trades={m['trades']} return={m['return_pct']:+.2f}% win={m['win_rate']:.0%} "
            f"pf={m['profit_factor']:g} md={m['max_drawdown_pct']:.2f}% sharpe={m['sharpe']:g}"
        )


def _resolve_annualization(
    df: pd.DataFrame, timeframe: str | None
) -> tuple[str, float]:
    if timeframe:
        normalized = normalize_timeframe(timeframe)
        return normalized, float(timeframe_bars_per_year(normalized))
    if len(df) < 2:
        return "5m", float(BARS_PER_YEAR_5M)
    deltas = df["time"].astype("int64").diff().dropna()
    median = float(deltas.median()) if len(deltas) else 300.0
    label = infer_timeframe(median)
    if label is not None:
        return label, float(timeframe_bars_per_year(label))
    return f"{int(round(median))}s", bars_per_year_for_seconds(median)


def run_backtest(
    candles: list[dict] | pd.DataFrame,
    engine: SignalEngine,
    risk_manager: RiskManager,
    initial_equity: float = 10_000.0,
    warmup_bars: int = 60,
    commission_pct: float = 0.05,
    slippage_bps: float = 0.0,
    ema_spans: tuple[int, int, int] = (12, 26, 50),
    regime_col: str = "regime",
    profile: TradingProfile | None = None,
    dataset_metadata: dict | None = None,
    *,
    timeframe: str | None = None,
    funding_rate_bps_8h: float = 0.0,
    max_participation_pct: float = 1.0,
    rejection_rate: float = 0.0,
    seed: int = 42,
    max_holding_bars: int | None = None,
) -> BacktestResult:
    """Replay candles with next-bar execution and full cost accounting.

    A supplied profile overrides EMA/fee/slippage/contract defaults. Replay is
    research-only: a risk manager bound to persistence is replaced by a
    non-persisting copy before the loop starts.
    """
    if initial_equity <= 0:
        raise ValueError("initial_equity must be positive")
    if commission_pct < 0 or slippage_bps < 0 or funding_rate_bps_8h < 0:
        raise ValueError("cost assumptions must be non-negative")
    if not 0.0 < max_participation_pct <= 1.0:
        raise ValueError("max_participation_pct must be in (0, 1]")
    if not 0.0 <= rejection_rate <= 1.0:
        raise ValueError("rejection_rate must be in [0, 1]")
    if max_holding_bars is not None and max_holding_bars <= 0:
        raise ValueError("max_holding_bars must be positive")

    if profile is not None:
        ema_spans = profile.ema_spans
        commission_pct = profile.paper_fee_pct
        slippage_bps = profile.paper_slippage_pct * 100.0
        contract_value = profile.contract_value
    else:
        contract_value = CONTRACT_VALUE
    if isinstance(risk_manager, RiskManager) and risk_manager.session_factory is not None:
        research_risk = risk_manager.research_copy()
        if getattr(engine, "risk_manager", None) is risk_manager:
            engine.risk_manager = research_risk
        risk_manager = research_risk

    df = pd.DataFrame(candles) if not isinstance(candles, pd.DataFrame) else candles.copy()
    if len(df) < 2:
        raise ValueError("backtest requires at least two candles")
    pipeline = FeaturePipeline(ema_spans=ema_spans)
    df = pipeline.transform(df)
    resolved_timeframe, annual_bars = _resolve_annualization(df, timeframe)

    slip = slippage_bps / 10_000.0
    fee_rate = commission_pct / 100.0
    funding_rate = funding_rate_bps_8h / 10_000.0

    import random

    rng = random.Random(seed)
    result = BacktestResult(
        initial_equity=float(initial_equity),
        final_equity=float(initial_equity),
        timeframe=resolved_timeframe,
        bars_per_year=annual_bars,
        benchmark_start_price=float(df["open"].iloc[min(warmup_bars, len(df) - 1)]),
        benchmark_end_price=float(df["close"].iloc[-1]),
    )
    result.feature_pipeline_version = pipeline.version
    if dataset_metadata is not None:
        result.dataset_metadata = dict(dataset_metadata)
        result.dataset_hash = dataset_metadata.get("dataset_hash")

    cash = float(initial_equity)
    equity = float(initial_equity)
    position: BacktestTrade | None = None
    pending: dict | None = None
    last_funding_boundary: int | None = None
    start = max(0, min(int(warmup_bars), len(df) - 1))

    def unrealized(price: float) -> float:
        if position is None:
            return 0.0
        direction = 1.0 if position.side == "long" else -1.0
        return (price - position.entry_price) * position.size * contract_value * direction

    def close_position(bar_index: int, bar_time: int, reference: float, reason: str) -> None:
        nonlocal cash, equity, position
        assert position is not None
        if position.side == "long":
            exit_price = reference * (1.0 - slip)
            gross = (exit_price - position.entry_price) * position.size * contract_value
        else:
            exit_price = reference * (1.0 + slip)
            gross = (position.entry_price - exit_price) * position.size * contract_value
        exit_fee = fee_rate * position.size * exit_price * contract_value
        exit_slippage = abs(exit_price - reference) * position.size * contract_value
        pnl = gross - position.entry_fee - exit_fee - position.funding
        cash += gross - exit_fee
        position.exit_price = exit_price
        position.exit_time = bar_time
        position.exit_fee = exit_fee
        position.exit_slippage = exit_slippage
        position.gross_pnl = gross
        position.pnl = pnl
        position.reason = reason
        position.bars_held = bar_index - position.entry_bar
        result.fees_total += exit_fee
        result.slippage_total += exit_slippage
        result.turnover_notional += abs(position.size * exit_price * contract_value)
        result.trades.append(position)
        if isinstance(risk_manager, RiskManager):
            risk_manager.record_trade(
                pnl,
                bar_index=bar_index,
                now=datetime.fromtimestamp(bar_time, tz=timezone.utc),
            )
        position = None
        equity = cash

    for i in range(start, len(df)):
        row = df.iloc[i]
        bar_time = int(row["time"])
        bar_now = datetime.fromtimestamp(bar_time, tz=timezone.utc)
        open_price = float(row["open"])
        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])
        regime = str(row[regime_col]) if regime_col in df.columns else None
        exposure_this_bar = position is not None

        # 1) A signal from the previous close is filled at this bar's open.
        if pending is not None and position is None:
            order = pending
            pending = None
            side = order["side"]
            requested = float(order["size"])
            max_size = requested
            if max_participation_pct < 1.0:
                max_size = min(requested, float(row["volume"]) * max_participation_pct)
            if max_size <= 0 or rng.random() < rejection_rate:
                result.rejected_orders += 1
            else:
                stop = float(order["stop_loss"])
                target = float(order["target"])
                valid_gap = (side == "long" and stop < open_price) or (side == "short" and stop > open_price)
                if not valid_gap:
                    result.rejected_orders += 1
                else:
                    entry_price = open_price * (1.0 + slip) if side == "long" else open_price * (1.0 - slip)
                    entry_fee = fee_rate * max_size * entry_price * contract_value
                    entry_slippage = abs(entry_price - open_price) * max_size * contract_value
                    cash -= entry_fee
                    position = BacktestTrade(
                        entry_time=bar_time,
                        exit_time=0,
                        side=side,
                        size=max_size,
                        entry_price=entry_price,
                        stop_loss=stop,
                        target=target,
                        exit_price=0.0,
                        pnl=0.0,
                        reason="",
                        bars_held=0,
                        entry_bar=i,
                        regime=regime,
                        entry_fee=entry_fee,
                        entry_slippage=entry_slippage,
                        requested_size=requested,
                    )
                    result.fees_total += entry_fee
                    result.slippage_total += entry_slippage
                    result.turnover_notional += max_size * entry_price * contract_value
                    last_funding_boundary = (bar_time // FUNDING_INTERVAL_SECONDS) * FUNDING_INTERVAL_SECONDS
                    exposure_this_bar = True
                    if isinstance(risk_manager, RiskManager):
                        risk_manager.on_position_opened(bar_index=i, now=bar_now)

        # 2) Manage the open position on this bar (worst case first).
        if position is not None:
            reference: float | None = None
            exit_reason = ""
            if position.side == "long":
                if open_price <= position.stop_loss:
                    reference, exit_reason = open_price, "stop"
                elif open_price >= position.target:
                    reference, exit_reason = open_price, "target"
                elif low <= position.stop_loss:
                    reference, exit_reason = position.stop_loss, "stop"
                elif high >= position.target:
                    reference, exit_reason = position.target, "target"
            else:
                if open_price >= position.stop_loss:
                    reference, exit_reason = open_price, "stop"
                elif open_price <= position.target:
                    reference, exit_reason = open_price, "target"
                elif high >= position.stop_loss:
                    reference, exit_reason = position.stop_loss, "stop"
                elif low <= position.target:
                    reference, exit_reason = position.target, "target"
            if reference is None and max_holding_bars is not None and i - position.entry_bar >= max_holding_bars:
                reference, exit_reason = close, "bar_close"
            if reference is not None:
                close_position(i, bar_time, reference, exit_reason)

        # 3) Funding at 8h boundaries for any still-open position (paid at close).
        if position is not None and funding_rate:
            boundary = (bar_time // FUNDING_INTERVAL_SECONDS) * FUNDING_INTERVAL_SECONDS
            if last_funding_boundary is None:
                last_funding_boundary = boundary
            periods = max(0, (boundary - last_funding_boundary) // FUNDING_INTERVAL_SECONDS)
            if periods:
                notional = position.size * close * contract_value
                funding_cost = notional * funding_rate * periods
                if position.side == "short":
                    funding_cost = -funding_cost
                cash -= funding_cost
                position.funding += funding_cost
                result.funding_total += funding_cost
                last_funding_boundary = boundary

        # 4) Final bar: no bar remains to fill a pending signal; close open risk.
        if i == len(df) - 1 and position is not None:
            close_position(i, bar_time, close, "end_of_data")

        equity = cash + unrealized(close)
        if exposure_this_bar:
            result.exposure_bars += 1
        result.total_bars += 1
        result.equity_curve.append((bar_time, equity))

        # 5) Generate a signal at this close, executable only on the next open.
        sig = engine.analyze_candle(df, end_idx=i, equity=equity, now=bar_now)
        result.signals += 1
        if (
            position is None
            and pending is None
            and sig["direction"] in ("long", "short")
            and sig.get("risk_allowed")
        ):
            size = sig.get("accepted_size")
            if size and size > 0:
                pending = {
                    "side": sig["direction"],
                    "size": float(size),
                    "stop_loss": float(sig["stop_loss"]),
                    "target": float(sig["target"]),
                }

    result.final_equity = equity
    return result
