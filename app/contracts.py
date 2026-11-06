"""Typed contracts shared by strategy, risk, execution and persistence.

This module is deliberately dependency-free with respect to the rest of the
application. Every boundary consumes these models instead of free-form dicts,
so a signal, risk decision, order and fill always carry the same identity,
provenance and mode information.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


class _StringEnum(str, Enum):
    def __str__(self) -> str:
        return str(self.value)


class ExecutionMode(_StringEnum):
    RESEARCH = "research"
    BACKTEST = "backtest"
    PAPER = "paper"
    TESTNET = "testnet"
    LIVE = "live"


class SignalDirection(_StringEnum):
    LONG = "long"
    SHORT = "short"
    NONE = "none"


class OrderSide(_StringEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(_StringEnum):
    MARKET = "market"
    LIMIT = "limit"


class RiskApprovalStatus(_StringEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    NOT_EVALUATED = "not_evaluated"
    PENDING = "pending"


class DataStatus(_StringEnum):
    STREAMING = "streaming"
    STALE = "stale"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class OrderStatus(_StringEnum):
    ACCEPTED = "accepted"
    FILLED = "filled"
    OPEN = "open"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


TradingMode = ExecutionMode
SignalSide = SignalDirection
Side = OrderSide
Direction = SignalDirection
DataQualityStatus = DataStatus


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _finite(value: object) -> float:
    if isinstance(value, bool):
        raise ValueError("boolean is not a number")
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError("value must be numeric") from exc
    if not math.isfinite(number):
        raise ValueError("value must be finite")
    return number


def _positive(value: object) -> float:
    number = _finite(value)
    if number <= 0:
        raise ValueError("value must be positive")
    return number


def _as_utc(value: datetime | None) -> datetime:
    if value is None:
        return _utcnow()
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _normalize_symbol(value: str | None) -> str:
    symbol = str(value or "").strip().upper()
    if not symbol:
        raise ValueError("symbol is required")
    return symbol


def _normalize_timeframe(value: str | None) -> str:
    timeframe = str(value or "").strip().lower()
    if not timeframe:
        raise ValueError("timeframe is required")
    return timeframe


class DataQuality(BaseModel):
    """Provenance and freshness of the data that produced a decision."""

    model_config = ConfigDict(extra="allow", use_enum_values=True)

    status: DataStatus = DataStatus.UNKNOWN
    price_valid: bool = False
    source: str = "unknown"
    last_candle_age_secs: float | None = None
    symbol: str | None = None
    timeframe: str | None = None
    fallback_used: bool = False

    @field_validator("status", mode="before")
    @classmethod
    def _status_valid(cls, value: object) -> str:
        text = str(value or "unknown").strip().lower()
        allowed = {item.value for item in DataStatus}
        return text if text in allowed else DataStatus.UNKNOWN.value

    @field_validator("source")
    @classmethod
    def _source_valid(cls, value: str) -> str:
        source = str(value or "unknown").strip()
        return source or "unknown"

    @field_validator("last_candle_age_secs")
    @classmethod
    def _age_valid(cls, value: float | None) -> float | None:
        if value is None:
            return None
        age = _finite(value)
        if age < 0:
            raise ValueError("last_candle_age_secs must not be negative")
        return age

    @field_validator("symbol")
    @classmethod
    def _symbol_valid(cls, value: str | None) -> str | None:
        return _normalize_symbol(value) if value else None

    @field_validator("timeframe")
    @classmethod
    def _timeframe_valid(cls, value: str | None) -> str | None:
        return _normalize_timeframe(value) if value else None


class Candle(BaseModel):
    """One closed OHLCV bar."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    time: int = Field(validation_alias=AliasChoices("time", "timestamp"))
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @field_validator("time", mode="before")
    @classmethod
    def _time_valid(cls, value: object) -> int:
        if isinstance(value, bool):
            raise ValueError("time must be an integer")
        try:
            number = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ValueError("time must be an integer") from exc
        if number < 0:
            raise ValueError("time must not be negative")
        return number

    @field_validator("open", "high", "low", "close")
    @classmethod
    def _price_valid(cls, value: object) -> float:
        return _positive(value)

    @field_validator("volume")
    @classmethod
    def _volume_valid(cls, value: object) -> float:
        volume = _finite(value)
        if volume < 0:
            raise ValueError("volume must not be negative")
        return volume

    @model_validator(mode="after")
    def _ohlc_consistent(self) -> "Candle":
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high must be the highest price in the bar")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low must be the lowest price in the bar")
        return self

    @property
    def timestamp(self) -> int:
        return self.time


