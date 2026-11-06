"""Isolated research storage for datasets and reproducible experiments."""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config.settings import get_settings
from app.data.dataset import DatasetMetadata
from app.data.news import NewsItem
from app.research.models import (
    ResearchBase,
    ResearchDataset,
    ResearchExperiment,
    ResearchNewsEvent,
    ResearchWalkForwardFold,
    ResearchWalkForwardRun,
    ResearchWalkForwardTrial,
)


class ResearchStorageError(RuntimeError):
    """Research data could not be stored or validated."""


def _ensure_sqlite_dir(url: str) -> None:
    if url.startswith("sqlite:///") and not url.startswith("sqlite:///:memory:"):
        path = url.replace("sqlite:///", "", 1)
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)


def _json_safe(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _json(payload: Any) -> str:
    return json.dumps(_json_safe(payload), sort_keys=True, default=str, allow_nan=False)


class ResearchStore:
    """Dedicated research database, never the trading database."""

    def __init__(
        self,
        database_url: str | None = None,
        *,
        session_factory=None,
        create: bool = True,
    ) -> None:
        settings = get_settings()
        self.database_url = database_url or settings.research_database_url
        if session_factory is not None:
            self.engine = None
            self.SessionLocal = session_factory
        else:
            _ensure_sqlite_dir(self.database_url)
            kwargs: dict[str, Any] = {"echo": False}
            if self.database_url.startswith("sqlite"):
                kwargs["connect_args"] = {"check_same_thread": False}
                if ":memory:" in self.database_url:
                    kwargs["poolclass"] = StaticPool
            self.engine = create_engine(self.database_url, **kwargs)
            self.SessionLocal = sessionmaker(bind=self.engine, autoflush=False, autocommit=False, expire_on_commit=False)
        if create:
            self.init_db()

    def init_db(self) -> None:
        if self.engine is not None:
            bind = self.engine
        else:
            with self.SessionLocal() as db:
                bind = db.get_bind()
        ResearchBase.metadata.create_all(bind=bind)
        self._migrate_news_hash(bind)

    @staticmethod
    def _migrate_news_hash(bind) -> None:
        """Upgrade a pre-existing research DB without discarding headlines."""
        if bind.dialect.name != "sqlite":
            return
        inspector = inspect(bind)
        if "research_news_events" not in inspector.get_table_names():
            return
        columns = {column["name"] for column in inspector.get_columns("research_news_events")}
        with bind.begin() as conn:
            if "headline_hash" not in columns:
                conn.execute(text("ALTER TABLE research_news_events ADD COLUMN headline_hash VARCHAR(64)"))
                rows = conn.execute(
                    text("SELECT id, source, headline, url FROM research_news_events ORDER BY id")
                ).fetchall()
                seen: set[str] = set()
                for row_id, source, headline, url in rows:
                    identity = "|".join(
                        (
                            str(source or "").strip().lower(),
                            " ".join(str(headline or "").split()).lower(),
                            str(url or "").strip(),
                        )
                    )
                    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
                    if digest in seen:
                        conn.execute(
                            text("DELETE FROM research_news_events WHERE id = :id"),
                            {"id": row_id},
                        )
                        continue
                    seen.add(digest)
                    conn.execute(
                        text("UPDATE research_news_events SET headline_hash = :hash WHERE id = :id"),
                        {"hash": digest, "id": row_id},
                    )
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_research_news_hash "
                    "ON research_news_events (headline_hash)"
                )
            )

    # --------------------------------------------------------------- datasets

    def record_dataset(self, metadata: DatasetMetadata | dict) -> ResearchDataset:
        payload = metadata.as_dict() if isinstance(metadata, DatasetMetadata) else dict(metadata)
        with self.SessionLocal() as db:
            existing = db.execute(
                select(ResearchDataset).where(ResearchDataset.dataset_hash == payload["dataset_hash"])
            ).scalar_one_or_none()
            if existing is not None:
                return existing
            row = ResearchDataset(
                dataset_hash=str(payload["dataset_hash"]),
                symbol=str(payload.get("symbol") or ""),
                timeframe=str(payload.get("timeframe") or ""),
                rows=int(payload.get("rows") or 0),
                start_time=payload.get("start"),
                end_time=payload.get("end"),
                first_candle_time=payload.get("first_candle_time"),
                last_candle_time=payload.get("last_candle_time"),
                source=str(payload.get("source") or "unknown"),
                venue=str(payload.get("venue") or "unknown"),
                transport=str(payload.get("transport") or "unknown"),
                fallback_used=bool(payload.get("fallback_used")),
                closed_only=bool(payload.get("closed_only", True)),
                provenance_json=_json(
                    {
                        key: payload.get(key)
                        for key in (
                            "endpoint",
                            "fetched_at",
                            "page_count",
                            "request_count",
                            "pagination_complete",
                            "schema_version",
                        )
                    }
                ),
                quality_json=_json(payload.get("quality") or {}),
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return row

    def get_dataset(self, dataset_hash: str) -> ResearchDataset | None:
        with self.SessionLocal() as db:
            return db.execute(
                select(ResearchDataset).where(ResearchDataset.dataset_hash == dataset_hash)
            ).scalar_one_or_none()

    # ----------------------------------------------------------- experiments

    def record_experiment(
        self,
        *,
        strategy_id: str,
        strategy_version: str,
        symbol: str,
        timeframe: str,
        dataset_hash: str,
        params: dict,
        metrics: dict,
        profile: dict | None = None,
        assumptions: dict | None = None,
        dataset_start: int | None = None,
        dataset_end: int | None = None,
        fee_pct: float = 0.0,
        slippage_bps: float = 0.0,
        status: str = "completed",
        rejection_reason: str = "",
        feature_pipeline_version: str = "",
        code_version: str = "",
        experiment_id: str | None = None,
    ) -> ResearchExperiment:
        if not dataset_hash:
            raise ResearchStorageError("an experiment requires a dataset hash")
        if not self.get_dataset(dataset_hash):
            raise ResearchStorageError("record the dataset before its experiment")
        if status == "validated":
            raise ResearchStorageError("use validate_experiment() instead of recording as validated")
        experiment_id = experiment_id or uuid.uuid4().hex
        with self.SessionLocal() as db:
            existing = db.execute(
                select(ResearchExperiment).where(ResearchExperiment.experiment_id == experiment_id)
            ).scalar_one_or_none()
            if existing is not None:
                raise ResearchStorageError(f"experiment {experiment_id} already exists")
            row = ResearchExperiment(
                experiment_id=experiment_id,
                strategy_id=strategy_id,
                strategy_version=strategy_version,
                symbol=symbol.upper(),
                timeframe=timeframe,
                dataset_hash=dataset_hash,
                dataset_start=dataset_start,
                dataset_end=dataset_end,
                params_json=_json(params),
                assumptions_json=_json(assumptions or {}),
                metrics_json=_json(metrics),
                profile_json=_json(profile or {}),
                fee_pct=float(fee_pct),
                slippage_bps=float(slippage_bps),
                status=status,
                rejection_reason=rejection_reason,
                feature_pipeline_version=feature_pipeline_version,
                code_version=code_version,
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return row

    def get_experiment(self, experiment_id: str) -> ResearchExperiment | None:
        with self.SessionLocal() as db:
            return db.execute(
                select(ResearchExperiment).where(ResearchExperiment.experiment_id == experiment_id)
            ).scalar_one_or_none()

    def list_experiments(self, limit: int = 100) -> list[ResearchExperiment]:
        with self.SessionLocal() as db:
            return list(
                db.execute(
                    select(ResearchExperiment).order_by(ResearchExperiment.created_at.desc()).limit(max(1, int(limit)))
                ).scalars()
            )

    def validate_experiment(
        self,
        experiment_id: str,
        *,
        reproduce: Callable[[ResearchExperiment, ResearchDataset], bool] | None = None,
    ) -> ResearchExperiment:
        """Mark an experiment validated only against its recorded dataset.

        If a ``reproduce`` callback is supplied it must return True; otherwise
        the caller is asserting the stored metrics were produced by rerunning the
        recorded configuration against the recorded dataset hash.
        """
        with self.SessionLocal() as db:
            experiment = db.execute(
                select(ResearchExperiment).where(ResearchExperiment.experiment_id == experiment_id)
            ).scalar_one_or_none()
            if experiment is None:
                raise ResearchStorageError(f"experiment {experiment_id} not found")
            if experiment.status == "rejected":
                raise ResearchStorageError(f"experiment {experiment_id} was rejected")
            dataset = db.execute(
                select(ResearchDataset).where(ResearchDataset.dataset_hash == experiment.dataset_hash)
            ).scalar_one_or_none()
            if dataset is None or dataset.rows <= 0:
                raise ResearchStorageError("experiment cannot be validated without its source dataset")
            if reproduce is not None and not bool(reproduce(experiment, dataset)):
                raise ResearchStorageError("experiment did not reproduce against the recorded dataset")
            experiment.status = "validated"
            experiment.validated_at = datetime.now(timezone.utc)
            db.commit()
            db.refresh(experiment)
            return experiment

    def reject_experiment(self, experiment_id: str, reason: str) -> ResearchExperiment:
        with self.SessionLocal() as db:
            experiment = db.execute(
                select(ResearchExperiment).where(ResearchExperiment.experiment_id == experiment_id)
            ).scalar_one_or_none()
            if experiment is None:
                raise ResearchStorageError(f"experiment {experiment_id} not found")
            experiment.status = "rejected"
            experiment.rejection_reason = str(reason or "rejected")
            db.commit()
            db.refresh(experiment)
            return experiment

    # ------------------------------------------------------------------ news

    def record_news(self, item: NewsItem) -> ResearchNewsEvent:
        """Persist a headline once; replaying the same item is a no-op."""
        headline_hash = item.content_hash
        with self.SessionLocal() as db:
            existing = db.execute(
                select(ResearchNewsEvent).where(ResearchNewsEvent.headline_hash == headline_hash)
            ).scalar_one_or_none()
            if existing is not None:
                return existing
            row = ResearchNewsEvent(
                headline_hash=headline_hash,
                headline=item.headline,
                source=item.source,
                url=item.url,
                published_at=item.published_at,
                fetched_at=item.fetched_at,
                provider=item.provider,
                symbols_json=_json(list(item.symbols)),
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return row

    # -------------------------------------------------------- walk-forward
    def record_walk_forward(
        self,
        result,
        *,
        experiment_id: str = "",
        strategy_id: str = "",
    ) -> ResearchWalkForwardRun:
        """Persist a deterministic walk-forward run, its folds and every trial.

        The run id is the reproducibility hash, so replaying the same run is an
        idempotent no-op rather than a duplicate row.
        """
        run_id = str(result.reproducibility_hash)
        if experiment_id and self.get_experiment(experiment_id) is None:
            raise ResearchStorageError(f"experiment {experiment_id} is not recorded")
        with self.SessionLocal() as db:
            existing = db.execute(
                select(ResearchWalkForwardRun).where(ResearchWalkForwardRun.run_id == run_id)
            ).scalar_one_or_none()
            if existing is not None:
                return existing
            row = ResearchWalkForwardRun(
                run_id=run_id,
                experiment_id=str(experiment_id or ""),
                strategy_id=str(strategy_id or result.config.get("strategy_id") or ""),
                dataset_hash=str(result.dataset_hash or ""),
                artifact_path=str(result.artifact_path or ""),
                command=str(result.command or ""),
                code_version=str(result.code_version or ""),
                config_json=_json(result.config),
                aggregate_json=_json(result.aggregate()),
            )
            db.add(row)
            for fold in result.folds:
                span = fold.span
                db.add(
                    ResearchWalkForwardFold(
                        run_id=run_id,
                        fold_index=int(span.index),
                        train_start=int(span.train_start),
                        train_end=int(span.train_end),
                        test_start=int(span.test_start),
                        test_end=int(span.test_end),
                        warmup_start=int(span.warmup_start),
                        embargo=int(span.embargo),
                        params_json=_json(fold.params or {}),
                        train_metrics_json=_json(fold.train_metrics or {}),
                        test_metrics_json=_json(fold.test_metrics or {}),
                        accepted=bool(fold.accepted),
                        reason=str(fold.reason or ""),
                        trials=int(fold.trials or 0),
                    )
                )
                optimization = getattr(fold, "optimization", None)
                for trial_index, trial in enumerate(getattr(optimization, "trials", []) or []):
                    db.add(
                        ResearchWalkForwardTrial(
                            run_id=run_id,
                            fold_index=int(span.index),
                            trial_index=int(trial_index),
                            params_hash=str(trial.params_hash or ""),
                            params_json=_json(trial.params or {}),
                            metrics_json=_json(trial.metrics or {}),
                            score=float(trial.score or 0.0),
                            accepted=bool(trial.accepted),
                            reason=str(trial.reason or ""),
                        )
                    )
            db.commit()
            db.refresh(row)
            return row

    def get_walk_forward_run(self, run_id: str) -> ResearchWalkForwardRun | None:
        with self.SessionLocal() as db:
            return db.execute(
                select(ResearchWalkForwardRun).where(ResearchWalkForwardRun.run_id == run_id)
            ).scalar_one_or_none()

    def list_walk_forward_runs(self, limit: int = 100) -> list[ResearchWalkForwardRun]:
        with self.SessionLocal() as db:
            return list(
                db.execute(
                    select(ResearchWalkForwardRun)
                    .order_by(ResearchWalkForwardRun.created_at.desc())
                    .limit(max(1, int(limit)))
                ).scalars()
            )


__all__ = ["ResearchStorageError", "ResearchStore"]
