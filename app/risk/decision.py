"""Backwards-compatible import surface for the shared risk decision contract."""

from app.contracts import RiskApprovalStatus, RiskDecision

__all__ = ["RiskApprovalStatus", "RiskDecision"]
