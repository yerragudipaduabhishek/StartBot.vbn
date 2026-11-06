"""Read-only strategy registry for the dashboard.

The registry is a *descriptor layer*. Status is derived from a stored,
leak-free walk-forward artifact; source code cannot assert validation. The
registry never modifies or re-runs strategy/backtest logic.
"""

from __future__ import annotations

# The locked 1h strategy config (single source of truth in the study).
# Imported read-only; nothing here changes the strategy code or backtest logic.
from app.strategy.status import UNVALIDATED, validation_status
from app.strategy.strategy_a import (
    STRATEGY_A_EMAS,
    STRATEGY_A_ID,
    STRATEGY_A_NAME,
    STRATEGY_A_PARAMS,
    STRATEGY_A_TIMEFRAME,
    STRATEGY_A_WEIGHTS,
)

STRATEGY_STATUS_VALIDATED = "VALIDATED"

ENGINE_WEIGHTS = dict(STRATEGY_A_WEIGHTS)

_INITIAL_STATUS = validation_status(STRATEGY_A_ID)

STRATEGY_A = {
    "id": STRATEGY_A_ID,
    "name": STRATEGY_A_NAME,
    "status": _INITIAL_STATUS.status,
    "validation": _INITIAL_STATUS.as_dict(),
    "timeframe": STRATEGY_A_TIMEFRAME.upper(),
    "description": (
        "EMA-alignment trend strategy (fast/slow/mid EMA 8/40/50) with ATR-based "
        "stops and targets. Status is derived from stored out-of-sample validation."
    ),
    "parameters": {
        "ema_fast": STRATEGY_A_PARAMS["ema_fast"],
        "ema_slow": STRATEGY_A_PARAMS["ema_slow"],
        "ema_mid": STRATEGY_A_PARAMS["ema_mid"],
        "ema_spans": STRATEGY_A_EMAS,
        "threshold": STRATEGY_A_PARAMS["threshold"],
        "min_confidence": STRATEGY_A_PARAMS["min_confidence"],
        "sl_atr_multiple": STRATEGY_A_PARAMS["sl_atr"],
        "target_atr_multiple": STRATEGY_A_PARAMS["target_atr"],
        "weights": dict(ENGINE_WEIGHTS),
    },
}

DEFAULT_STRATEGY_ID = STRATEGY_A["id"]


def _sync_status() -> dict:
    current = validation_status(STRATEGY_A_ID)
    STRATEGY_A["status"] = current.status
    STRATEGY_A["validation"] = current.as_dict()
    return STRATEGY_A


def get_strategy(strategy_id: str) -> dict:
    if strategy_id == STRATEGY_A_ID:
        return _sync_status()
    raise ValueError(f"unknown strategy: {strategy_id}")


def list_strategies() -> list[dict]:
    return [_sync_status()]


def is_validated(strategy_id: str = STRATEGY_A_ID) -> bool:
    return validation_status(strategy_id).status == STRATEGY_STATUS_VALIDATED


__all__ = [
    "DEFAULT_STRATEGY_ID",
    "STRATEGY_A",
    "STRATEGY_STATUS_VALIDATED",
    "UNVALIDATED",
    "get_strategy",
    "is_validated",
    "list_strategies",
]