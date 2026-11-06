"""Phase 7A: local paper-trading soak test (real data, paper execution only)."""

from app.soak.feed import RESOLUTION_SECS, SoakFeed  # noqa: F401
from app.soak.runner import (  # noqa: F401
    BANNER,
    DEFAULT_EMAS,
    DEFAULT_WEIGHTS,
    TELEMETRY_FIELDS,
    SoakRunner,
    build_paper_engine,
)
from app.soak.telemetry import SoakTelemetry  # noqa: F401

__all__ = [
    "BANNER",
    "DEFAULT_EMAS",
    "DEFAULT_WEIGHTS",
    "TELEMETRY_FIELDS",
    "RESOLUTION_SECS",
    "SoakFeed",
    "SoakRunner",
    "SoakTelemetry",
    "build_paper_engine",
]