"""Reproducible historical datasets: pagination, metadata, hashing, storage.

A research result is only meaningful when it can be tied to the exact candle
rows that produced it. This module builds a canonical dataframe, validates it,
computes a content hash, and records provenance (source/venue/transport/
fallback) alongside row and gap statistics.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import pandas as pd
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.data.quality import CandleQualityReport, DataQualityError, validate_candles
from app.strategy.timeframes import normalize_timeframe, timeframe_seconds

MAX_ROWS_PER_REQUEST = 4000
DATASET_SCHEMA_VERSION = "phase2.v1"


@dataclass
class DatasetMetadata:
    symbol: str
    timeframe: str
    rows: int
    dataset_hash: str
    start: int | None
    end: int | None
    first_candle_time: int | None
    last_candle_time: int | None
    source: str = "unknown"
    venue: str = "unknown"
    transport: str = "unknown"
    endpoint: str = ""
    fallback_used: bool = False
    fetched_at: int | None = None
    closed_only: bool = True
    page_count: int = 0
    request_count: int = 0
    pagination_complete: bool = True
    quality: dict[str, Any] = field(default_factory=dict)
    schema_version: str = DATASET_SCHEMA_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "rows": self.rows,
            "dataset_hash": self.dataset_hash,
            "start": self.start,
            "end": self.end,
            "first_candle_time": self.first_candle_time,
            "last_candle_time": self.last_candle_time,
            "source": self.source,
            "venue": self.venue,
            "transport": self.transport,
            "endpoint": self.endpoint,
            "fallback_used": self.fallback_used,
            "fetched_at": self.fetched_at,
            "closed_only": self.closed_only,
            "page_count": self.page_count,
            "request_count": self.request_count,
            "pagination_complete": self.pagination_complete,
            "quality": dict(self.quality),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DatasetMetadata":
        return cls(
            symbol=str(payload.get("symbol") or ""),
            timeframe=str(payload.get("timeframe") or ""),
            rows=int(payload.get("rows") or 0),
            dataset_hash=str(payload.get("dataset_hash") or ""),
            start=payload.get("start"),
            end=payload.get("end"),
            first_candle_time=payload.get("first_candle_time"),
            last_candle_time=payload.get("last_candle_time"),
            source=str(payload.get("source") or "unknown"),
            venue=str(payload.get("venue") or "unknown"),
            transport=str(payload.get("transport") or "unknown"),
            endpoint=str(payload.get("endpoint") or ""),
            fallback_used=bool(payload.get("fallback_used")),
            fetched_at=payload.get("fetched_at"),
            closed_only=bool(payload.get("closed_only", True)),
            page_count=int(payload.get("page_count") or 0),
            request_count=int(payload.get("request_count") or 0),
            pagination_complete=bool(payload.get("pagination_complete", True)),
            quality=dict(payload.get("quality") or {}),
            schema_version=str(payload.get("schema_version") or DATASET_SCHEMA_VERSION),
        )


def _numeric_frame(candles: Iterable[dict] | pd.DataFrame) -> pd.DataFrame:
    df = candles.copy() if isinstance(candles, pd.DataFrame) else pd.DataFrame(list(candles))
    required = ["time", "open", "high", "low", "close"]
    missing = [column for column in required if column not in df.columns]
    if missing:
        raise ValueError(f"candles missing required columns: {', '.join(missing)}")
    if "volume" not in df.columns:
        df["volume"] = 0.0
    df = df[["time", "open", "high", "low", "close", "volume"]].copy()
    for column in ("time",):
        df[column] = pd.to_numeric(df[column], errors="coerce").astype("Int64")
    for column in ("open", "high", "low", "close", "volume"):
        df[column] = pd.to_numeric(df[column], errors="coerce").astype(float)
    return df


def canonical_candles(candles: Iterable[dict] | pd.DataFrame) -> pd.DataFrame:
    """Return a canonical ascending dataframe, dropping exact duplicate bars.

    Duplicate timestamps are collapsed deterministically (first occurrence after
    sorting by time). Any remaining data-quality issue is reported by
    :func:`validate_candles`.
    """
    df = _numeric_frame(candles)
    df = df.dropna(subset=["time"]).copy()
    df["time"] = df["time"].astype("int64")
    df = df.sort_values("time", kind="mergesort").drop_duplicates(subset=["time"], keep="first")
    return df.reset_index(drop=True)


def dataset_hash(candles: Iterable[dict] | pd.DataFrame, *, symbol: str, timeframe: str) -> str:
    """SHA-256 over canonical row content (not provenance or metadata)."""
    df = canonical_candles(candles)
    digest = hashlib.sha256()
    digest.update(f"{DATASET_SCHEMA_VERSION}|{symbol.upper()}|{normalize_timeframe(timeframe)}\n".encode())
    for row in df.itertuples(index=False):
        digest.update(
            (
                f"{int(row.time)}|{float(row.open)!r}|{float(row.high)!r}|"
                f"{float(row.low)!r}|{float(row.close)!r}|{float(row.volume)!r}\n"
            ).encode()
        )
    return digest.hexdigest()


def filter_range(df: pd.DataFrame, start: int | None, end: int | None) -> pd.DataFrame:
    out = df
    if start is not None:
        out = out[out["time"] >= int(start)]
    if end is not None:
        out = out[out["time"] <= int(end)]
    return out.reset_index(drop=True)


def _symbol_from_url(url: str) -> str:
    try:
        return urlparse(url).netloc or url
    except ValueError:
        return url


def build_dataset(
    candles: Iterable[dict] | pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    start: int | None = None,
    end: int | None = None,
    source: str = "unknown",
    venue: str = "unknown",
    transport: str = "unknown",
    endpoint: str = "",
    fallback_used: bool = False,
    fetched_at: int | None = None,
    closed_only: bool = True,
    now: float | None = None,
    strict: bool = False,
    allow_gaps: bool = True,
    check_stale: bool = False,
) -> tuple[pd.DataFrame, DatasetMetadata]:
    """Build a validated, hashed dataset and its provenance metadata."""
    import time as _time

    timeframe = normalize_timeframe(timeframe)
    now_value = float(now if now is not None else _time.time())
    raw = _numeric_frame(candles)
    raw = raw.dropna(subset=["time"]).copy()
    raw["time"] = raw["time"].astype("int64")
    if not raw["time"].is_monotonic_increasing:
        raise DataQualityError("candles must be in ascending time order")
    raw = raw.sort_values("time", kind="mergesort")
    raw = filter_range(raw, start, end)
    unclosed_dropped = 0
    if closed_only:
        interval = timeframe_seconds(timeframe)
        unclosed_dropped = int((raw["time"] + interval > now_value).sum())
        raw = raw[raw["time"] + interval <= now_value].reset_index(drop=True)

    report: CandleQualityReport = validate_candles(
        raw.to_dict("records"),
        symbol=symbol,
        timeframe=timeframe,
        now=now_value,
        require_closed=closed_only,
        check_stale=check_stale,
        strict=False,
    )
    # Critical issues (duplicates, invalid OHLCV, unclosed, misaligned,
    # out-of-order rows) always reject the dataset. Gap strictness is opt-in.
    report.require_ok(allow_gaps=(allow_gaps if strict else True), allow_stale=True)
    report.unclosed_dropped = unclosed_dropped

    df = canonical_candles(raw)
    row_hash = dataset_hash(df, symbol=symbol, timeframe=timeframe)
    metadata = DatasetMetadata(
        symbol=symbol.upper(),
        timeframe=timeframe,
        rows=len(df),
        dataset_hash=row_hash,
        start=start,
        end=end,
        first_candle_time=int(df["time"].iloc[0]) if len(df) else None,
        last_candle_time=int(df["time"].iloc[-1]) if len(df) else None,
        source=source,
        venue=venue,
        transport=transport,
        endpoint=endpoint,
        fallback_used=fallback_used,
        fetched_at=fetched_at,
        closed_only=closed_only,
        quality=report.as_dict(),
    )
    return df, metadata


def load_candles_from_db(
    symbol: str,
    timeframe: str,
    *,
    start: int | None = None,
    end: int | None = None,
    limit: int | None = None,
    session: Session | None = None,
    closed_only: bool = True,
    now: float | None = None,
) -> pd.DataFrame:
    """Load candles from the trading database without mutating it."""
    import time as _time

    from app.database.database import SessionLocal
    from app.database.models import Candle

    own = session is None
    db = session or SessionLocal()
    try:
        query = (
            select(Candle)
            .where(
                Candle.symbol == symbol,
                Candle.resolution == normalize_timeframe(timeframe),
                or_(Candle.quality_status.is_(None), Candle.quality_status != "quarantined"),
            )
            .order_by(Candle.time)
        )
        if start is not None:
            query = query.where(Candle.time >= int(start))
        if end is not None:
            query = query.where(Candle.time <= int(end))
        if limit:
            query = query.limit(int(limit))
        rows = db.execute(query).scalars().all()
        records = [
            {"time": row.time, "open": row.open, "high": row.high, "low": row.low, "close": row.close, "volume": row.volume}
            for row in rows
        ]
    finally:
        if own:
            db.close()
    interval = timeframe_seconds(timeframe)
    now_value = float(now if now is not None else _time.time())
    if closed_only:
        records = [row for row in records if int(row["time"]) + interval <= now_value]
    return pd.DataFrame(records)


def load_dataset(
    symbol: str,
    timeframe: str,
    *,
    start: int | None = None,
    end: int | None = None,
    limit: int | None = None,
    session: Session | None = None,
    source: str = "database",
    strict: bool = False,
) -> tuple[pd.DataFrame, DatasetMetadata]:
    df = load_candles_from_db(symbol, timeframe, start=start, end=end, limit=limit, session=session)
    if df.empty:
        raise ValueError(f"no closed candles for {symbol} {timeframe} in the database")
    return build_dataset(
        df,
        symbol=symbol,
        timeframe=timeframe,
        start=start,
        end=end,
        source=source,
        venue="local-db",
        transport="database",
        strict=strict,
    )


def _client_meta(client: Any) -> dict[str, Any]:
    meta = getattr(client, "last_read_meta", None)
    return dict(meta) if isinstance(meta, dict) else {}


def _fetch_window(
    client: Any,
    symbol: str,
    timeframe: str,
    start: int,
    end: int,
    *,
    max_rows: int,
    depth: int = 0,
) -> tuple[list[dict], int, bool]:
    if end < start:
        return [], 0, True
    fetch_meta = getattr(client, "get_candles_with_metadata", None)
    if callable(fetch_meta):
        rows = fetch_meta(symbol, resolution=timeframe, start=start, end=end)
    else:
        rows = client.get_candles(symbol, resolution=timeframe, start=start, end=end)
    rows = list(rows or [])
    requests = 1
    interval = timeframe_seconds(timeframe)
    if len(rows) < min(max_rows, 2) or end - start <= interval or depth >= 24:
        return rows, requests, True
    if len(rows) >= max_rows:
        midpoint = ((start + end) // (2 * interval)) * interval
        if midpoint <= start or midpoint >= end:
            return rows, requests, True
        left, left_requests, left_ok = _fetch_window(
            client, symbol, timeframe, start, midpoint - interval, max_rows=max_rows, depth=depth + 1
        )
        right, right_requests, right_ok = _fetch_window(
            client, symbol, timeframe, midpoint, end, max_rows=max_rows, depth=depth + 1
        )
        return left + right, requests + left_requests + right_requests, left_ok and right_ok
    return rows, requests, True


def fetch_dataset(
    client: Any,
    *,
    symbol: str,
    timeframe: str,
    start: int,
    end: int | None = None,
    max_rows: int = MAX_ROWS_PER_REQUEST,
    now: float | None = None,
    strict: bool = False,
    transport: str = "rest",
) -> tuple[pd.DataFrame, DatasetMetadata]:
    """Fetch a range with bounded pages and return a hashed, validated dataset.

    The Delta endpoint returns the newest rows of a window when the window is
    too large. The fetch recurses by time-splitting whenever a page is full, so
    no silently-truncated history can enter the dataset.
    """
    import time as _time

    timeframe = normalize_timeframe(timeframe)
    now_value = float(now if now is not None else _time.time())
    end_value = int(end if end is not None else now_value)
    end_value = min(end_value, int(now_value))
    start_value = int(start)
    if end_value < start_value:
        raise ValueError("dataset end must be after start")

    rows, requests, complete = _fetch_window(
        client,
        symbol,
        timeframe,
        start_value,
        end_value,
        max_rows=max(1, int(max_rows)),
    )
    meta = _client_meta(client)
    fetched_at = int(_time.time())
    df, metadata = build_dataset(
        rows,
        symbol=symbol,
        timeframe=timeframe,
        start=start_value,
        end=end_value,
        source=str(meta.get("source") or "delta"),
        venue=str(meta.get("venue") or _symbol_from_url(str(meta.get("base_url") or ""))),
        transport=transport,
        endpoint=str(meta.get("endpoint") or "/v2/history/candles"),
        fallback_used=bool(meta.get("fallback_used")),
        fetched_at=fetched_at,
        closed_only=True,
        now=now_value,
        strict=strict,
        allow_gaps=True,
    )
    metadata.request_count = requests
    metadata.page_count = requests
    metadata.pagination_complete = complete
    metadata.quality["requested_start"] = start_value
    metadata.quality["requested_end"] = end_value
    return df, metadata


def save_metadata(metadata: DatasetMetadata, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(metadata.as_dict(), indent=2, sort_keys=True), encoding="utf-8")
    return target


def load_metadata(path: str | Path) -> DatasetMetadata:
    return DatasetMetadata.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


__all__ = [
    "DATASET_SCHEMA_VERSION",
    "DatasetMetadata",
    "MAX_ROWS_PER_REQUEST",
    "build_dataset",
    "canonical_candles",
    "dataset_hash",
    "fetch_dataset",
    "filter_range",
    "load_candles_from_db",
    "load_dataset",
    "load_metadata",
    "save_metadata",
]
