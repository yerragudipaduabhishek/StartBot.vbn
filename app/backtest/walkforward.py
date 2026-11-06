"""Rolling/expanding walk-forward validation.

Each fold optimizes parameters using only its training window, then evaluates
the selected parameters on an untouched, strictly later test window. The test
window is never shown to the optimizer, so parameter selection cannot leak
future information. Fold boundaries, trials, metrics, and a reproducibility
hash are retained in the result and optionally written as artifacts.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from app.backtest.optimize import OptimizationResult, Trial, optimize


class WalkForwardError(ValueError):
    """The fold specification or input data is invalid."""


@dataclass(frozen=True)
class FoldSpan:
    index: int
    train_start: int
    train_end: int
    test_start: int
    test_end: int
    warmup_start: int
    embargo: int = 0

    @property
    def train_bars(self) -> int:
        return self.train_end - self.train_start

    @property
    def test_bars(self) -> int:
        return self.test_end - self.test_start

    def as_dict(self) -> dict[str, int]:
        return {
            "fold": self.index,
            "train_start": self.train_start,
            "train_end": self.train_end,
            "test_start": self.test_start,
            "test_end": self.test_end,
            "warmup_start": self.warmup_start,
            "train_bars": self.train_bars,
            "test_bars": self.test_bars,
            "embargo": self.embargo,
        }


def generate_folds(
    n_bars: int,
    *,
    train_bars: int,
    test_bars: int,
    step: int | None = None,
    mode: str = "rolling",
    embargo: int = 0,
    warmup: int = 0,
) -> list[FoldSpan]:
    """Generate non-overlapping test folds in chronological order."""
    if mode not in {"rolling", "expanding"}:
        raise WalkForwardError("mode must be 'rolling' or 'expanding'")
    if n_bars <= 0:
        raise WalkForwardError("n_bars must be positive")
    if train_bars <= 1:
        raise WalkForwardError("train_bars must be greater than one")
    if test_bars <= 0:
        raise WalkForwardError("test_bars must be positive")
    if embargo < 0 or warmup < 0:
        raise WalkForwardError("embargo and warmup must be non-negative")
    stride = int(step if step is not None else test_bars)
    if stride <= 0:
        raise WalkForwardError("step must be positive")

    folds: list[FoldSpan] = []
    test_start = train_bars + embargo
    index = 0
    while test_start + test_bars <= n_bars:
        train_end = test_start - embargo
        train_start = 0 if mode == "expanding" else train_end - train_bars
        folds.append(
            FoldSpan(
                index=index,
                train_start=train_start,
                train_end=train_end,
                test_start=test_start,
                test_end=test_start + test_bars,
                warmup_start=max(0, test_start - warmup),
                embargo=embargo,
            )
        )
        index += 1
        test_start += stride
    return folds


def _canonical(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _payload(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_payload(item) for item in value]
    if is_dataclass(value):
        return _payload(asdict(value))
    if hasattr(value, "__dict__"):
        return _payload(vars(value))
    return str(value)


@dataclass
class WalkForwardFold:
    span: FoldSpan
    params: dict[str, Any] | None = None
    train_metrics: dict[str, float] = field(default_factory=dict)
    test_metrics: dict[str, float] = field(default_factory=dict)
    score: float = float("-inf")
    accepted: bool = False
    reason: str = ""
    trials: int = 0
    optimization: OptimizationResult | None = field(default=None, repr=False)
    test_result: Any = field(default=None, repr=False)
    test_trades: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.span.as_dict(),
            "params": _canonical(self.params or {}),
            "train_metrics": _canonical(self.train_metrics),
            "test_metrics": _canonical(self.test_metrics),
            "score": self.score if math.isfinite(self.score) else None,
            "accepted": self.accepted,
            "reason": self.reason,
            "trials": self.trials,
        }


@dataclass
class WalkForwardResult:
    folds: list[WalkForwardFold]
    config: dict[str, Any]
    dataset_hash: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    artifact_path: str | None = None
    code_version: str = ""
    command: str = ""

    @property
    def accepted_folds(self) -> list[WalkForwardFold]:
        return [fold for fold in self.folds if fold.accepted]

    def aggregate(self) -> dict[str, Any]:
        accepted = self.accepted_folds
        tested = [fold for fold in self.folds if fold.test_metrics]
        if not accepted:
            return {
                "folds": len(self.folds),
                "accepted_folds": 0,
                "tested_folds": len(tested),
                "mean_test_return_pct": 0.0,
                "median_test_return_pct": 0.0,
                "positive_folds": 0,
                "positive_fold_pct": 0.0,
                "mean_profit_factor": 0.0,
                "mean_max_drawdown_pct": 0.0,
                "worst_max_drawdown_pct": 0.0,
                "total_test_trades": 0,
                "fold_coverage_pct": 0.0,
                "parameter_stability": 0.0,
                "distinct_parameter_sets": 0,
            }
        returns = [float(fold.test_metrics.get("return_pct", 0.0) or 0.0) for fold in accepted]
        pfs = [float(fold.test_metrics.get("profit_factor", 0.0) or 0.0) for fold in accepted]
        dds = [abs(float(fold.test_metrics.get("max_drawdown_pct", 0.0) or 0.0)) for fold in accepted]
        ordered = sorted(returns)
        middle = len(ordered) // 2
        median = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2.0
        total_bars = max(1, int(self.config.get("n_bars", 0) or 0))
        covered = min(
            total_bars,
            sum(fold.span.test_bars for fold in accepted),
        )
        hashes = [json.dumps(_canonical(fold.params or {}), sort_keys=True) for fold in accepted]
        most_common = Counter(hashes).most_common(1)[0][1] if hashes else 0
        return {
            "folds": len(self.folds),
            "accepted_folds": len(accepted),
            "tested_folds": len(tested),
            "mean_test_return_pct": round(sum(returns) / len(returns), 4),
            "median_test_return_pct": round(median, 4),
            "positive_folds": sum(1 for value in returns if value > 0),
            "positive_fold_pct": round(100.0 * sum(1 for value in returns if value > 0) / len(returns), 2),
            "mean_profit_factor": round(sum(pfs) / len(pfs), 4),
            "mean_max_drawdown_pct": round(sum(dds) / len(dds), 4),
            "worst_max_drawdown_pct": round(max(dds), 4),
            "total_test_trades": sum(int(fold.test_metrics.get("trades", 0) or 0) for fold in accepted),
            "fold_coverage_pct": round(100.0 * covered / total_bars, 2),
            "parameter_stability": round(most_common / len(accepted), 4),
            "distinct_parameter_sets": len(set(hashes)),
        }

    @property
    def reproducibility_hash(self) -> str:
        payload = {
            "config": _canonical(self.config),
            "dataset_hash": self.dataset_hash,
            "folds": [
                {
                    "span": fold.span.as_dict(),
                    "params": _canonical(fold.params or {}),
                    "test_metrics": _canonical(fold.test_metrics),
                }
                for fold in self.folds
            ],
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return {
            "reproducibility_hash": self.reproducibility_hash,
            "dataset_hash": self.dataset_hash,
            "created_at": self.created_at,
            "code_version": self.code_version,
            "command": self.command,
            "config": _canonical(self.config),
            "aggregate": self.aggregate(),
            "folds": [fold.as_dict() for fold in self.folds],
            "artifact_path": self.artifact_path,
        }


def _trades_payload(result: Any) -> list[dict[str, Any]]:
    trades = getattr(result, "trades", None) or []
    return [_payload(trade) for trade in trades]


def write_artifacts(
    result: WalkForwardResult,
    directory: str | Path,
    *,
    include_trials: bool = True,
) -> Path:
    """Write the full reproducibility bundle for a walk-forward run."""
    root = Path(directory)
    path = root / result.reproducibility_hash[:16]
    path.mkdir(parents=True, exist_ok=True)
    result.artifact_path = str(path)
    manifest = result.as_dict()
    (path / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    (path / "config.json").write_text(json.dumps(_canonical(result.config), indent=2, sort_keys=True), encoding="utf-8")
    (path / "metrics.json").write_text(json.dumps(result.aggregate(), indent=2, sort_keys=True), encoding="utf-8")
    (path / "folds.json").write_text(
        json.dumps([fold.as_dict() for fold in result.folds], indent=2, sort_keys=True),
        encoding="utf-8",
    )
    trades = {
        str(fold.span.index): fold.test_trades
        for fold in result.folds
        if fold.test_trades
    }
    (path / "trades.json").write_text(json.dumps(trades, indent=2, sort_keys=True), encoding="utf-8")
    if include_trials:
        trials = {
            str(fold.span.index): [trial.as_dict() for trial in (fold.optimization.trials if fold.optimization else [])]
            for fold in result.folds
        }
        (path / "trials.json").write_text(json.dumps(trials, indent=2, sort_keys=True), encoding="utf-8")
    (path / "command.txt").write_text((result.command or "") + "\n", encoding="utf-8")
    return path


def run_walk_forward(
    candles: pd.DataFrame | Sequence[Mapping[str, Any]],
    evaluate: Callable[[pd.DataFrame, dict[str, Any]], Any],
    space: Mapping[str, Sequence[Any]],
    *,
    train_bars: int,
    test_bars: int,
    step: int | None = None,
    mode: str = "rolling",
    embargo: int = 0,
    warmup: int = 0,
    optimization_mode: str = "random",
    max_trials: int | None = None,
    seed: int = 42,
    min_trades: int = 1,
    max_drawdown_pct: float | None = None,
    objective: Callable[[dict[str, float]], float] | None = None,
    constraint: Callable[[dict[str, Any]], bool] | None = None,
    artifact_dir: str | Path | None = None,
    dataset_hash: str = "",
    strategy_id: str = "",
    code_version: str | None = None,
    command: str | None = None,
    trial_callback: Callable[[FoldSpan, Trial], None] | None = None,
    fold_callback: Callable[[WalkForwardFold], None] | None = None,
) -> WalkForwardResult:
    """Run a leak-free walk-forward study and optionally persist artifacts."""
    df = candles.copy() if isinstance(candles, pd.DataFrame) else pd.DataFrame(list(candles))
    if df.empty:
        raise WalkForwardError("candles are empty")
    if "time" in df.columns:
        times = df["time"].astype("int64")
        if times.duplicated().any():
            raise WalkForwardError("candles contain duplicate timestamps")
        if not times.is_monotonic_increasing:
            raise WalkForwardError("candles must be sorted oldest to newest")
    df = df.reset_index(drop=True)

    spans = generate_folds(
        len(df),
        train_bars=train_bars,
        test_bars=test_bars,
        step=step,
        mode=mode,
        embargo=embargo,
        warmup=warmup,
    )
    if not spans:
        raise WalkForwardError("the requested fold sizes do not fit the dataset")

    config = {
        "n_bars": len(df),
        "train_bars": int(train_bars),
        "test_bars": int(test_bars),
        "step": int(step if step is not None else test_bars),
        "mode": mode,
        "embargo": int(embargo),
        "warmup": int(warmup),
        "optimization_mode": optimization_mode,
        "max_trials": max_trials,
        "seed": int(seed),
        "min_trades": int(min_trades),
        "max_drawdown_pct": max_drawdown_pct,
        "strategy_id": strategy_id,
        "space": {name: list(values) for name, values in space.items()},
    }
    folds: list[WalkForwardFold] = []
    for span in spans:
        train = df.iloc[span.train_start : span.train_end].reset_index(drop=True)
        test = df.iloc[span.warmup_start : span.test_end].reset_index(drop=True)

        def evaluate_train(params: dict[str, Any], _train: pd.DataFrame = train) -> Any:
            return evaluate(_train, params)

        optimization = optimize(
            evaluate_train,
            space,
            mode=optimization_mode,
            max_trials=max_trials,
            seed=seed + span.index,
            min_trades=min_trades,
            max_drawdown_pct=max_drawdown_pct,
            objective=objective,
            constraint=constraint,
        )
        if trial_callback is not None:
            for trial in optimization.trials:
                trial_callback(span, trial)

        best = optimization.best
        fold = WalkForwardFold(span=span, trials=len(optimization.trials), optimization=optimization)
        if best is None:
            fold.reason = "no accepted parameter set in training window"
            folds.append(fold)
            if fold_callback is not None:
                fold_callback(fold)
            continue

        fold.params = dict(best.params)
        fold.train_metrics = dict(best.metrics)
        fold.score = best.score
        try:
            test_result = evaluate(test, fold.params)
        except Exception as exc:
            fold.reason = f"test evaluation failed: {exc}"
            folds.append(fold)
            if fold_callback is not None:
                fold_callback(fold)
            continue
        fold.test_result = test_result
        metrics = getattr(test_result, "metrics", None)
        metrics = metrics() if callable(metrics) else metrics
        fold.test_metrics = {
            str(key): float(value)
            for key, value in (metrics or {}).items()
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))
        }
        fold.test_trades = _trades_payload(test_result)
        fold.accepted = True
        folds.append(fold)
        if fold_callback is not None:
            fold_callback(fold)

    result = WalkForwardResult(
        folds=folds,
        config=config,
        dataset_hash=str(dataset_hash or ""),
        code_version=code_version if code_version is not None else os.environ.get("GIT_COMMIT", ""),
        command=command if command is not None else " ".join(sys.argv),
    )
    if artifact_dir is not None:
        result.artifact_path = str(write_artifacts(result, artifact_dir))
    return result


__all__ = [
    "FoldSpan",
    "WalkForwardError",
    "WalkForwardFold",
    "WalkForwardResult",
    "generate_folds",
    "run_walk_forward",
    "write_artifacts",
]