class MarketSnapshot(BaseModel):
    """Validated, time-stamped market data handed to a strategy."""

    model_config = ConfigDict(extra="forbid")

    symbol: str
    timeframe: str
    candles: list[Candle] = Field(default_factory=list)
    ticker: dict[str, Any] = Field(default_factory=dict)
    source: str = "unknown"
    generated_at: datetime = Field(default_factory=_utcnow)
    last_candle_age_secs: float | None = None
    data_status: DataStatus = DataStatus.UNKNOWN
    price_valid: bool = False
    quality: DataQuality = Field(default_factory=DataQuality)

    @field_validator("symbol")
    @classmethod
    def _symbol_valid(cls, value: str) -> str:
        return _normalize_symbol(value)

    @field_validator("timeframe")
    @classmethod
    def _timeframe_valid(cls, value: str) -> str:
        return _normalize_timeframe(value)

    @field_validator("generated_at")
    @classmethod
    def _generated_valid(cls, value: datetime | None) -> datetime:
        return _as_utc(value)

    @field_validator("last_candle_age_secs")
    @classmethod
    def _age_valid(cls, value: float | None) -> float | None:
        if value is None:
            return None
        age = _finite(value)
        if age < 0:
            raise ValueError("last_candle_age_secs must not be negative")
        return age

    @field_validator("candles")
    @classmethod
    def _candles_ascending(cls, value: list[Candle]) -> list[Candle]:
        times = [candle.time for candle in value]
        if times != sorted(times) or len(times) != len(set(times)):
            raise ValueError("candles must be unique and ascending by time")
        return value

    @property
    def last_candle(self) -> Candle | None:
        return self.candles[-1] if self.candles else None


