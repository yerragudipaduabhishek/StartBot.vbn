"""Deterministic parameter optimization with an auditable trial log.

The optimizer is deliberately independent of the strategy implementation: it
receives a parameter space and an evaluation callable, samples candidates with
a seeded RNG, applies explicit trade/drawdown constraints, and retains every
trial (including rejected ones). It never touches trading state.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any


class OptimizationError(ValueError):
    """The optimization request is invalid."""


def _canonical(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def parameter_hash(params: Mapping[str, Any]) -> str:
    encoded = json.dumps(_canonical(params), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _metrics_of(result: Any) -> dict[str, float]:
    if result is None:
        return {}
    metrics = getattr(result, "metrics", None)
    if callable(metrics):
        metrics = metrics()
    if not isinstance(metrics, Mapping) and isinstance(result, Mapping):
        metrics = result
    if not isinstance(metrics, Mapping):
        return {}
    out: dict[str, float] = {}
    for key, value in metrics.items():
        if isinstance(value, bool):
            out[str(key)] = float(value)
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            out[str(key)] = number
    return out


def default_objective(metrics: Mapping[str, float]) -> float:
    """Reward expectancy with more evidence, penalize drawdown."""
    trades = float(metrics.get("trades", 0.0) or 0.0)
    if trades <= 0:
        return float("-inf")
    expectancy = float(metrics.get("expectancy", 0.0) or 0.0)
    drawdown = abs(float(metrics.get("max_drawdown_pct", 0.0) or 0.0))
    return expectancy * math.sqrt(trades) - 0.5 * drawdown


def sample_parameters(
    space: Mapping[str, Sequence[Any]],
    *,
    mode: str = "random",
    max_trials: int | None = None,
    seed: int = 42,
    constraint: Callable[[dict[str, Any]], bool] | None = None,
) -> list[dict[str, Any]]:
    """Return deterministic candidate parameter dictionaries.

    ``grid`` enumerates all combinations in declaration order. ``random``
    shuffles the same exhaustive grid with a fixed seed and takes the first
    ``max_trials`` entries, so it is sampling without replacement and never
    depends on process hash randomization.
    """
    if mode not in {"grid", "random"}:
        raise OptimizationError("mode must be 'grid' or 'random'")
    if not space:
        raise OptimizationError("parameter space is empty")
    names = list(space)
    values: list[tuple[Any, ...]] = []
    for name in names:
        options = tuple(space[name])
        if not options:
            raise OptimizationError(f"parameter {name!r} has no values")
        values.append(options)

    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for combination in itertools.product(*values):
        params = dict(zip(names, combination))
        digest = parameter_hash(params)
        if digest in seen:
            continue
        seen.add(digest)
        candidates.append(params)

    if constraint is not None:
        candidates = [params for params in candidates if bool(constraint(params))]
    if not candidates:
        raise OptimizationError("no parameter combinations satisfy the constraint")

    if mode == "random":
        random.Random(int(seed)).shuffle(candidates)
    if max_trials is not None:
        if int(max_trials) <= 0:
            raise OptimizationError("max_trials must be positive")
        candidates = candidates[: int(max_trials)]
    return candidates


@dataclass
class Trial:
    index: int
    params: dict[str, Any]
    metrics: dict[str, float]
    score: float
    accepted: bool
    reason: str = ""
    result: Any = field(default=None, repr=False)
    experiment_id: str | None = None

    @property
    def params_hash(self) -> str:
        return parameter_hash(self.params)

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "params": _canonical(self.params),
            "params_hash": self.params_hash,
            "metrics": _canonical(self.metrics),
            "score": self.score if math.isfinite(self.score) else None,
            "accepted": self.accepted,
            "reason": self.reason,
            "experiment_id": self.experiment_id,
        }


@dataclass
class OptimizationResult:
    trials: list[Trial]
    space: dict[str, list[Any]]
    seed: int
    mode: str
    min_trades: int
    max_drawdown_pct: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def best(self) -> Trial | None:
        accepted = [trial for trial in self.trials if trial.accepted]
        return max(accepted, key=lambda trial: trial.score) if accepted else None

    def ranked(self, limit: int | None = None) -> list[Trial]:
        accepted = sorted(
            (trial for trial in self.trials if trial.accepted),
            key=lambda trial: trial.score,
            reverse=True,
        )
        return accepted[:limit] if limit is not None else accepted

    def as_dict(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "mode": self.mode,
            "space": _canonical(self.space),
            "min_trades": self.min_trades,
            "max_drawdown_pct": self.max_drawdown_pct,
            "best": self.best.as_dict() if self.best else None,
            "trials": [trial.as_dict() for trial in self.trials],
            "metadata": _canonical(self.metadata),
        }


def optimize(
    evaluate: Callable[[dict[str, Any]], Any],
    space: Mapping[str, Sequence[Any]],
    *,
    mode: str = "random",
    max_trials: int | None = None,
    seed: int = 42,
    min_trades: int = 1,
    max_drawdown_pct: float | None = None,
    objective: Callable[[dict[str, float]], float] | None = None,
    constraint: Callable[[dict[str, Any]], bool] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> OptimizationResult:
    """Evaluate candidates, retain every trial and return the ranked result."""
    if min_trades < 0:
        raise OptimizationError("min_trades must be non-negative")
    if max_drawdown_pct is not None and not 0 <= float(max_drawdown_pct) <= 100:
        raise OptimizationError("max_drawdown_pct must be in [0, 100]")
    scorer = objective or default_objective
    candidates = sample_parameters(space, mode=mode, max_trials=max_trials, seed=seed, constraint=constraint)
    normalized_space = {name: list(values) for name, values in space.items()}
    trials: list[Trial] = []

    for index, params in enumerate(candidates):
        try:
            result = evaluate(dict(params))
        except Exception as exc:  # a bad candidate must not abort the study
            trials.append(
                Trial(index=index, params=dict(params), metrics={}, score=float("-inf"), accepted=False, reason=f"error: {exc}")
            )
            continue
        metrics = _metrics_of(result)
        trades = int(metrics.get("trades", 0) or 0)
        drawdown = abs(float(metrics.get("max_drawdown_pct", 0.0) or 0.0))
        reason = ""
        if trades < min_trades:
            reason = f"insufficient trades ({trades} < {min_trades})"
        elif max_drawdown_pct is not None and drawdown > float(max_drawdown_pct):
            reason = f"drawdown {drawdown:.3f}% exceeds {float(max_drawdown_pct):.3f}%"
        try:
            score = float(scorer(metrics))
        except Exception as exc:
            score = float("-inf")
            reason = reason or f"objective error: {exc}"
        if not math.isfinite(score):
            reason = reason or "objective is not finite"
        accepted = not reason and math.isfinite(score)
        trials.append(
            Trial(
                index=index,
                params=dict(params),
                metrics=metrics,
                score=score if math.isfinite(score) else float("-inf"),
                accepted=accepted,
                reason=reason,
                result=result,
            )
        )

    return OptimizationResult(
        trials=trials,
        space=normalized_space,
        seed=int(seed),
        mode=mode,
        min_trades=int(min_trades),
        max_drawdown_pct=None if max_drawdown_pct is None else float(max_drawdown_pct),
        metadata=dict(metadata or {}),
    )


__all__ = [
    "OptimizationError",
    "OptimizationResult",
    "Trial",
    "default_objective",
    "optimize",
    "parameter_hash",
    "sample_parameters",
]
