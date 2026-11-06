"""Locked Strategy A constants.

This module is the single source of truth for the validated 1H strategy. The
backtest study, dashboard registry, paper runner and testnet validation loop
all read these values instead of carrying independent copies.
"""

from __future__ import annotations

STRATEGY_A_ID = "strategy_a"
STRATEGY_A_NAME = "Strategy A"
STRATEGY_A_VERSION = "1.0.0"
STRATEGY_A_SYMBOL = "BTCUSDT"
STRATEGY_A_TIMEFRAME = "1h"
STRATEGY_A_CONTRACT_VALUE = 0.001

# best combo from the 1h in-sample scan, validated on held-out data
STRATEGY_A_PARAMS: dict[str, float | int] = {
    "ema_fast": 8,
    "ema_slow": 40,
    "ema_mid": 50,
    "threshold": 0.25,
    "min_confidence": 0.5,
    "sl_atr": 1.0,
    "target_atr": 2.0,
    "trend_w": 0.4,
    "structure_w": 0.0,
}

STRATEGY_A_WEIGHTS: dict[str, float] = {
    "trend": float(STRATEGY_A_PARAMS["trend_w"]),
    "structure": float(STRATEGY_A_PARAMS["structure_w"]),
    "sentiment": 0.15,
    "derivatives": 0.15,
}

STRATEGY_A_EMAS: tuple[int, int, int] = (
    int(STRATEGY_A_PARAMS["ema_fast"]),
    int(STRATEGY_A_PARAMS["ema_slow"]),
    int(STRATEGY_A_PARAMS["ema_mid"]),
)
