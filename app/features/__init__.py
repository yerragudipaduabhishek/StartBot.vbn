"""Shared causal feature pipeline used by signals, backtests and the soak runner."""

from app.features.pipeline import FEATURE_PIPELINE_VERSION, FeaturePipeline

__all__ = ["FEATURE_PIPELINE_VERSION", "FeaturePipeline"]
