"""Authoritative trading profile.

The profile is the only place that binds symbol, timeframe, strategy version,
engine parameters, cost assumptions, execution mode and the risk configuration
together. Runners, research, execution and persistence all consume this object.
"""

from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.config.settings import Settings, get_settings
from app.contracts import ExecutionMode
from app.risk.config import RiskSettings
from app.strategy.strategy_a import (
    STRATEGY_A_CONTRACT_VALUE,
    STRATEGY_A_EMAS,
    STRATEGY_A_ID,
    STRATEGY_A_NAME,
    STRATEGY_A_PARAMS,
    STRATEGY_A_SYMBOL,
    STRATEGY_A_TIMEFRAME,
    STRATEGY_A_VERSION,
    STRATEGY_A_WEIGHTS,
)
from app.strategy.timeframes import max_candle_age_seconds, normalize_timeframe

_MODE_ALIASES = {
    "dev": "paper",
    "development": "paper",
    "test": "paper",
    "staging": "paper",
    "simulation": "paper",
}


def normalize_execution_mode(value: ExecutionMode | str | None) -> ExecutionMode:
    if isinstance(value, ExecutionMode):
        return value
    text = str(value or "paper").strip().lower()
    text = _MODE_ALIASES.get(text, text)
    try:
        return ExecutionMode(text)
    except ValueError as exc:
        raise ValueError(f"unsupported execution mode: {value!r}") from exc


