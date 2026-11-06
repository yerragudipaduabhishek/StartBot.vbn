"""Strategy compatibility metadata.

Strategy A stays locked to BTCUSDT / 1H / validated. A strategy may declare the
markets, asset types, timeframes, symbols and required data it is compatible
with. Compatibility checks are read-only descriptors — nothing here executes a
strategy or extends Strategy A to other instruments.
"""

from __future__ import annotations

from app.analysis.strategies import DEFAULT_STRATEGY_ID, STRATEGY_A
from app.instruments.model import Instrument

TIME_FRAME_1H = "1H"

# Locked strategy-a compatibility scope (mirrors the validated study). Do not
# widen these sets - strategy A is validated for BTCUSDT / 1H only.
_COMPAT: dict[str, dict] = {
    DEFAULT_STRATEGY_ID: {
        "name": STRATEGY_A["name"],
        "markets": {"crypto"},
        "asset_types": {"perpetual"},
        "timeframes": {TIME_FRAME_1H},
        "symbols": {"BTCUSDT"},
        "required_data": ("ticker", "candles"),
    }
}


class StrategyCompatibility:
    """Declares and validates what instruments a strategy may analyse."""

    def requirements(self, strategy_id: str) -> dict:
        if strategy_id not in _COMPAT:
            raise ValueError(f"unknown strategy: {strategy_id}")
        info = _COMPAT[strategy_id]
        return {
            "strategy_id": strategy_id,
            "name": info["name"],
            "markets": sorted(info["markets"]),
            "asset_types": sorted(info["asset_types"]),
            "timeframes": sorted(info["timeframes"]),
            "symbols": sorted(info["symbols"]),
            "required_data": list(info["required_data"]),
        }

    def check(self, strategy_id: str, instrument: Instrument) -> dict:
        if strategy_id not in _COMPAT:
            raise ValueError(f"unknown strategy: {strategy_id}")
        info = _COMPAT[strategy_id]
        reasons: list[str] = []
        if instrument.symbol not in info["symbols"]:
            reasons.append(f"symbol {instrument.symbol} not in {sorted(info['symbols'])}")
        if instrument.market not in info["markets"]:
            reasons.append(f"market {instrument.market} not supported")
        if instrument.asset_type not in info["asset_types"]:
            reasons.append(f"asset_type {instrument.asset_type} not supported")
        missing = [c for c in info["required_data"] if c not in instrument.capabilities]
        if missing:
            reasons.append(f"missing required data: {missing}")
        return {"strategy_id": strategy_id, "instrument_id": instrument.id, "compatible": not reasons, "reasons": reasons}

    def is_compatible(self, strategy_id: str, instrument: Instrument) -> bool:
        return self.check(strategy_id, instrument)["compatible"]