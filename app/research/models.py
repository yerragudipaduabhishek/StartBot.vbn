"""Research-only persistence models.

These tables live in a dedicated research database, never in the trading
database. They hold datasets, experiment configurations and results, but no
account, order, position, risk or execution state.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class ResearchBase(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ResearchDataset(ResearchBase):
    __tablename__ = "research_datasets"
    __table_args__ = (UniqueConstraint("dataset_hash", name="uq_research_dataset_hash"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset_hash: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(32))
    timeframe: Mapped[str] = mapped_column(String(16))
    rows: Mapped[int] = mapped_column(Integer)
    start_time: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_time: Mapped[int | None] = mapped_column(Integer, nullable=True)
    first_candle_time: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_candle_time: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="unknown")
    venue: Mapped[str] = mapped_column(String(64), default="unknown")
    transport: Mapped[str] = mapped_column(String(16), default="unknown")
    fallback_used: Mapped[bool] = mapped_column(default=False)
    closed_only: Mapped[bool] = mapped_column(default=True)
    provenance_json: Mapped[str] = mapped_column(Text, default="{}")
    quality_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ResearchExperiment(ResearchBase):
    __tablename__ = "research_experiments"
    __table_args__ = (UniqueConstraint("experiment_id", name="uq_research_experiment_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    experiment_id: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    strategy_id: Mapped[str] = mapped_column(String(64))
    strategy_version: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(32))
    timeframe: Mapped[str] = mapped_column(String(16))
    dataset_hash: Mapped[str] = mapped_column(String(64), index=True)
    dataset_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dataset_end: Mapped[int | None] = mapped_column(Integer, nullable=True)

    params_json: Mapped[str] = mapped_column(Text, default="{}")
    assumptions_json: Mapped[str] = mapped_column(Text, default="{}")
    metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    profile_json: Mapped[str] = mapped_column(Text, default="{}")

    fee_pct: Mapped[float] = mapped_column(Float, default=0.0)
    slippage_bps: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(24), default="completed")
    rejection_reason: Mapped[str] = mapped_column(Text, default="")
    feature_pipeline_version: Mapped[str] = mapped_column(String(32), default="")
    code_version: Mapped[str] = mapped_column(String(64), default="")


class ResearchNewsEvent(ResearchBase):
    """Auditable optional news context; never consulted by execution."""

    __tablename__ = "research_news_events"
    __table_args__ = (UniqueConstraint("headline_hash", name="uq_research_news_hash"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    headline_hash: Mapped[str] = mapped_column(String(64), index=True)
    headline: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(128), default="")
    url: Mapped[str] = mapped_column(Text, default="")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    provider: Mapped[str] = mapped_column(String(32), default="rss")
    symbols_json: Mapped[str] = mapped_column(Text, default="[]")


class ResearchProposal(ResearchBase):
    """Append-only self-improvement proposal with explicit lineage."""

    __tablename__ = "research_proposals"
    __table_args__ = (UniqueConstraint("proposal_id", name="uq_research_proposal_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    proposal_id: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    experiment_id: Mapped[str] = mapped_column(String(64), default="")
    strategy_id: Mapped[str] = mapped_column(String(64), index=True)
    strategy_version: Mapped[str] = mapped_column(String(32))
    dataset_hash: Mapped[str] = mapped_column(String(64), default="")
    artifact_path: Mapped[str] = mapped_column(Text, default="")
    params_json: Mapped[str] = mapped_column(Text, default="{}")
    metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(24), default="draft")
    rejection_reason: Mapped[str] = mapped_column(Text, default="")


class ResearchReview(ResearchBase):
    __tablename__ = "research_reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    proposal_id: Mapped[str] = mapped_column(String(64), index=True)
    reviewer: Mapped[str] = mapped_column(String(128), default="")
    veto: Mapped[bool] = mapped_column(default=False)
    approved: Mapped[bool] = mapped_column(default=False)
    findings_json: Mapped[str] = mapped_column(Text, default="[]")
    required_changes_json: Mapped[str] = mapped_column(Text, default="[]")


class ResearchApproval(ResearchBase):
    __tablename__ = "research_approvals"
    __table_args__ = (UniqueConstraint("proposal_id", name="uq_research_approval_proposal"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    proposal_id: Mapped[str] = mapped_column(String(64), index=True)
    approver: Mapped[str] = mapped_column(String(128))
    rationale: Mapped[str] = mapped_column(Text, default="")


class ResearchCanary(ResearchBase):
    __tablename__ = "research_canaries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    proposal_id: Mapped[str] = mapped_column(String(64), index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0)
    min_trades: Mapped[int] = mapped_column(Integer, default=0)
    trades: Mapped[int] = mapped_column(Integer, default=0)
    risk_breaches: Mapped[int] = mapped_column(Integer, default=0)
    reconciliation_ok: Mapped[bool] = mapped_column(default=False)
    alerts_ok: Mapped[bool] = mapped_column(default=False)
    status: Mapped[str] = mapped_column(String(24), default="pending")


class ResearchPromotion(ResearchBase):
    __tablename__ = "research_promotions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    proposal_id: Mapped[str] = mapped_column(String(64), index=True)
    strategy_id: Mapped[str] = mapped_column(String(64), index=True)
    strategy_version: Mapped[str] = mapped_column(String(32))
    previous_version: Mapped[str] = mapped_column(String(32), default="")
    promoted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    promoted_by: Mapped[str] = mapped_column(String(128), default="")
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rollback_reason: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(24), default="promoted")


class ResearchWalkForwardRun(ResearchBase):
    """One reproducible walk-forward run with every fold and trial retained."""

    __tablename__ = "research_walk_forward_runs"
    __table_args__ = (UniqueConstraint("run_id", name="uq_research_walk_forward_run"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    experiment_id: Mapped[str] = mapped_column(String(64), default="")
    strategy_id: Mapped[str] = mapped_column(String(64), default="")
    dataset_hash: Mapped[str] = mapped_column(String(64), default="")
    artifact_path: Mapped[str] = mapped_column(Text, default="")
    command: Mapped[str] = mapped_column(Text, default="")
    code_version: Mapped[str] = mapped_column(String(64), default="")
    config_json: Mapped[str] = mapped_column(Text, default="{}")
    aggregate_json: Mapped[str] = mapped_column(Text, default="{}")


class ResearchWalkForwardFold(ResearchBase):
    __tablename__ = "research_walk_forward_folds"
    __table_args__ = (UniqueConstraint("run_id", "fold_index", name="uq_research_walk_forward_fold"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    fold_index: Mapped[int] = mapped_column(Integer)
    train_start: Mapped[int] = mapped_column(Integer, default=0)
    train_end: Mapped[int] = mapped_column(Integer, default=0)
    test_start: Mapped[int] = mapped_column(Integer, default=0)
    test_end: Mapped[int] = mapped_column(Integer, default=0)
    warmup_start: Mapped[int] = mapped_column(Integer, default=0)
    embargo: Mapped[int] = mapped_column(Integer, default=0)
    params_json: Mapped[str] = mapped_column(Text, default="{}")
    train_metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    test_metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    accepted: Mapped[bool] = mapped_column(default=False)
    reason: Mapped[str] = mapped_column(Text, default="")
    trials: Mapped[int] = mapped_column(Integer, default=0)


class ResearchWalkForwardTrial(ResearchBase):
    __tablename__ = "research_walk_forward_trials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    fold_index: Mapped[int] = mapped_column(Integer, index=True)
    trial_index: Mapped[int] = mapped_column(Integer, default=0)
    params_hash: Mapped[str] = mapped_column(String(64), default="")
    params_json: Mapped[str] = mapped_column(Text, default="{}")
    metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    score: Mapped[float] = mapped_column(Float, default=0.0)
    accepted: Mapped[bool] = mapped_column(default=False)
    reason: Mapped[str] = mapped_column(Text, default="")


__all__ = [
    "ResearchApproval",
    "ResearchBase",
    "ResearchCanary",
    "ResearchDataset",
    "ResearchExperiment",
    "ResearchNewsEvent",
    "ResearchPromotion",
    "ResearchProposal",
    "ResearchReview",
    "ResearchWalkForwardFold",
    "ResearchWalkForwardRun",
    "ResearchWalkForwardTrial",
]
