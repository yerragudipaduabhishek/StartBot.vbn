"""Phase 6 research/governance API contract and admin gating."""

from __future__ import annotations

from fastapi.testclient import TestClient

import app.main as main
from app.research.governance import GovernanceService
from app.research.store import ResearchStore

TOKEN = "test-admin-token"


def _client(monkeypatch, tmp_path):
    store = ResearchStore(database_url=f"sqlite:///{tmp_path / 'research.db'}", create=True)
    store.record_dataset(
        {
            "dataset_hash": "dataset-abc",
            "symbol": "BTCUSDT",
            "timeframe": "1h",
            "rows": 100,
            "start": 1,
            "end": 100,
        }
    )
    monkeypatch.setattr(
        main,
        "state",
        {"research_store": store, "governance": GovernanceService(store)},
    )
    monkeypatch.setattr(main, "settings", main.settings.model_copy(update={"admin_token": TOKEN}))
    return TestClient(main.app)


def _create(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    response = client.post(
        "/research/proposals",
        headers={"X-Admin-Token": TOKEN},
        json={
            "strategy_id": "strategy_a",
            "strategy_version": "v2",
            "dataset_hash": "dataset-abc",
            "metrics": {"walk_forward": {"accepted_folds": 4, "total_test_trades": 40, "fold_coverage_pct": 75}},
            "params": {"ema_slow": 50},
        },
    )
    assert response.status_code == 200, response.text
    return client, response.json()


def test_research_mutations_require_admin_token(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    response = client.post(
        "/research/proposals",
        json={"strategy_id": "strategy_a", "strategy_version": "v2", "dataset_hash": "dataset-abc"},
    )
    assert response.status_code == 403


def test_research_proposal_review_and_gate_truth(monkeypatch, tmp_path):
    client, proposal = _create(monkeypatch, tmp_path)
    assert proposal["status"] == "draft"
    assert proposal["dataset_hash"] == "dataset-abc"

    listed = client.get("/research/proposals").json()
    assert [row["proposal_id"] for row in listed] == [proposal["proposal_id"]]

    gates = client.get(f"/research/proposals/{proposal['proposal_id']}/gates").json()
    assert gates["passed"] is False
    assert gates["dataset"]["passed"] is True
    assert any("artifact" in reason for reason in gates["failed_reasons"])

    reviewed = client.post(
        f"/research/proposals/{proposal['proposal_id']}/reviews",
        headers={"X-Admin-Token": TOKEN},
        json={"reviewer": "reviewer@example.com", "approved": True, "findings": []},
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["approved"] is True

    approved = client.post(
        f"/research/proposals/{proposal['proposal_id']}/approve",
        headers={"X-Admin-Token": TOKEN},
        json={"approver": "human@example.com", "rationale": "reviewed artifacts"},
    )
    assert approved.status_code == 200
    assert approved.json()["approver"] == "human@example.com"

    promoted = client.post(
        f"/research/proposals/{proposal['proposal_id']}/promote",
        headers={"X-Admin-Token": TOKEN},
        json={"approver": "human@example.com"},
    )
    assert promoted.status_code == 400
    assert "gates failed" in promoted.json()["detail"]


def test_research_canary_and_rollback_errors_are_explicit(monkeypatch, tmp_path):
    client, proposal = _create(monkeypatch, tmp_path)
    started = client.post(
        f"/research/proposals/{proposal['proposal_id']}/canary/start",
        headers={"X-Admin-Token": TOKEN},
        json={"duration_seconds": 60, "min_trades": 1},
    )
    assert started.status_code == 400
    assert "gates failed" in started.json()["detail"]

    rollback = client.post(
        "/research/strategies/strategy_a/rollback",
        headers={"X-Admin-Token": TOKEN},
        json={"actor": "human@example.com", "reason": "regression"},
    )
    assert rollback.status_code == 400
    assert "no active promotion" in rollback.json()["detail"]


def test_research_reads_are_empty_not_fabricated(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    assert client.get("/research/experiments").json() == []
    assert client.get("/research/proposals").json() == []
    assert client.get("/research/strategies").json() == []
    assert client.get("/research/promotions").json() == []