class TradingProfile(BaseModel):
    """Validated, immutable description of the one active trading strategy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile_id: str = "strategy_a_paper"
    name: str = STRATEGY_A_NAME
    mode: ExecutionMode = ExecutionMode.PAPER
    symbol: str
    timeframe: str
    strategy_id: str = STRATEGY_A_ID
    strategy_version: str = STRATEGY_A_VERSION
    ema_spans: tuple[int, int, int] = STRATEGY_A_EMAS
    decision_threshold: float = Field(default=float(STRATEGY_A_PARAMS["threshold"]), ge=0.0, le=1.0)
    weights: dict[str, float] = Field(default_factory=lambda: dict(STRATEGY_A_WEIGHTS))
    risk: RiskSettings = Field(default_factory=RiskSettings)
    contract_value: float = Field(default=STRATEGY_A_CONTRACT_VALUE, gt=0.0)
    paper_fee_pct: float = Field(default=0.05, ge=0.0)
    paper_slippage_pct: float = Field(default=0.02, ge=0.0)
    allow_restart_entry: bool = False

    @field_validator("mode", mode="before")
    @classmethod
    def _mode_valid(cls, value: object) -> ExecutionMode:
        return normalize_execution_mode(value)  # type: ignore[arg-type]

    @field_validator("symbol")
    @classmethod
    def _symbol_upper(cls, value: str) -> str:
        symbol = str(value or "").strip().upper()
        if not symbol:
            raise ValueError("symbol is required")
        return symbol

    @field_validator("timeframe")
    @classmethod
    def _timeframe_valid(cls, value: str) -> str:
        return normalize_timeframe(value)

    @field_validator("ema_spans")
    @classmethod
    def _ema_spans_valid(cls, value: tuple[int, int, int]) -> tuple[int, int, int]:
        spans = tuple(int(item) for item in value)
        if len(spans) != 3 or any(span <= 0 for span in spans):
            raise ValueError("ema_spans must contain three positive integers")
        if not spans[0] < spans[1] < spans[2]:
            raise ValueError("ema_spans must be strictly increasing")
        return spans

    @field_validator("weights")
    @classmethod
    def _weights_valid(cls, value: dict[str, float]) -> dict[str, float]:
        weights = {str(key): float(weight) for key, weight in dict(value).items()}
        if not weights:
            raise ValueError("weights are required")
        if any(not math.isfinite(weight) or weight < 0 for weight in weights.values()):
            raise ValueError("weights must be finite and non-negative")
        return weights

    @field_validator("strategy_id", "strategy_version")
    @classmethod
    def _strategy_identity(cls, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("strategy identity is required")
        return text

    @model_validator(mode="after")
    def _validate_strategy_binding(self) -> "TradingProfile":
        if self.mode == ExecutionMode.LIVE:
            raise ValueError("live mode is disabled; live order submission is never enabled")
        if self.strategy_id == STRATEGY_A_ID:
            if self.symbol != STRATEGY_A_SYMBOL:
                raise ValueError(f"strategy A is locked to {STRATEGY_A_SYMBOL}, got {self.symbol}")
            if self.timeframe != STRATEGY_A_TIMEFRAME:
                raise ValueError(f"strategy A is locked to {STRATEGY_A_TIMEFRAME}, got {self.timeframe}")
        if self.risk.risk_per_trade_pct <= 0:
            raise ValueError("risk_per_trade_pct must be positive")
        return self

    @property
    def execution_mode(self) -> ExecutionMode:
        return self.mode

    @property
    def fee_pct(self) -> float:
        return self.paper_fee_pct

    @property
    def slippage_pct(self) -> float:
        return self.paper_slippage_pct

    @property
    def ema_fast(self) -> int:
        return self.ema_spans[0]

    @property
    def ema_slow(self) -> int:
        return self.ema_spans[1]

    @property
    def ema_mid(self) -> int:
        return self.ema_spans[2]

    @property
    def max_candle_age_secs(self) -> int:
        return int(self.risk.max_candle_age_secs)

    @property
    def profile_key(self) -> str:
        return f"{self.strategy_id}:{self.strategy_version}:{self.symbol}:{self.timeframe}"

    @property
    def is_paper(self) -> bool:
        return self.mode == ExecutionMode.PAPER

    @property
    def is_testnet(self) -> bool:
        return self.mode == ExecutionMode.TESTNET

    @property
    def is_research(self) -> bool:
        return self.mode in (ExecutionMode.RESEARCH, ExecutionMode.BACKTEST)

    @classmethod
    def from_settings(
        cls,
        settings: Settings | None = None,
        *,
        mode: ExecutionMode | str = ExecutionMode.PAPER,
    ) -> "TradingProfile":
        return profile_from_settings(settings, mode=mode)

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "name": self.name,
            "mode": self.mode.value,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "ema_spans": list(self.ema_spans),
            "decision_threshold": self.decision_threshold,
            "weights": dict(self.weights),
            "risk_per_trade_pct": self.risk.risk_per_trade_pct,
            "max_leverage": self.risk.max_leverage,
            "contract_value": self.contract_value,
            "paper_fee_pct": self.paper_fee_pct,
            "paper_slippage_pct": self.paper_slippage_pct,
        }


def profile_from_settings(
    settings: Settings | None = None,
    *,
    mode: ExecutionMode | str = ExecutionMode.PAPER,
) -> TradingProfile:
    settings = settings or get_settings()
    execution_mode = normalize_execution_mode(mode)
    if execution_mode == ExecutionMode.LIVE:
        raise ValueError("live mode is disabled; live order submission is never enabled")
    if execution_mode == ExecutionMode.TESTNET and (settings.live_mode or not settings.testnet_mode):
        raise ValueError("testnet profile requires TESTNET_MODE=on and LIVE_MODE=off")
    timeframe = normalize_timeframe(settings.trading_timeframe)
    risk = RiskSettings(
        risk_per_trade_pct=settings.paper_risk_pct,
        min_confidence=float(STRATEGY_A_PARAMS["min_confidence"]),
        sl_atr_multiple=float(STRATEGY_A_PARAMS["sl_atr"]),
        target_atr_multiple=float(STRATEGY_A_PARAMS["target_atr"]),
        max_candle_age_secs=max_candle_age_seconds(timeframe, bars=2),
    )
    symbol = settings.trading_symbol if execution_mode == ExecutionMode.TESTNET else settings.paper_symbol
    return TradingProfile(
        profile_id=f"strategy_a_{execution_mode.value}",
        mode=execution_mode,
        symbol=symbol,
        timeframe=timeframe,
        risk=risk,
        contract_value=settings.paper_contract_value,
        paper_fee_pct=settings.paper_fee_pct,
        paper_slippage_pct=settings.paper_slippage_pct,
    )
