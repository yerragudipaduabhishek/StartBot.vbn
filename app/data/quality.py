"""Candle data quality and provenance validation.

The research engine must never silently train or evaluate on malformed,
duplicated, misaligned, incomplete or stale market data. This module is pure
and dependency-light so every ingestion path can apply the same rules.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from app.strategy.timeframes import max_candle_age_seconds, normalize_timeframe, timeframe_seconds


class DataQualityError(ValueError):
    """Raised when market data fails a required validation gate."""


@dataclass(frozen=True)
class DataIssue:
    code: str
    message: str
    time: int | None = None

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "time": self.time}


@dataclass
class CandleQualityReport:
    symbol: str
    timeframe: str
    row_count: int = 0
    first_time: int | None = None
    last_time: int | None = None
    expected_bars: int | None = None
    missing_bars: int = 0
    duplicate_count: int = 0
    invalid_count: int = 0
    wrong_symbol_count: int = 0
    wrong_timeframe_count: int = 0
    unclosed_count: int = 0
    unclosed_dropped: int = 0
    unaligned_count: int = 0
    out_of_order: bool = False
    stale: bool = False
    issues: list[DataIssue] = field(default_factory=list)

    @property
    def critical_ok(self) -> bool:
        return not (
            self.row_count == 0
            or self.duplicate_count
            or self.invalid_count
            or self.wrong_symbol_count
            or self.wrong_timeframe_count
            or self.unclosed_count
            or self.unaligned_count
            or self.out_of_order
        )

    @property
    def complete(self) -> bool:
        return self.missing_bars == 0

    @property
    def ok(self) -> bool:
        return self.critical_ok and self.complete and not self.stale

    def require_ok(self, *, allow_gaps: bool = False, allow_stale: bool = False) -> "CandleQualityReport":
        problems = [issue.message for issue in self.issues]
        if not self.critical_ok or (not allow_gaps and not self.complete) or (not allow_stale and self.stale):
            if not problems:
                problems.append("candle data failed quality validation")
            raise DataQualityError("; ".join(problems[:8]))
        return self

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "row_count": self.row_count,
            "first_time": self.first_time,
            "last_time": self.last_time,
            "expected_bars": self.expected_bars,
            "missing_bars": self.missing_bars,
            "duplicate_count": self.duplicate_count,
            "invalid_count": self.invalid_count,
            "wrong_symbol_count": self.wrong_symbol_count,
            "wrong_timeframe_count": self.wrong_timeframe_count,
            "unclosed_count": self.unclosed_count,
            "unclosed_dropped": self.unclosed_dropped,
            "unaligned_count": self.unaligned_count,
            "out_of_order": self.out_of_order,
            "stale": self.stale,
            "critical_ok": self.critical_ok,
            "complete": self.complete,
            "ok": self.ok,
            "issues": [issue.as_dict() for issue in self.issues],
        }


def _as_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _valid_ohlcv(row: dict) -> str | None:
    for key in ("open", "high", "low", "close"):
        number = _as_float(row.get(key))
        if number is None or number <= 0:
            return f"{key} must be finite and positive"
    volume = _as_float(row.get("volume", 0.0))
    if volume is None or volume < 0:
        return "volume must be finite and non-negative"
    high = float(row["high"])
    low = float(row["low"])
    open_ = float(row["open"])
    close = float(row["close"])
    if high < max(open_, close, low):
        return "high must be the highest price in the bar"
    if low > min(open_, close, high):
        return "low must be the lowest price in the bar"
    return None


def validate_candles(
    candles: list[dict] | tuple[dict, ...],
    *,
    symbol: str,
    timeframe: str,
    now: float | None = None,
    require_closed: bool = True,
    check_stale: bool = False,
    strict: bool = False,
    allow_gaps: bool = False,
    allow_stale: bool = False,
) -> CandleQualityReport:
    """Validate a candle sequence and return a structured report.

    Duplicates, out-of-order rows, malformed OHLCV, wrong symbol/timeframe,
    unaligned timestamps, and (optionally) unclosed or stale candles are
    reported. ``strict=True`` raises :class:`DataQualityError` unless the data
    is fully usable for research.
    """
    timeframe = normalize_timeframe(timeframe)
    interval = timeframe_seconds(timeframe)
    now_value = float(now if now is not None else time.time())
    report = CandleQualityReport(symbol=symbol, timeframe=timeframe)

    if not candles:
        report.issues.append(DataIssue("empty", "no candles supplied"))
        if strict:
            report.require_ok(allow_gaps=allow_gaps, allow_stale=allow_stale)
        return report

    times: list[int] = []
    seen: set[int] = set()
    previous: int | None = None
    for index, row in enumerate(candles):
        if not isinstance(row, dict):
            report.invalid_count += 1
            report.issues.append(DataIssue("invalid", f"row {index} is not a mapping"))
            continue
        raw_time = row.get("time")
        if isinstance(raw_time, bool):
            raw_time = None
        try:
            candle_time = int(raw_time)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            candle_time = None
        if candle_time is None or candle_time < 0:
            report.invalid_count += 1
            report.issues.append(DataIssue("invalid_time", f"row {index} has an invalid time"))
            continue

        issue = _valid_ohlcv(row)
        if issue is not None:
            report.invalid_count += 1
            report.issues.append(DataIssue("invalid_ohlcv", f"row {candle_time}: {issue}", candle_time))
            continue

        row_symbol = str(row.get("symbol") or symbol).upper()
        if row_symbol and row_symbol != symbol.upper():
            report.wrong_symbol_count += 1
            report.issues.append(DataIssue("wrong_symbol", f"row {candle_time} symbol {row_symbol} != {symbol}", candle_time))
        row_timeframe = row.get("resolution", row.get("timeframe"))
        if row_timeframe:
            try:
                row_resolution = normalize_timeframe(str(row_timeframe))
            except ValueError:
                row_resolution = str(row_timeframe)
            if row_resolution != timeframe:
                report.wrong_timeframe_count += 1
                report.issues.append(
                    DataIssue("wrong_timeframe", f"row {candle_time} timeframe {row_resolution} != {timeframe}", candle_time)
                )

        if candle_time % interval != 0:
            report.unaligned_count += 1
            report.issues.append(DataIssue("unaligned", f"row {candle_time} is not aligned to {timeframe}", candle_time))
        if require_closed and candle_time + interval > now_value:
            report.unclosed_count += 1
            report.issues.append(DataIssue("unclosed", f"row {candle_time} has not closed yet", candle_time))

        if candle_time in seen:
            report.duplicate_count += 1
            report.issues.append(DataIssue("duplicate", f"duplicate candle time {candle_time}", candle_time))
            continue
        if previous is not None and candle_time < previous:
            report.out_of_order = True
            report.issues.append(DataIssue("out_of_order", f"row {candle_time} follows {previous}", candle_time))
        previous = candle_time
        seen.add(candle_time)
        times.append(candle_time)

    report.row_count = len(times)
    if times:
        report.first_time = min(times)
        report.last_time = max(times)
        report.expected_bars = (report.last_time - report.first_time) // interval + 1
        report.missing_bars = max(0, report.expected_bars - len(times))
        if report.missing_bars:
            report.issues.append(
                DataIssue("missing_bars", f"{report.missing_bars} bar(s) missing between {report.first_time} and {report.last_time}")
            )
        max_age = max_candle_age_seconds(timeframe, bars=2)
        if check_stale and now_value - report.last_time > max_age:
            report.stale = True
            report.issues.append(
                DataIssue("stale", f"last candle is {now_value - report.last_time:.0f}s old (> {max_age}s)", report.last_time)
            )

    if strict:
        report.require_ok(allow_gaps=allow_gaps, allow_stale=allow_stale)
    return report


def closed_candles(
    candles: list[dict] | tuple[dict, ...],
    *,
    timeframe: str,
    now: float | None = None,
) -> list[dict]:
    """Return only candles whose full interval has elapsed."""
    interval = timeframe_seconds(timeframe)
    now_value = float(now if now is not None else time.time())
    out: list[dict] = []
    for row in candles:
        try:
            candle_time = int(row.get("time"))  # type: ignore[arg-type]
        except (TypeError, ValueError, AttributeError):
            continue
        if candle_time + interval <= now_value:
            out.append(dict(row))
    return out


__all__ = [
    "CandleQualityReport",
    "DataIssue",
    "DataQualityError",
    "closed_candles",
    "validate_candles",
]
