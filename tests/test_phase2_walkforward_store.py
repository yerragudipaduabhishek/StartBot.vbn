"""Walk-forward trial/fold persistence into the isolated research store."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
from sqlalchemy import func, select

from app.backtest.walkforward import run_walk_forward
from app.research.models import (
    ResearchWalkForwardFold,
    ResearchWalkForwardRun,
    ResearchWalkForwardTrial,
)
from app.research.store import ResearchStore


def _frame(n: int = 40, *, start: int = 0) -> pd.DataFrame:
    indices = [start + i for i in range(n)]
    return pd.DataFrame(
        {
            "time": [1_700_000_000 + i * 300 for i in indices],
            "open": [100.0 + i for i in indices],
            "high": [101.0 + i for i in indices],
            "low": [99.0 + i for i in indices],
            "close": [100.5 + i for i in indices],
            "volume": [10.0] * n,
        }
    )


@dataclass
class _Result:
    metrics_value: dict

    def metrics(self) -> dict:
        return self.metrics_value


def _evaluate(df, params):
    return _Result(
        {
            "trades": 10,
            "expectancy": float(df["close"].iloc[-1]) + params["x"],
            "return_pct": float(df["close"].iloc[-1]) / 100.0,
            "max_drawdown_pct": 2.0,
            "win_rate": 0.5,
            "profit_factor": 1.2,
        }
    )


def test_walk_forward_run_folds_and_trials_are_persisted_idempotently(tmp_path):
    store = ResearchStore(database_url=f"sqlite:///{tmp_path / 'research.db'}", create=True)
    store.record_dataset(
        {
            "dataset_hash": "dataset-abc",
            "symbol": "BTCUSDT",
            "timeframe": "5m",
            "rows": 50,
            "start": 1,
            "end": 50,
        }
    )
    result = run_walk_forward(
        _frame(50),
        _evaluate,
        {"x": (1, 2)},
        train_bars=30,
        test_bars=10,
        optimization_mode="grid",
        dataset_hash="dataset-abc",
        strategy_id="strategy_a",
        artifact_dir=tmp_path / "artifacts",
    )
    experiment = store.record_experiment(
        strategy_id="strategy_a",
        strategy_version="1.0.0",
        symbol="BTCUSDT",
        timeframe="5m",
        dataset_hash="dataset-abc",
        params={"x": 1},
        metrics=result.aggregate(),
    )
    run = store.record_walk_forward(result, experiment_id=experiment.experiment_id, strategy_id="strategy_a")
    assert run.run_id == result.reproducibility_hash
    assert run.experiment_id == experiment.experiment_id
    assert run.artifact_path == result.artifact_path
    assert run.dataset_hash == "dataset-abc"

    with store.SessionLocal() as db:
        folds = db.execute(select(func.count()).select_from(ResearchWalkForwardFold)).scalar_one()
        trials = db.execute(select(func.count()).select_from(ResearchWalkForwardTrial)).scalar_one()
        assert folds == len(result.folds)
        assert trials == sum(len(fold.optimization.trials) for fold in result.folds if fold.optimization)

    replay = store.record_walk_forward(result, experiment_id=experiment.experiment_id)
    assert replay.id == run.id
    with store.SessionLocal() as db:
        assert db.execute(select(func.count()).select_from(ResearchWalkForwardRun)).scalar_one() == 1
        assert db.execute(select(func.count()).select_from(ResearchWalkForwardFold)).scalar_one() == folds
        assert db.execute(select(func.count()).select_from(ResearchWalkForwardTrial)).scalar_one() == trials

    assert store.get_walk_forward_run(run.run_id).id == run.id
    assert [row.run_id for row in store.list_walk_forward_runs()] == [run.run_id]


def test_walk_forward_api_exposes_persisted_run(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import app.main as main

    store = ResearchStore(database_url=f"sqlite:///{tmp_path / 'research.db'}", create=True)
    store.record_dataset(
        {
            "dataset_hash": "dataset-abc",
            "symbol": "BTCUSDT",
            "timeframe": "5m",
            "rows": 50,
            "start": 1,
            "end": 50,
        }
    )
    result = run_walk_forward(
        _frame(50),
        _evaluate,
        {"x": (1, 2)},
        train_bars=30,
        test_bars=10,
        optimization_mode="grid",
        dataset_hash="dataset-abc",
        strategy_id="strategy_a",
    )
    experiment = store.record_experiment(
        strategy_id="strategy_a",
        strategy_version="1.0.0",
        symbol="BTCUSDT",
        timeframe="5m",
        dataset_hash="dataset-abc",
        params={"x": 1},
        metrics=result.aggregate(),
    )
    store.record_walk_forward(result, experiment_id=experiment.experiment_id, strategy_id="strategy_a")
    monkeypatch.setattr(main, "state", {"research_store": store})

    rows = TestClient(main.app).get("/research/walk-forward").json()
    assert len(rows) == 1
    assert rows[0]["run_id"] == result.reproducibility_hash
    assert rows[0]["experiment_id"] == experiment.experiment_id
    assert rows[0]["strategy_id"] == "strategy_a"
    assert rows[0]["dataset_hash"] == "dataset-abc"
    assert rows[0]["folds"] == len(result.folds)
    assert rows[0]["aggregate"]["accepted_folds"] == result.aggregate()["accepted_folds"]
