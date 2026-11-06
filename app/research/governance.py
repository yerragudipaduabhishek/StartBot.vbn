"""Human-approved strategy promotion, canary and rollback.

No path in this module promotes automatically. A proposal must pass dataset,
walk-forward, drawdown, review, human-approval, canary and health gates; every
step is persisted for audit. Promotion only changes a paper strategy pin.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.database.database import SessionLocal
from app.database.models import StrategyVersion
from app.research.models import (
    ResearchApproval,
    ResearchCanary,
    ResearchPromotion,
    ResearchProposal,
    ResearchReview,
)
from app.research.store import ResearchStore


class GovernanceError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json(payload: Any) -> str:
    return json.dumps(payload or {}, sort_keys=True, default=str)


def _loads(value: str | None) -> Any:
    try:
        return json.loads(value or "{}")
    except ValueError:
        return {}


class GovernanceService:
    def __init__(
        self,
        store: ResearchStore,
        *,
        trading_session_factory=SessionLocal,
        min_folds: int = 3,
        min_trades: int = 20,
        min_fold_coverage_pct: float = 50.0,
        max_drawdown_pct: float = 25.0,
        require_positive_oos: bool = True,
    ) -> None:
        self.store = store
        self.trading_session_factory = trading_session_factory
        self.min_folds = int(min_folds)
        self.min_trades = int(min_trades)
        self.min_fold_coverage_pct = float(min_fold_coverage_pct)
        self.max_drawdown_pct = float(max_drawdown_pct)
        self.require_positive_oos = bool(require_positive_oos)

    # ------------------------------------------------------------- proposals
    def create_proposal(
        self,
        *,
        strategy_id: str,
        strategy_version: str,
        experiment_id: str = "",
        dataset_hash: str = "",
        artifact_path: str = "",
        params: dict | None = None,
        metrics: dict | None = None,
        proposal_id: str | None = None,
    ) -> ResearchProposal:
        if not strategy_id or not strategy_version:
            raise GovernanceError("strategy id and version are required")
        experiment = self.store.get_experiment(experiment_id) if experiment_id else None
        if experiment_id and experiment is None:
            raise GovernanceError(f"experiment {experiment_id} is not recorded")
        dataset_hash = str(dataset_hash or (experiment.dataset_hash if experiment else "") or "")
        if not dataset_hash:
            raise GovernanceError("a proposal requires a recorded dataset hash")
        document = dict(metrics or {})
        if artifact_path:
            manifest = self._read_manifest(artifact_path)
            if manifest:
                document.setdefault("walk_forward", manifest.get("aggregate") or {})
                document.setdefault("artifact_dataset_hash", manifest.get("dataset_hash"))
                dataset_hash = str(manifest.get("dataset_hash") or dataset_hash)
        proposal_id = proposal_id or uuid.uuid4().hex
        with self.store.SessionLocal() as db:
            existing = db.execute(
                select(ResearchProposal).where(ResearchProposal.proposal_id == proposal_id)
            ).scalar_one_or_none()
            if existing is not None:
                raise GovernanceError(f"proposal {proposal_id} already exists")
            row = ResearchProposal(
                proposal_id=proposal_id,
                experiment_id=str(experiment_id or ""),
                strategy_id=str(strategy_id),
                strategy_version=str(strategy_version),
                dataset_hash=dataset_hash,
                artifact_path=str(artifact_path or ""),
                params_json=_json(params or {}),
                metrics_json=_json(document),
                status="draft",
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return row

    def get_proposal(self, proposal_id: str) -> ResearchProposal | None:
        with self.store.SessionLocal() as db:
            return db.execute(
                select(ResearchProposal).where(ResearchProposal.proposal_id == proposal_id)
            ).scalar_one_or_none()

    def _read_manifest(self, artifact_path: str) -> dict:
        path = Path(artifact_path)
        if path.is_dir():
            path = path / "manifest.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    # --------------------------------------------------------------- reviews
    def add_review(
        self,
        proposal_id: str,
        *,
        reviewer: str,
        veto: bool,
        approved: bool = False,
        findings: list[dict] | None = None,
        required_changes: list[str] | None = None,
    ) -> ResearchReview:
        self._require_proposal(proposal_id)
        with self.store.SessionLocal() as db:
            row = ResearchReview(
                proposal_id=str(proposal_id),
                reviewer=str(reviewer or ""),
                veto=bool(veto),
                approved=bool(approved),
                findings_json=_json(findings or []),
                required_changes_json=_json(required_changes or []),
            )
            db.add(row)
            proposal = db.execute(
                select(ResearchProposal).where(ResearchProposal.proposal_id == proposal_id)
            ).scalar_one()
            proposal.status = "rejected" if veto else "reviewed"
            proposal.updated_at = _now()
            db.commit()
            db.refresh(row)
            return row

    def approve(self, proposal_id: str, *, approver: str, rationale: str) -> ResearchApproval:
        self._require_proposal(proposal_id)
        if not str(approver or "").strip():
            raise GovernanceError("approver identity is required")
        if not str(rationale or "").strip():
            raise GovernanceError("approval rationale is required")
        with self.store.SessionLocal() as db:
            existing = db.execute(
                select(ResearchApproval).where(ResearchApproval.proposal_id == proposal_id)
            ).scalar_one_or_none()
            if existing is not None:
                return existing
            row = ResearchApproval(
                proposal_id=str(proposal_id),
                approver=str(approver),
                rationale=str(rationale),
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return row

    # ---------------------------------------------------------------- canary
    def start_canary(
        self,
        proposal_id: str,
        *,
        duration_seconds: int,
        min_trades: int | None = None,
    ) -> ResearchCanary:
        self._require_proposal(proposal_id)
        gates = self.gates(proposal_id, include_canary=False)
        failed = [
            name
            for name, gate in gates.items()
            if isinstance(gate, dict) and not gate.get("passed", False) and name not in {"canary", "health"}
        ]
        if failed:
            raise GovernanceError(f"canary cannot start; gates failed: {', '.join(failed)}")
        with self.store.SessionLocal() as db:
            row = ResearchCanary(
                proposal_id=str(proposal_id),
                started_at=_now(),
                duration_seconds=max(0, int(duration_seconds)),
                min_trades=int(self.min_trades if min_trades is None else min_trades),
                status="running",
            )
            db.add(row)
            proposal = db.execute(
                select(ResearchProposal).where(ResearchProposal.proposal_id == proposal_id)
            ).scalar_one()
            proposal.status = "canary"
            proposal.updated_at = _now()
            db.commit()
            db.refresh(row)
            return row

    def record_canary(
        self,
        proposal_id: str,
        *,
        trades: int,
        risk_breaches: int = 0,
        reconciliation_ok: bool = True,
        alerts_ok: bool = True,
    ) -> ResearchCanary:
        with self.store.SessionLocal() as db:
            row = db.execute(
                select(ResearchCanary)
                .where(ResearchCanary.proposal_id == proposal_id)
                .order_by(ResearchCanary.id.desc())
                .limit(1)
            ).scalar_one_or_none()
            if row is None:
                raise GovernanceError("no canary has been started")
            row.trades = max(0, int(trades))
            row.risk_breaches = max(0, int(risk_breaches))
            row.reconciliation_ok = bool(reconciliation_ok)
            row.alerts_ok = bool(alerts_ok)
            row.updated_at = _now()
            if row.risk_breaches or not row.reconciliation_ok or not row.alerts_ok:
                row.status = "failed"
            elif row.trades >= row.min_trades:
                row.status = "passed"
            else:
                row.status = "running"
            db.commit()
            db.refresh(row)
            return row

    def finish_canary(self, proposal_id: str) -> ResearchCanary:
        with self.store.SessionLocal() as db:
            row = db.execute(
                select(ResearchCanary)
                .where(ResearchCanary.proposal_id == proposal_id)
                .order_by(ResearchCanary.id.desc())
                .limit(1)
            ).scalar_one_or_none()
            if row is None:
                raise GovernanceError("no canary has been started")
            now = _now()
            started = row.started_at
            if started is not None and started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            elapsed = (now - started).total_seconds() if started else 0.0
            if elapsed < row.duration_seconds:
                raise GovernanceError(f"canary duration not elapsed ({elapsed:.0f}s < {row.duration_seconds}s)")
            row.ended_at = now
            row.updated_at = now
            if row.risk_breaches or not row.reconciliation_ok or not row.alerts_ok or row.trades < row.min_trades:
                row.status = "failed"
            else:
                row.status = "passed"
            db.commit()
            db.refresh(row)
            return row

    # ----------------------------------------------------------------- gates
    def gates(self, proposal_id: str, *, include_canary: bool = True) -> dict[str, dict]:
        proposal = self._require_proposal(proposal_id)
        metrics = _loads(proposal.metrics_json)
        walk = metrics.get("walk_forward") if isinstance(metrics.get("walk_forward"), dict) else {}
        accepted_folds = int(walk.get("accepted_folds", 0) or 0)
        trades = int(walk.get("total_test_trades", 0) or 0)
        coverage = float(walk.get("fold_coverage_pct", 0.0) or 0.0)
        mean_return = float(walk.get("mean_test_return_pct", 0.0) or 0.0)
        worst_dd = abs(float(walk.get("worst_max_drawdown_pct", 0.0) or 0.0))
        dataset = self.store.get_dataset(proposal.dataset_hash) if proposal.dataset_hash else None
        artifact_ok = bool(proposal.artifact_path) and bool(self._read_manifest(proposal.artifact_path))

        with self.store.SessionLocal() as db:
            reviews = list(
                db.execute(select(ResearchReview).where(ResearchReview.proposal_id == proposal_id)).scalars()
            )
            approval = db.execute(
                select(ResearchApproval).where(ResearchApproval.proposal_id == proposal_id)
            ).scalar_one_or_none()
            canary = db.execute(
                select(ResearchCanary)
                .where(ResearchCanary.proposal_id == proposal_id)
                .order_by(ResearchCanary.id.desc())
                .limit(1)
            ).scalar_one_or_none()

        failed: list[str] = []
        if dataset is None or int(dataset.rows or 0) <= 0:
            failed.append("recorded dataset is missing or empty")
        if not proposal.dataset_hash:
            failed.append("dataset hash missing")
        if not artifact_ok:
            failed.append("validation artifact is missing or unreadable")
        if accepted_folds < self.min_folds:
            failed.append(f"fold coverage {accepted_folds} < {self.min_folds}")
        if trades < self.min_trades:
            failed.append(f"test trades {trades} < {self.min_trades}")
        if coverage < self.min_fold_coverage_pct:
            failed.append(f"fold coverage {coverage:.1f}% < {self.min_fold_coverage_pct:.1f}%")
        if self.require_positive_oos and mean_return <= 0:
            failed.append(f"out-of-sample return {mean_return:.3f}% is not positive")
        if worst_dd > self.max_drawdown_pct:
            failed.append(f"worst drawdown {worst_dd:.3f}% > {self.max_drawdown_pct:.3f}%")
        if metrics.get("cost_sensitivity_ok") is False:
            failed.append("cost sensitivity check failed")
        if not reviews:
            failed.append("no reviewer critique recorded")
        if any(review.veto for review in reviews):
            failed.append("reviewer veto is unresolved")
        if approval is None:
            failed.append("human approval is missing")
        if include_canary:
            if canary is None or canary.status != "passed":
                failed.append("paper canary has not passed")
            elif canary.reconciliation_ok is not True or canary.alerts_ok is not True or canary.risk_breaches:
                failed.append("canary health/reconciliation breach")

        return {
            "dataset": {"passed": dataset is not None and int(dataset.rows or 0) > 0, "reasons": []},
            "walk_forward": {
                "passed": accepted_folds >= self.min_folds and trades >= self.min_trades and coverage >= self.min_fold_coverage_pct,
                "accepted_folds": accepted_folds,
                "test_trades": trades,
                "fold_coverage_pct": coverage,
            },
            "performance": {
                "passed": (mean_return > 0 or not self.require_positive_oos) and worst_dd <= self.max_drawdown_pct,
                "mean_test_return_pct": mean_return,
                "worst_max_drawdown_pct": worst_dd,
            },
            "review": {
                "passed": bool(reviews) and not any(review.veto for review in reviews),
                "reviews": len(reviews),
            },
            "approval": {"passed": approval is not None, "approver": approval.approver if approval else None},
            "canary": {
                "passed": canary is not None and canary.status == "passed",
                "status": canary.status if canary else "missing",
                "trades": canary.trades if canary else 0,
            },
            "health": {
                "passed": canary is not None and canary.reconciliation_ok and canary.alerts_ok and canary.risk_breaches == 0,
                "reconciliation_ok": canary.reconciliation_ok if canary else False,
                "alerts_ok": canary.alerts_ok if canary else False,
            },
            "failed_reasons": failed,
            "passed": not failed,
        }

    # ------------------------------------------------------------ promotion
    def promote(self, proposal_id: str, *, approver: str) -> ResearchPromotion:
        proposal = self._require_proposal(proposal_id)
        gates = self.gates(proposal_id, include_canary=True)
        if not gates["passed"]:
            raise GovernanceError("promotion gates failed: " + "; ".join(gates["failed_reasons"]))
        if not str(approver or "").strip():
            raise GovernanceError("promoter identity is required")

        with self.trading_session_factory() as db:
            active = db.execute(
                select(StrategyVersion).where(
                    StrategyVersion.strategy_id == proposal.strategy_id,
                    StrategyVersion.active.is_(True),
                )
            ).scalar_one_or_none()
            previous = active.version if active is not None else ""
            for row in db.execute(
                select(StrategyVersion).where(StrategyVersion.strategy_id == proposal.strategy_id)
            ).scalars():
                row.active = False
            version = db.execute(
                select(StrategyVersion).where(
                    StrategyVersion.strategy_id == proposal.strategy_id,
                    StrategyVersion.version == proposal.strategy_version,
                )
            ).scalar_one_or_none()
            if version is None:
                version = StrategyVersion(
                    strategy_id=proposal.strategy_id,
                    version=proposal.strategy_version,
                    status="VALIDATED",
                    artifact_path=proposal.artifact_path,
                    validation_run_id=str(_loads(proposal.metrics_json).get("reproducibility_hash", "")),
                    dataset_hash=proposal.dataset_hash,
                    params_json=proposal.params_json,
                    active=True,
                    promoted_at=_now(),
                )
                db.add(version)
            else:
                version.active = True
                version.status = "VALIDATED"
                version.promoted_at = _now()
            db.commit()

        with self.store.SessionLocal() as db:
            proposal_row = db.execute(
                select(ResearchProposal).where(ResearchProposal.proposal_id == proposal_id)
            ).scalar_one()
            proposal_row.status = "promoted"
            proposal_row.updated_at = _now()
            promotion = ResearchPromotion(
                proposal_id=proposal_id,
                strategy_id=proposal.strategy_id,
                strategy_version=proposal.strategy_version,
                previous_version=previous,
                promoted_by=str(approver),
                status="promoted",
            )
            db.add(promotion)
            db.commit()
            db.refresh(promotion)
            return promotion

    def rollback(self, strategy_id: str, *, actor: str, reason: str) -> ResearchPromotion:
        with self.store.SessionLocal() as research:
            promotion = research.execute(
                select(ResearchPromotion)
                .where(
                    ResearchPromotion.strategy_id == strategy_id,
                    ResearchPromotion.status == "promoted",
                )
                .order_by(ResearchPromotion.id.desc())
                .limit(1)
            ).scalar_one_or_none()
            if promotion is None:
                raise GovernanceError(f"no active promotion for {strategy_id}")
            promotion.status = "rolled_back"
            promotion.rolled_back_at = _now()
            promotion.rollback_reason = str(reason or "")
            previous = promotion.previous_version
            proposal = research.execute(
                select(ResearchProposal).where(ResearchProposal.proposal_id == promotion.proposal_id)
            ).scalar_one_or_none()
            if proposal is not None:
                proposal.status = "rolled_back"
                proposal.updated_at = _now()
            research.commit()
            research.refresh(promotion)

        with self.trading_session_factory() as db:
            for row in db.execute(
                select(StrategyVersion).where(StrategyVersion.strategy_id == strategy_id)
            ).scalars():
                row.active = False
            if previous:
                target = db.execute(
                    select(StrategyVersion).where(
                        StrategyVersion.strategy_id == strategy_id,
                        StrategyVersion.version == previous,
                    )
                ).scalar_one_or_none()
                if target is not None:
                    target.active = True
                    target.promoted_at = _now()
            db.commit()
        return promotion

    def _require_proposal(self, proposal_id: str) -> ResearchProposal:
        proposal = self.get_proposal(proposal_id)
        if proposal is None:
            raise GovernanceError(f"proposal {proposal_id} not found")
        return proposal


__all__ = ["GovernanceError", "GovernanceService"]
