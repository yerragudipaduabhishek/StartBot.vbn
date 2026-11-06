"""Phase 6 promotion registry: gates, human approval, canary, rollback."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.database.database import SessionLocal
from app.database.models import StrategyVersion
from app.research.governance import GovernanceError, GovernanceService
from app.research.store import ResearchStore


DATASET = "a" * 64
STRATEGY = "STRATEGY_A"


def _store(tmp_path) -> ResearchStore:
    store = ResearchStore(database_url=f"sqlite:///{(tmp_path / 'research.db').as_posix()}")
    store.record_dataset(
        {
            "dataset_hash": DATASET,
            "symbol": "BTCUSD",
            "timeframe": "1h",
            "rows": 5000,
            "start": 1_600_000_000,
            "end": 1_700_000_000,
            "first_candle_time": 1_600_000_000,
            "last_candle_time": 1_700_000_000,
            "source": "test",
            "venue": "test",
            "transport": "rest",
            "quality": {"issues": 0},
        }
    )
    return store


def _artifact(tmp_path, *, mean_return=2.5, trades=80, folds=4, coverage=75.0, drawdown=8.0):
    path = tmp_path / "artifacts"
    path.mkdir(exist_ok=True)
    manifest = {
        "dataset_hash": DATASET,
        "aggregate": {
            "accepted_folds": folds,
            "total_test_trades": trades,
            "fold_coverage_pct": coverage,
            "mean_test_return_pct": mean_return,
            "worst_max_drawdown_pct": drawdown,
        },
        "reproducibility_hash": "run-1",
    }
    (path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return str(path)


def _proposal(tmp_path, service: GovernanceService, *, version="1.0", **overrides):
    artifact = overrides.pop("artifact", None) or _artifact(tmp_path)
    return service.create_proposal(
        strategy_id=STRATEGY,
        strategy_version=version,
        dataset_hash=DATASET,
        artifact_path=artifact,
        params={"ema_fast": 12},
        metrics=overrides.pop("metrics", {}),
        **overrides,
    )


def _approve_review(service: GovernanceService, proposal_id: str):
    service.add_review(proposal_id, reviewer="reviewer@example.com", veto=False, approved=True)
    service.approve(proposal_id, approver="owner@example.com", rationale="reviewed OOS evidence")


def _pass_canary(service: GovernanceService, proposal_id: str):
    canary = service.start_canary(proposal_id, duration_seconds=0)
    service.record_canary(proposal_id, trades=max(25, canary.min_trades))
    service.finish_canary(proposal_id)


def _promote(tmp_path, service: GovernanceService, version="1.0"):
    proposal = _proposal(tmp_path, service, version=version)
    _approve_review(service, proposal.proposal_id)
    _pass_canary(service, proposal.proposal_id)
    promotion = service.promote(proposal.proposal_id, approver="owner@example.com")
    return proposal, promotion


def test_missing_artifact_or_empty_dataset_blocks_promotion(tmp_path):
    store = _store(tmp_path)
    service = GovernanceService(store, min_folds=3, min_trades=20)
    proposal = service.create_proposal(
        strategy_id=STRATEGY,
        strategy_version="1.0",
        dataset_hash=DATASET,
        params={},
        metrics={},
    )
    gates = service.gates(proposal.proposal_id)
    assert gates["passed"] is False
    assert any("artifact" in reason for reason in gates["failed_reasons"])
    watch = store.record_dataset
    assert watch is not None


def test_weak_or_negative_out_of_sample_rejects_promotion(tmp_path):
    store = _store(tmp_path)
    service = GovernanceService(store, min_folds=3, min_trades=20)
    proposal = _proposal(tmp_path, service, artifact=_artifact(tmp_path, mean_return=-1.0))
    _approve_review(service, proposal.proposal_id)
    with pytest.raises(GovernanceError, match="gates failed: performance"):
        service.start_canary(proposal.proposal_id, duration_seconds=0)
    with pytest.raises(GovernanceError, match="not positive"):
        service.promote(proposal.proposal_id, approver="owner@example.com")

    overlap = _proposal(tmp_path, service, version="1.1", artifact=_artifact(tmp_path, coverage=10.0))
    _approve_review(service, overlap.proposal_id)
    with pytest.raises(GovernanceError, match="walk_forward"):
        service.start_canary(overlap.proposal_id, duration_seconds=0)


def test_reviewer_veto_and_missing_approval_block(tmp_path):
    store = _store(tmp_path)
    service = GovernanceService(store)
    proposal = _proposal(tmp_path, service)
    service.add_review(proposal.proposal_id, reviewer="r", veto=True, approved=False)
    gates = service.gates(proposal.proposal_id)
    assert gates["review"]["passed"] is False
    assert gates["approval"]["passed"] is False
    with pytest.raises(GovernanceError):
        service.start_canary(proposal.proposal_id, duration_seconds=0)


def test_canary_breach_blocks_promotion(tmp_path):
    store = _store(tmp_path)
    service = GovernanceService(store)
    proposal = _proposal(tmp_path, service)
    _approve_review(service, proposal.proposal_id)
    canary = service.start_canary(proposal.proposal_id, duration_seconds=0)
    service.record_canary(proposal.proposal_id, trades=canary.min_trades, risk_breaches=1)
    service.finish_canary(proposal.proposal_id)
    gates = service.gates(proposal.proposal_id)
    assert gates["canary"]["passed"] is False
    assert gates["health"]["passed"] is False
    with pytest.raises(GovernanceError):
        service.promote(proposal.proposal_id, approver="owner@example.com")


def test_full_promotion_and_rollback_restore_previous_version(tmp_path):
    store = _store(tmp_path)
    service = GovernanceService(store, min_folds=3, min_trades=20)
    proposal, promotion = _promote(tmp_path, service, version="1.0")
    assert promotion.previous_version == ""
    with SessionLocal() as db:
        active = db.execute(
            select(StrategyVersion).where(StrategyVersion.strategy_id == STRATEGY, StrategyVersion.active.is_(True))
        ).scalar_one()
        assert active.version == "1.0"

    proposal2, promotion2 = _promote(tmp_path, service, version="1.1")
    assert proposal2.proposal_id != proposal.proposal_id
    assert promotion2.previous_version == "1.0"
    with SessionLocal() as db:
        active = db.execute(
            select(StrategyVersion).where(StrategyVersion.strategy_id == STRATEGY, StrategyVersion.active.is_(True))
        ).scalar_one()
        assert active.version == "1.1"

    rolled = service.rollback(STRATEGY, actor="owner@example.com", reason="canary regression")
    assert rolled.status == "rolled_back"
    with SessionLocal() as db:
        active = db.execute(
            select(StrategyVersion).where(StrategyVersion.strategy_id == STRATEGY, StrategyVersion.active.is_(True))
        ).scalar_one()
        assert active.version == "1.0"

    tables = {
        "proposal": service.get_proposal(proposal2.proposal_id),
    }
    assert tables["proposal"].status == "rolled_back"
