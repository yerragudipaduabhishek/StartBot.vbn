"""Derive strategy validation status from stored walk-forward artifacts.

A strategy is never marked validated because source code says so. Validation
requires a complete walk-forward manifest containing a dataset hash, at least
one leak-free fold, and untouched out-of-sample metrics.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

UNVALIDATED = "UNVALIDATED"
VALIDATED = "VALIDATED"


@dataclass(frozen=True)
class ValidationStatus:
    status: str
    reason: str
    artifact_path: str | None = None
    run_id: str | None = None
    dataset_hash: str | None = None
    folds: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "artifact_path": self.artifact_path,
            "run_id": self.run_id,
            "dataset_hash": self.dataset_hash,
            "folds": self.folds,
        }


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _candidate_paths(strategy_id: str, artifacts_dir: str | Path | None) -> list[Path]:
    explicit = os.environ.get("STRATEGY_VALIDATION_ARTIFACT", "").strip()
    paths: list[Path] = []
    if explicit:
        path = Path(explicit)
        paths.append(path / "manifest.json" if path.is_dir() else path)
    root = Path(artifacts_dir) if artifacts_dir is not None else Path(
        os.environ.get("ARTIFACTS_DIR", str(_project_root() / "artifacts"))
    )
    for base in (root, root / strategy_id):
        if not base.is_dir():
            continue
        paths.extend(sorted(base.glob("*/manifest.json"), reverse=True))
        paths.extend(sorted(base.glob("**/manifest.json"), reverse=True))
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def _validate_manifest(payload: Any, strategy_id: str) -> ValidationStatus:
    if not isinstance(payload, dict):
        return ValidationStatus(UNVALIDATED, "validation manifest is not an object")
    config = payload.get("config")
    folds = payload.get("folds")
    aggregate = payload.get("aggregate")
    dataset_hash = str(payload.get("dataset_hash") or "").strip()
    run_id = str(payload.get("reproducibility_hash") or "").strip()
    if not isinstance(config, dict) or not isinstance(folds, list) or not isinstance(aggregate, dict):
        return ValidationStatus(UNVALIDATED, "validation manifest is incomplete")
    declared_strategy = str(config.get("strategy_id") or "").strip()
    if declared_strategy and declared_strategy != strategy_id:
        return ValidationStatus(UNVALIDATED, f"artifact belongs to {declared_strategy}")
    if not dataset_hash:
        return ValidationStatus(UNVALIDATED, "validation artifact has no dataset hash")
    if not run_id or len(run_id) != 64:
        return ValidationStatus(UNVALIDATED, "validation artifact has no reproducibility hash")
    if not folds:
        return ValidationStatus(UNVALIDATED, "validation artifact contains no folds")
    accepted = 0
    for fold in folds:
        if not isinstance(fold, dict):
            return ValidationStatus(UNVALIDATED, "malformed fold entry")
        try:
            train_end = int(fold["train_end"])
            test_start = int(fold["test_start"])
            embargo = int(fold.get("embargo", 0) or 0)
        except (KeyError, TypeError, ValueError):
            return ValidationStatus(UNVALIDATED, "fold boundaries are malformed")
        if test_start < train_end + embargo:
            return ValidationStatus(UNVALIDATED, "fold test window overlaps training data")
        if fold.get("accepted") and fold.get("test_metrics"):
            accepted += 1
    if accepted <= 0:
        return ValidationStatus(UNVALIDATED, "no fold produced out-of-sample metrics")
    if int(aggregate.get("accepted_folds", accepted) or 0) <= 0:
        return ValidationStatus(UNVALIDATED, "aggregate reports no accepted folds")
    return ValidationStatus(
        VALIDATED,
        "walk-forward artifact verified",
        run_id=run_id,
        dataset_hash=dataset_hash,
        folds=accepted,
    )


def validation_status(
    strategy_id: str,
    *,
    artifacts_dir: str | Path | None = None,
) -> ValidationStatus:
    """Return the status derived from the newest usable artifact."""
    for path in _candidate_paths(strategy_id, artifacts_dir):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        status = _validate_manifest(payload, strategy_id)
        if status.status == VALIDATED:
            return ValidationStatus(
                status.status,
                status.reason,
                artifact_path=str(path),
                run_id=status.run_id,
                dataset_hash=status.dataset_hash,
                folds=status.folds,
            )
    return ValidationStatus(UNVALIDATED, "no verified walk-forward artifact found")


__all__ = ["UNVALIDATED", "VALIDATED", "ValidationStatus", "validation_status"]
