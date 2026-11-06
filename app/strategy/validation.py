"""Fail-closed validation at every trading boundary."""

from __future__ import annotations

import math
from datetime import datetime, timezone

from app.config.trading import TradingProfile
from app.strategy.signal import DataQuality, Signal
from app.strategy.timeframes import normalize_timeframe, timeframe_seconds


class SignalValidationError(ValueError):
    """Raised when a signal must not reach an execution boundary."""


def _finite(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _positive(value: object) -> float | None:
    number = _finite(value)
    return number if number is not None and number > 0 else None


def _as_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def data_quality_from_snapshot(snapshot: dict | None, *, profile: TradingProfile) -> DataQuality:
    if not snapshot:
        return DataQuality(status="unavailable", price_valid=False, source="none", symbol=profile.symbol, timeframe=profile.timeframe)
    source = str(snapshot.get("data_source") or snapshot.get("source") or "unknown")
    fallback = bool(snapshot.get("fallback_used")) or "fallback" in source.lower()
    return DataQuality(
        status=str(snapshot.get("data_status") or snapshot.get("status") or "unknown"),
        price_valid=bool(snapshot.get("price_valid")),
        source=source,
        last_candle_age_secs=_finite(snapshot.get("last_candle_age_secs")),
        symbol=str(snapshot.get("symbol") or profile.symbol),
        timeframe=str(snapshot.get("timeframe") or profile.timeframe),
        fallback_used=fallback,
    )


def validate_signal(
    payload: Signal | dict,
    *,
    profile: TradingProfile,
    candle_time: int | None = None,
    now: datetime | None = None,
    data_quality: DataQuality | dict | None = None,
    check_freshness: bool = True,
) -> Signal:
    """Validate and normalize a signal.

    Missing risk approval, malformed prices, wrong-side stops/targets, stale
    candles, mismatched profiles and non-streaming data are all rejected. A
    ``none`` signal is allowed only with ``risk_allowed=False``.
    """

    raw = payload.model_dump() if isinstance(payload, Signal) else dict(payload)
    direction_raw = raw.get("direction")
    direction = "none" if direction_raw in ("neutral", None, "") else str(direction_raw).lower()
    if direction not in ("long", "short", "none"):
        raise SignalValidationError(f"invalid direction: {direction_raw!r}")

    symbol_raw = raw.get("symbol")
    symbol = str(symbol_raw).strip().upper() if symbol_raw else profile.symbol
    if symbol != profile.symbol:
        raise SignalValidationError(f"symbol mismatch: signal={symbol} profile={profile.symbol}")

    timeframe_raw = raw.get("timeframe")
    try:
        timeframe = normalize_timeframe(str(timeframe_raw)) if timeframe_raw else profile.timeframe
    except ValueError as exc:
        raise SignalValidationError(str(exc)) from exc
    if timeframe != profile.timeframe:
        raise SignalValidationError(f"timeframe mismatch: signal={timeframe} profile={profile.timeframe}")

    strategy_id = str(raw.get("strategy_id") or profile.strategy_id)
    strategy_version = str(raw.get("strategy_version") or profile.strategy_version)
    if strategy_id != profile.strategy_id or strategy_version != profile.strategy_version:
        raise SignalValidationError(
            f"strategy mismatch: signal={strategy_id}@{strategy_version} profile={profile.strategy_id}@{profile.strategy_version}"
        )

    candle_raw = raw.get("candle_time", raw.get("time", candle_time))
    try:
        candle_time_value = int(candle_raw)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise SignalValidationError("candle_time is required") from exc
    if isinstance(candle_raw, bool):
        raise SignalValidationError("candle_time is required")

    created_at_raw = raw.get("created_at")
    if created_at_raw is None:
        created_at = _as_utc(now)
    elif isinstance(created_at_raw, datetime):
        created_at = _as_utc(created_at_raw)
    else:
        try:
            created_at = _as_utc(datetime.fromisoformat(str(created_at_raw)))
        except ValueError as exc:
            raise SignalValidationError("created_at is invalid") from exc

    risk_allowed = raw.get("risk_allowed")
    if risk_allowed is not True and risk_allowed is not False:
        raise SignalValidationError("risk_allowed must be an explicit boolean")
    accepted_raw = raw.get("accepted_size")
    accepted_size = _finite(accepted_raw) if accepted_raw is not None else None

    entry = _positive(raw.get("entry"))
    stop = _positive(raw.get("stop_loss"))
    target = _positive(raw.get("target"))

    dq_raw = raw.get("data_quality")
    if dq_raw is None:
        dq_raw = data_quality
    if isinstance(dq_raw, DataQuality):
        dq = dq_raw.model_copy(update={"symbol": symbol, "timeframe": timeframe})
    elif isinstance(dq_raw, dict):
        dq = DataQuality(**{**dq_raw, "symbol": symbol, "timeframe": timeframe})
    else:
        dq = DataQuality(status="unknown", price_valid=False, source="unknown", symbol=symbol, timeframe=timeframe)

    extras = {key: value for key, value in raw.items() if key not in Signal.model_fields}

    if profile.risk.require_streaming_data:
        if dq.status != "streaming":
            raise SignalValidationError(f"data is not streaming: {dq.status}")
        if not dq.price_valid:
            raise SignalValidationError("price is invalid")
    if check_freshness:
        now_utc = _as_utc(now)
        age = now_utc.timestamp() - float(candle_time_value)
        if age > profile.max_candle_age_secs:
            raise SignalValidationError(f"candle is stale ({age:.0f}s > {profile.max_candle_age_secs}s)")
        if age < -2 * timeframe_seconds(timeframe):
            raise SignalValidationError("candle_time is in the future")

    if direction == "none":
        if risk_allowed is not False:
            raise SignalValidationError("a none signal cannot be risk-approved")
        return Signal(
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            symbol=symbol,
            timeframe=timeframe,
            candle_time=candle_time_value,
            created_at=created_at,
            direction="none",
            score=_finite(raw.get("score")),
            confidence=_finite(raw.get("confidence")),
            entry=entry,
            stop_loss=stop,
            target=target,
            accepted_size=None,
            risk_allowed=False,
            risk_reason=str(raw.get("risk_reason") or "no trade"),
            data_quality=dq,
            **extras,
        )

    if risk_allowed is not True:
        raise SignalValidationError("risk_allowed must be True for an entry signal")
    if accepted_size is None or accepted_size <= 0:
        raise SignalValidationError("accepted_size must be positive")
    if entry is None:
        raise SignalValidationError("entry must be positive")
    if stop is None:
        raise SignalValidationError("stop_loss must be positive")
    if target is None:
        raise SignalValidationError("target must be positive")
    if direction == "long":
        if not stop < entry:
            raise SignalValidationError("long stop_loss must be below entry")
        if not target > entry:
            raise SignalValidationError("long target must be above entry")
    else:
        if not stop > entry:
            raise SignalValidationError("short stop_loss must be above entry")
        if not target < entry:
            raise SignalValidationError("short target must be below entry")

    confidence = _finite(raw.get("confidence"))
    if confidence is None or not 0.0 <= confidence <= 1.0:
        raise SignalValidationError("confidence must be between 0 and 1")
    score = _finite(raw.get("score"))
    if score is None:
        raise SignalValidationError("score must be finite")

    return Signal(
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        symbol=symbol,
        timeframe=timeframe,
        candle_time=candle_time_value,
        created_at=created_at,
        direction=direction,
        score=score,
        confidence=confidence,
        entry=entry,
        stop_loss=stop,
        target=target,
        accepted_size=accepted_size,
        risk_allowed=True,
        risk_reason=str(raw.get("risk_reason") or "risk approved"),
        data_quality=dq,
        **extras,
    )
