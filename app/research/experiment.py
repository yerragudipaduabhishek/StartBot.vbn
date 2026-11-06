"""Reproducible research experiment runner."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import pandas as pd

from app.backtest.engine import BacktestResult, run_backtest
from app.config.trading import TradingProfile
from app.data.dataset import DatasetMetadata, build_dataset
from app.features.pipeline import FEATURE_PIPELINE_VERSION
from app.research.models import ResearchExperiment
from app.research.store import ResearchStore
from app.risk.risk_manager import RiskManager
from app.strategy.signal_engine import SignalEngine


@dataclass
class ExperimentResult:
    result: BacktestResult
    dataset: DatasetMetadata
    experiment: ResearchExperiment


def run_experiment(
    candles: list[dict] | pd.DataFrame,
    engine: SignalEngine,
    risk_manager: RiskManager,
    *,
    profile: TradingProfile,
    params: dict[str, Any],
    assumptions: dict[str, Any] | None = None,
    store: ResearchStore | None = None,
    initial_equity: float = 10_000.0,
    warmup_bars: int = 60,
    commission_pct: float | None = None,
    slippage_bps: float | None = None,
    dataset_source: str = "experiment-input",
    closed_only: bool = True,
) -> ExperimentResult:
    """Run a backtest and persist its dataset hash, config and metrics.

    The dataset is recorded before the experiment, and validation refuses an
    experiment whose dataset hash is not present in research storage.
    """
    store = store or ResearchStore()
    fee = profile.paper_fee_pct if commission_pct is None else float(commission_pct)
    slip = profile.paper_slippage_pct * 100.0 if slippage_bps is None else float(slippage_bps)
    df, metadata = build_dataset(
        candles,
        symbol=profile.symbol,
        timeframe=profile.timeframe,
        source=dataset_source,
        venue="research-input",
        transport="memory",
        closed_only=closed_only,
        strict=False,
        allow_gaps=True,
    )
    store.record_dataset(metadata)
    result = run_backtest(
        df,
        engine,
        risk_manager,
        initial_equity=initial_equity,
        warmup_bars=warmup_bars,
        commission_pct=fee,
        slippage_bps=slip,
        ema_spans=profile.ema_spans,
        profile=profile,
        dataset_metadata=metadata.as_dict(),
    )
    experiment = store.record_experiment(
        strategy_id=profile.strategy_id,
        strategy_version=profile.strategy_version,
        symbol=profile.symbol,
        timeframe=profile.timeframe,
        dataset_hash=metadata.dataset_hash,
        dataset_start=metadata.first_candle_time,
        dataset_end=metadata.last_candle_time,
        params=params,
        metrics=result.metrics(),
        assumptions={
            "commission_pct": fee,
            "slippage_bps": slip,
            "initial_equity": initial_equity,
            "warmup_bars": warmup_bars,
            **(assumptions or {}),
        },
        profile=profile.as_dict(),
        fee_pct=fee,
        slippage_bps=slip,
        feature_pipeline_version=FEATURE_PIPELINE_VERSION,
        code_version=os.environ.get("GIT_COMMIT", ""),
    )
    return ExperimentResult(result=result, dataset=metadata, experiment=experiment)


__all__ = ["ExperimentResult", "run_experiment"]