class PositionSize(BaseModel):
    """Risk-approved order size and the arithmetic behind it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    quantities: float
    notional_usd: float
    margin_usd: float
    leverage: float
    explanation: str = ""
    symbol: str | None = None
    side: SignalDirection | None = None
    contract_value: float | None = None

    def __init__(
        self,
        quantities: float | None = None,
        notional_usd: float | None = None,
        margin_usd: float | None = None,
        leverage: float | None = None,
        explanation: str = "",
        **data: Any,
    ) -> None:
        if quantities is not None:
            data["quantities"] = quantities
        if notional_usd is not None:
            data["notional_usd"] = notional_usd
        if margin_usd is not None:
            data["margin_usd"] = margin_usd
        if leverage is not None:
            data["leverage"] = leverage
        if explanation:
            data["explanation"] = explanation
        super().__init__(**data)

    @field_validator("quantities", "notional_usd", "leverage")
    @classmethod
    def _positive_value(cls, value: object) -> float:
        return _positive(value)

    @field_validator("margin_usd")
    @classmethod
    def _margin_value(cls, value: object) -> float:
        margin = _finite(value)
        if margin < 0:
            raise ValueError("margin_usd must not be negative")
        return margin

    @field_validator("contract_value")
    @classmethod
    def _contract_value(cls, value: float | None) -> float | None:
        return _positive(value) if value is not None else None

    @property
    def quantity(self) -> float:
        return self.quantities

    def __repr__(self) -> str:
        return (
            f"PositionSize(quantities={self.quantities:.6f}, "
            f"notional=${self.notional_usd:,.2f}, margin=${self.margin_usd:,.2f}, "
            f"lev={self.leverage:.1f}x)"
        )


class RiskDecision(BaseModel):
    """The only token that authorises an order."""

    model_config = ConfigDict(frozen=True, extra="forbid", use_enum_values=True)

    allowed: bool = False
    size: PositionSize | None = None
    reason: str = ""
    checks: tuple[str, ...] = ()
    signal_key: str = ""
    approval_status: RiskApprovalStatus = RiskApprovalStatus.NOT_EVALUATED
    signal_id: str = ""
    symbol: str = ""
    timeframe: str = ""
    strategy_id: str = ""
    strategy_version: str = ""
    data_source: str = "unknown"
    confidence: float | None = None
    entry: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    execution_mode: ExecutionMode | None = None
    decided_at: datetime = Field(default_factory=_utcnow)

    def __init__(
        self,
        allowed: bool | None = None,
        size: PositionSize | dict | None = None,
        reason: str = "",
        checks: tuple[str, ...] | list[str] = (),
        signal_key: str = "",
        **data: Any,
    ) -> None:
        if allowed is not None:
            data["allowed"] = allowed
        if size is not None:
            data["size"] = size
        if reason:
            data["reason"] = reason
        if checks:
            data["checks"] = tuple(checks)
        if signal_key:
            data["signal_key"] = signal_key
        if "approval_status" not in data:
            effective_allowed = data.get("allowed", allowed)
            data["approval_status"] = (
                RiskApprovalStatus.APPROVED.value
                if effective_allowed
                else RiskApprovalStatus.REJECTED.value
            )
        super().__init__(**data)

    @field_validator("checks")
    @classmethod
    def _checks_tuple(cls, value: tuple[str, ...] | list[str]) -> tuple[str, ...]:
        return tuple(str(item) for item in value)

    @field_validator("confidence")
    @classmethod
    def _confidence_valid(cls, value: float | None) -> float | None:
        if value is None:
            return None
        confidence = _finite(value)
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        return confidence

    @field_validator("entry", "stop_loss", "take_profit")
    @classmethod
    def _price_valid(cls, value: float | None) -> float | None:
        return _positive(value) if value is not None else None

    @field_validator("decided_at")
    @classmethod
    def _decided_valid(cls, value: datetime | None) -> datetime:
        return _as_utc(value)

    @model_validator(mode="after")
    def _decision_consistent(self) -> "RiskDecision":
        if self.allowed and self.size is None:
            raise ValueError("an approved decision requires a position size")
        if not self.allowed and self.size is not None:
            raise ValueError("a rejected decision cannot carry a position size")
        status = str(self.approval_status)
        if self.allowed and status not in (RiskApprovalStatus.APPROVED.value, RiskApprovalStatus.PENDING.value):
            raise ValueError("allowed decision must have an approved/pending status")
        if not self.allowed and status == RiskApprovalStatus.APPROVED.value:
            raise ValueError("rejected decision cannot have an approved status")
        return self

    @property
    def approved(self) -> bool:
        return bool(self.allowed and self.approval_status == RiskApprovalStatus.APPROVED.value)

    def __bool__(self) -> bool:
        return bool(self.allowed)

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump()


class Signal(BaseModel):
    """A strategy decision; execution accepts nothing less structured."""

    model_config = ConfigDict(extra="allow", use_enum_values=True)

    strategy_id: str
    strategy_version: str
    symbol: str
    timeframe: str
    candle_time: int
    created_at: datetime = Field(default_factory=_utcnow)
    direction: SignalDirection
    score: float | None = None
    confidence: float | None = None
    entry: float | None = Field(
        default=None,
        validation_alias=AliasChoices("entry", "expected_price"),
    )
    stop_loss: float | None = None
    target: float | None = Field(
        default=None,
        validation_alias=AliasChoices("target", "take_profit"),
    )
    accepted_size: float | None = None
    risk_allowed: bool = False
    risk_reason: str = ""
    risk_approval_status: RiskApprovalStatus = RiskApprovalStatus.NOT_EVALUATED
    data_quality: DataQuality = Field(default_factory=DataQuality)
    data_source: str = "unknown"
    execution_mode: ExecutionMode | None = None

    @field_validator("strategy_id", "strategy_version")
    @classmethod
    def _nonempty(cls, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("value is required")
        return text

    @field_validator("symbol")
    @classmethod
    def _symbol_valid(cls, value: str) -> str:
        return _normalize_symbol(value)

    @field_validator("timeframe")
    @classmethod
    def _timeframe_valid(cls, value: str) -> str:
        return _normalize_timeframe(value)

    @field_validator("candle_time", mode="before")
    @classmethod
    def _candle_time_valid(cls, value: object) -> int:
        if isinstance(value, bool):
            raise ValueError("candle_time must be an integer")
        try:
            number = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ValueError("candle_time must be an integer") from exc
        if number < 0:
            raise ValueError("candle_time must not be negative")
        return number

    @field_validator("created_at")
    @classmethod
    def _created_valid(cls, value: datetime | None) -> datetime:
        return _as_utc(value)

    @field_validator("score", "confidence")
    @classmethod
    def _number_valid(cls, value: float | None) -> float | None:
        number = _finite(value) if value is not None else None
        if number is not None and not 0.0 <= number <= 1.0 and value is not None and value != number:
            return number
        return number

    @field_validator("entry", "stop_loss", "target", "accepted_size")
    @classmethod
    def _positive_optional(cls, value: float | None) -> float | None:
        return _positive(value) if value is not None else None

    @model_validator(mode="after")
    def _signal_consistent(self) -> "Signal":
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        if self.direction == SignalDirection.LONG.value and self.entry is not None and self.stop_loss is not None:
            if not self.stop_loss < self.entry:
                raise ValueError("long stop_loss must be below entry")
            if self.target is not None and not self.target > self.entry:
                raise ValueError("long target must be above entry")
        if self.direction == SignalDirection.SHORT.value and self.entry is not None and self.stop_loss is not None:
            if not self.stop_loss > self.entry:
                raise ValueError("short stop_loss must be above entry")
            if self.target is not None and not self.target < self.entry:
                raise ValueError("short target must be below entry")
        if self.risk_allowed:
            if self.risk_approval_status in (
                RiskApprovalStatus.NOT_EVALUATED.value,
                RiskApprovalStatus.PENDING.value,
            ):
                self.risk_approval_status = RiskApprovalStatus.APPROVED.value
        elif self.risk_approval_status == RiskApprovalStatus.NOT_EVALUATED.value:
            self.risk_approval_status = RiskApprovalStatus.REJECTED.value
        return self

    @property
    def key(self) -> str:
        return f"{self.strategy_id}:{self.symbol}:{self.timeframe}:{self.candle_time}"

    @property
    def take_profit(self) -> float | None:
        return self.target

    def as_dict(self) -> dict[str, Any]:
        data = self.model_dump()
        data["take_profit"] = self.target
        return data


class OrderRequest(BaseModel):
    """A risk-approved intent to trade on one venue."""

    model_config = ConfigDict(extra="allow", use_enum_values=True)

    client_order_id: str
    symbol: str
    timeframe: str
    strategy_id: str
    strategy_version: str
    side: OrderSide
    size: float
    expected_price: float = Field(
        validation_alias=AliasChoices("expected_price", "entry", "price")
    )
    order_type: OrderType = OrderType.MARKET
    stop_loss: float | None = None
    take_profit: float | None = Field(
        default=None,
        validation_alias=AliasChoices("take_profit", "target"),
    )
    product_id: int = 0
    execution_mode: ExecutionMode
    risk_decision_key: str
    risk_approved: bool = False
    confidence: float | None = None
    data_source: str = "unknown"
    candle_time: int = 0
    reduce_only: bool = False
    created_at: datetime = Field(default_factory=_utcnow)

    @field_validator("client_order_id", "risk_decision_key")
    @classmethod
    def _id_required(cls, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("idempotency key is required")
        return text

    @field_validator("symbol")
    @classmethod
    def _symbol_valid(cls, value: str) -> str:
        return _normalize_symbol(value)

    @field_validator("timeframe")
    @classmethod
    def _timeframe_valid(cls, value: str) -> str:
        return _normalize_timeframe(value)

    @field_validator("side", mode="before")
    @classmethod
    def _side_valid(cls, value: object) -> str:
        text = str(value or "").strip().lower()
        if text in ("long", "buy"):
            return OrderSide.BUY.value
        if text in ("short", "sell"):
            return OrderSide.SELL.value
        raise ValueError("side must be buy or sell")

    @field_validator("size")
    @classmethod
    def _size_valid(cls, value: object) -> float:
        return _positive(value)

    @field_validator("expected_price", "stop_loss", "take_profit")
    @classmethod
    def _price_valid(cls, value: float | None) -> float | None:
        return _positive(value) if value is not None else None

    @field_validator("order_type", mode="before")
    @classmethod
    def _order_type_valid(cls, value: object) -> str:
        text = str(value or "market").strip().lower()
        if text in ("market", "limit", "limit_order"):
            return "limit" if text == "limit_order" else text
        raise ValueError("unsupported order type")

    @field_validator("created_at")
    @classmethod
    def _created_valid(cls, value: datetime | None) -> datetime:
        return _as_utc(value)

    @model_validator(mode="after")
    def _order_safe(self) -> "OrderRequest":
        if not self.risk_approved:
            raise ValueError("orders require an explicit risk approval")
        if self.execution_mode in (ExecutionMode.RESEARCH.value, ExecutionMode.BACKTEST.value):
            raise ValueError(f"{self.execution_mode} mode cannot submit orders")
        if self.execution_mode == ExecutionMode.LIVE.value:
            raise ValueError("live order submission is disabled")
        if self.stop_loss is not None:
            if self.side == OrderSide.BUY.value and not self.stop_loss < self.expected_price:
                raise ValueError("buy stop_loss must be below the expected price")
            if self.side == OrderSide.SELL.value and not self.stop_loss > self.expected_price:
                raise ValueError("sell stop_loss must be above the expected price")
        if self.take_profit is not None:
            if self.side == OrderSide.BUY.value and not self.take_profit > self.expected_price:
                raise ValueError("buy take_profit must be above the expected price")
            if self.side == OrderSide.SELL.value and not self.take_profit < self.expected_price:
                raise ValueError("sell take_profit must be below the expected price")
        return self

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump()


class OrderResult(BaseModel):
    """Normalised outcome of an execution attempt on any adapter."""

    model_config = ConfigDict(extra="allow", use_enum_values=True)

    order_id: int | str | None = None
    client_order_id: str | None = None
    product_id: int = 0
    side: str = ""
    size: float = 0.0
    price: float | None = None
    fill_price: float | None = None
    fees: float = 0.0
    latency_ms: float = 0.0
    expected_price: float | None = None
    slippage: float = 0.0
    slippage_bps: float = 0.0
    order_state: str = "open"
    venue: str = ""
    status: str = "accepted"
    message: str = ""
    symbol: str = ""
    timeframe: str = ""
    risk_decision_key: str = ""
    risk_approved: bool = False
    execution_mode: ExecutionMode | None = None
    created_at: datetime = Field(default_factory=_utcnow)

    @field_validator("size", "fees", "latency_ms")
    @classmethod
    def _nonnegative(cls, value: object) -> float:
        number = _finite(value)
        if number < 0:
            raise ValueError("value must not be negative")
        return number

    @field_validator("slippage", "slippage_bps")
    @classmethod
    def _signed(cls, value: object) -> float:
        return _finite(value)

    @field_validator("price", "fill_price", "expected_price")
    @classmethod
    def _price_valid(cls, value: float | None) -> float | None:
        return _positive(value) if value is not None else None

    @field_validator("created_at")
    @classmethod
    def _created_valid(cls, value: datetime | None) -> datetime:
        return _as_utc(value)

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump()


class Position(BaseModel):
    """A local or exchange position in the trading domain."""

    model_config = ConfigDict(extra="allow", use_enum_values=True)

    symbol: str
    side: SignalDirection
    size: float
    entry_price: float
    current_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    notional_usd: float | None = None
    leverage: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    fees: float = 0.0
    status: str = "open"
    opened_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    @field_validator("symbol")
    @classmethod
    def _symbol_valid(cls, value: str) -> str:
        return _normalize_symbol(value)

    @field_validator("size", "entry_price")
    @classmethod
    def _positive_value(cls, value: object) -> float:
        return _positive(value)

    @field_validator("current_price", "stop_loss", "take_profit", "notional_usd")
    @classmethod
    def _positive_optional(cls, value: float | None) -> float | None:
        return _positive(value) if value is not None else None

    @field_validator("leverage", "fees")
    @classmethod
    def _nonnegative(cls, value: object) -> float:
        number = _finite(value)
        if number < 0:
            raise ValueError("value must not be negative")
        return number


class Trade(BaseModel):
    """A closed round trip with its attributable result."""

    model_config = ConfigDict(extra="allow", use_enum_values=True)

    symbol: str
    side: SignalDirection
    size: float
    entry_price: float
    exit_price: float
    pnl: float
    fees: float = 0.0
    reason: str = ""
    strategy_id: str = ""
    strategy_version: str = ""
    execution_mode: ExecutionMode | None = None
    opened_at: datetime = Field(default_factory=_utcnow)
    closed_at: datetime = Field(default_factory=_utcnow)

    @field_validator("symbol")
    @classmethod
    def _symbol_valid(cls, value: str) -> str:
        return _normalize_symbol(value)

    @field_validator("size", "entry_price", "exit_price")
    @classmethod
    def _positive_value(cls, value: object) -> float:
        return _positive(value)

    @field_validator("pnl", "fees")
    @classmethod
    def _finite_value(cls, value: object) -> float:
        return _finite(value)


class AccountState(BaseModel):
    """A point-in-time account snapshot used by risk and reporting."""

    model_config = ConfigDict(extra="allow", use_enum_values=True)

    account_key: str
    currency: str = "INR"
    starting_equity: float
    equity: float
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    open_positions: int = 0
    updated_at: datetime = Field(default_factory=_utcnow)

    @field_validator("account_key")
    @classmethod
    def _account_key_valid(cls, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("account_key is required")
        return text

    @field_validator("starting_equity", "equity")
    @classmethod
    def _finite_value(cls, value: object) -> float:
        number = _finite(value)
        if number < 0:
            raise ValueError("equity must not be negative")
        return number

    @field_validator("open_positions")
    @classmethod
    def _positions_valid(cls, value: object) -> int:
        if isinstance(value, bool):
            raise ValueError("open_positions must be an integer")
        number = int(value)  # type: ignore[arg-type]
        if number < 0:
            raise ValueError("open_positions must not be negative")
        return number
