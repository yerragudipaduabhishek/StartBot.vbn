"""Phase 2 optimizer, leakage-free walk-forward, artifacts and status."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pandas as pd

from app.backtest.optimize import optimize, parameter_hash, sample_parameters
from app.backtest.walkforward import generate_folds, run_walk_forward, write_artifacts
from app.strategy.status import UNVALIDATED, VALIDATED, validation_status


def _frame(n: int = 40, *, start: int = 0, test_offset: float = 0.0) -> pd.DataFrame:
    indices = [start + i for i in range(n)]
    return pd.DataFrame(
        {
            "time": [1_700_000_000 + i * 300 for i in indices],
            "open": [100.0 + i + test_offset for i in indices],
            "high": [101.0 + i + test_offset for i in indices],
            "low": [99.0 + i + test_offset for i in indices],
            "close": [100.5 + i + test_offset for i in indices],
            "volume": [10.0] * n,
        }
    )


@dataclass
class _Result:
    metrics_value: dict

    def metrics(self) -> dict:
        return self.metrics_value


def _evaluate(df, params):
    last = float(df["close"].iloc[-1])
    return _Result(
        {
            "trades": 10,
            "expectancy": last + params["x"],
            "return_pct": last / 100.0,
            "max_drawdown_pct": 2.0,
        }
    )


def test_sampling_and_optimizer_are_deterministic_and_keep_every_trial():
    space = {"x": (1, 2, 3), "y": ("a", "b")}
    first = sample_parameters(space, mode="random", max_trials=4, seed=7)
    second = sample_parameters(space, mode="random", max_trials=4, seed=7)
    assert first == second
    assert len(first) == 4

    seen: list[dict] = []

    def evaluate(params):
        seen.append(dict(params))
        return {"trades": 10, "expectancy": 1.0, "max_drawdown_pct": 1.0}

    result = optimize(evaluate, space, mode="grid", seed=11, min_trades=5)
    assert len(result.trials) == 6
    assert len(seen) == 6
    assert result.best is not None
    assert all(trial.accepted for trial in result.trials)
    assert parameter_hash(result.trials[0].params) == result.trials[0].params_hash

    constrained = optimize(evaluate, space, mode="grid", constraint=lambda p: p["x"] > 1)
    assert len(constrained.trials) == 4


def test_optimizer_retains_rejected_trials_and_errors():
    def evaluate(params):
        if params["x"] == 2:
            raise RuntimeError("boom")
        return {"trades": 1, "expectancy": 0.0, "max_drawdown_pct": 10.0}

    result = optimize(
        evaluate,
        {"x": (1, 2, 3)},
        mode="grid",
        min_trades=5,
        max_drawdown_pct=5.0,
    )
    assert len(result.trials) == 3
    assert all(not trial.accepted for trial in result.trials)
    assert any("error" in trial.reason for trial in result.trials)
    assert any("insufficient" in trial.reason for trial in result.trials)


def test_fold_generation_is_chronological_and_has_no_overlap():
    folds = generate_folds(200, train_bars=80, test_bars=30, step=30, embargo=5, warmup=10)
    assert folds
    for fold in folds:
        assert fold.test_start >= fold.train_end + fold.embargo
        assert fold.test_end <= 200
    assert all(b.test_start > a.test_start for a, b in zip(folds, folds[1:]))

    expanding = generate_folds(200, train_bars=80, test_bars=30, mode="expanding")
    assert expanding[0].train_start == 0
    assert expanding[-1].train_bars > expanding[0].train_bars


def test_walk_forward_never_uses_test_data_to_choose_parameters():
    train = _frame(30)
    clean = pd.concat([train, _frame(20, start=30, test_offset=1000)], ignore_index=True)
    corrupted = pd.concat([train, _frame(20, start=30, test_offset=-500)], ignore_index=True)

    def evaluate(df, params):
        last = float(df["close"].iloc[-1])
        return _Result(
            {
                "trades": 10,
                "expectancy": last + params["x"],
                "return_pct": 1.0,
                "max_drawdown_pct": 1.0,
                "max_time": int(df["time"].max()),
            }
        )

    kwargs = dict(
        train_bars=30,
        test_bars=10,
        optimization_mode="grid",
        space={"x": (1, 2, 3)},
        min_trades=1,
    )
    a = run_walk_forward(clean, evaluate, **kwargs)
    b = run_walk_forward(corrupted, evaluate, **kwargs)
    assert a.folds and b.folds
    assert a.folds[0].params == b.folds[0].params
    times = clean["time"].tolist()
    for fold in a.folds:
        assert fold.train_metrics["max_time"] < times[fold.span.test_start]


def test_warmup_bars_keep_training_data_out_of_test_metrics():
    frame = _frame(50)
    times = frame["time"].tolist()

    def evaluate(df, params):
        traded = df.iloc[5:]  # engine consumes exactly the 5 prepended warmup bars
        return _Result(
            {
                "trades": len(traded),
                "expectancy": 1.0,
                "return_pct": 1.0,
                "max_drawdown_pct": 1.0,
                "first_trade_time": int(traded["time"].iloc[0]),
                "max_time": int(df["time"].max()),
            }
        )

    result = run_walk_forward(
        frame,
        evaluate,
        {"x": (1,)},
        train_bars=30,
        test_bars=10,
        warmup=5,
        optimization_mode="grid",
    )
    assert result.folds
    for fold in result.folds:
        assert fold.span.warmup_start == fold.span.test_start - 5
        assert fold.test_metrics["first_trade_time"] >= times[fold.span.test_start]
        assert fold.train_metrics["max_time"] < times[fold.span.test_start]


def test_walk_forward_writes_a_reproducible_artifact_bundle(tmp_path):
    kwargs = dict(
        train_bars=30,
        test_bars=10,
        optimization_mode="grid",
        space={"x": (1, 2)},
        dataset_hash="dataset-abc",
        strategy_id="strategy_a",
        artifact_dir=tmp_path,
    )
    first = run_walk_forward(_frame(50), _evaluate, **kwargs)
    second = run_walk_forward(_frame(50), _evaluate, **kwargs)
    assert first.reproducibility_hash == second.reproducibility_hash
    path = tmp_path / first.reproducibility_hash[:16]
    for name in ("manifest.json", "config.json", "metrics.json", "folds.json", "trades.json", "trials.json", "command.txt"):
        assert (path / name).is_file()
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["dataset_hash"] == "dataset-abc"
    assert manifest["config"]["strategy_id"] == "strategy_a"
    assert manifest["aggregate"]["accepted_folds"] >= 1


def test_status_is_derived_from_a_verified_artifact(tmp_path):
    assert validation_status("strategy_a", artifacts_dir=tmp_path).status == UNVALIDATED

    result = run_walk_forward(
        _frame(50),
        _evaluate,
        train_bars=30,
        test_bars=10,
        optimization_mode="grid",
        space={"x": (1, 2)},
        dataset_hash="dataset-abc",
        strategy_id="strategy_a",
        artifact_dir=tmp_path,
    )
    status = validation_status("strategy_a", artifacts_dir=tmp_path)
    assert status.status == VALIDATED
    assert status.run_id == result.reproducibility_hash

    manifest_path = tmp_path / result.reproducibility_hash[:16] / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["folds"][0]["test_start"] = payload["folds"][0]["train_start"]
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    assert validation_status("strategy_a", artifacts_dir=tmp_path).status == UNVALIDATED


def test_write_artifacts_is_idempotent(tmp_path):
    result = run_walk_forward(
        _frame(50),
        _evaluate,
        train_bars=30,
        test_bars=10,
        optimization_mode="grid",
        space={"x": (1, 2)},
    )
    first = write_artifacts(result, tmp_path)
    second = write_artifacts(result, tmp_path)
    assert first == second
    assert (first / "manifest.json").is_file()
