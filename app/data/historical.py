import logging
import time
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config.settings import get_settings
from app.data.delta_client import DeltaClient
from app.data.quality import validate_candles
from app.database.database import SessionLocal, init_db
from app.database.models import Candle
from app.strategy.timeframes import normalize_timeframe, timeframe_seconds

logger = logging.getLogger(__name__)

MAX_ROWS_PER_REQUEST = 4000


def _provenance(client: DeltaClient, symbol: str, resolution: str, candles: list[dict]) -> dict:
    meta = dict(getattr(client, "last_read_meta", {}) or {})
    now = int(time.time())
    interval = timeframe_seconds(resolution)
    report = validate_candles(
        candles,
        symbol=symbol,
        timeframe=resolution,
        now=now,
        require_closed=False,
        strict=False,
    )
    return {
        "source": str(meta.get("source") or "delta"),
        "venue": str(meta.get("venue") or "unknown"),
        "transport": str(meta.get("transport") or "rest"),
        "fallback_used": bool(meta.get("fallback_used")),
        "fetched_at": datetime.fromtimestamp(int(meta.get("fetched_at") or now), timezone.utc),
        "is_closed": lambda candle_time: int(candle_time) + interval <= now,
        "quality": report.as_dict(),
        "report": report,
    }


_CRITICAL_CODES = {
    "invalid",
    "invalid_time",
    "invalid_ohlcv",
    "wrong_symbol",
    "wrong_timeframe",
    "unaligned",
    "unclosed",
    "duplicate",
    "out_of_order",
}


def _quality_flags(provenance: dict) -> tuple[dict[int, list[str]], bool]:
    """Map critical quality findings to candle times.

    Findings that cannot be attributed to a specific row quarantine the whole
    response, because inserting an unknown bad row would be unsafe.
    """
    report = provenance.get("report")
    flags: dict[int, list[str]] = {}
    unattributed = False
    for issue in getattr(report, "issues", ()):
        if issue.code not in _CRITICAL_CODES:
            continue
        if issue.time is None:
            unattributed = True
            continue
        flags.setdefault(int(issue.time), []).append(issue.code)
    return flags, unattributed


def _row(symbol: str, resolution: str, candle: dict, provenance: dict) -> Candle:
    flags, unattributed = _quality_flags(provenance)
    candle_time = int(candle["time"])
    row_flags = flags.get(candle_time, [])
    status = "quarantined" if unattributed or row_flags else "ok"
    return Candle(
        symbol=symbol,
        resolution=resolution,
        time=candle_time,
        open=float(candle["open"]),
        high=float(candle["high"]),
        low=float(candle["low"]),
        close=float(candle["close"]),
        volume=float(candle.get("volume", 0.0) or 0.0),
        source=provenance["source"],
        venue=provenance["venue"],
        transport=provenance["transport"],
        fallback_used=provenance["fallback_used"],
        is_closed=provenance["is_closed"](candle_time),
        quality_status=status,
        quality_flags=",".join(sorted(set(row_flags))) if row_flags else "",
        ingested_at=datetime.now(timezone.utc),
        fetched_at=provenance["fetched_at"],
    )


def backfill(
    symbol: str,
    resolution: str = "5m",
    hours: int = 24,
    client: DeltaClient | None = None,
    session: Session | None = None,
) -> int:
    """Fetch historical candles and insert any that are missing (idempotent).

    Every inserted row carries source/venue/transport/fallback provenance and a
    closed-candle flag. The research dataset layer excludes unclosed bars.
    """
    resolution = normalize_timeframe(resolution)
    settings = get_settings()
    client = client or DeltaClient(
        settings.delta_market_data_url,
        settings=settings,
        read_fallback_urls=settings.delta_market_data_fallback_urls,
    )
    own_session = session is None
    session = session or SessionLocal()

    end = int(time.time())
    start = end - (hours * 3600)
    candles = client.get_candles(symbol, resolution=resolution, start=start, end=end)
    if not candles:
        logger.warning("no candles returned for %s %s", symbol, resolution)
        return 0

    provenance = _provenance(client, symbol, resolution, candles)
    if not provenance["quality"]["critical_ok"]:
        logger.warning("backfill quality issues for %s %s: %s", symbol, resolution, provenance["quality"]["issues"][:3])
    times = set(session.execute(select(Candle.time).where(Candle.symbol == symbol, Candle.resolution == resolution)).scalars())
    inserted = 0
    for c in candles:
        candle_time = int(c["time"])
        if candle_time in times:
            continue
        try:
            session.add(_row(symbol, resolution, c, provenance))
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("skipping malformed candle %s %s %s: %s", symbol, resolution, candle_time, exc)
            continue
        times.add(candle_time)
        inserted += 1
    if own_session:
        session.commit()
        session.close()
    return inserted


def backfill_deep(
    symbol: str,
    resolution: str = "5m",
    days: int = 90,
    chunk_days: int = 14,
    client: DeltaClient | None = None,
    session: Session | None = None,
) -> dict[str, int]:
    """Fetch history walking backward in chunks sized so each REST call stays
    under the ~4000-row response cap (the API only returns the newest rows of a
    window, so deep history requires paginating by time slices).

    Idempotent: existing rows are skipped via the (symbol, resolution, time)
    unique constraint. Returns {"fetched": n, "chunks": k, "quality": report}.
    """
    resolution = normalize_timeframe(resolution)
    settings = get_settings()
    client = client or DeltaClient(
        settings.delta_market_data_url,
        settings=settings,
        read_fallback_urls=settings.delta_market_data_fallback_urls,
    )
    own_session = session is None
    session = session or SessionLocal()

    existing = set(session.execute(select(Candle.time).where(Candle.symbol == symbol, Candle.resolution == resolution)).scalars())

    step_sec = chunk_days * 86400
    end = int(time.time())
    start = end - step_sec
    fetched = 0
    chunks = 0
    quality: dict = {}
    guard = max(1, int(days / chunk_days) + 2)

    for _ in range(guard):
        rows = client.get_candles(symbol, resolution=resolution, start=start, end=end)
        chunks += 1
        if not rows:
            break
        provenance = _provenance(client, symbol, resolution, rows)
        quality = provenance["quality"]
        if not provenance["quality"]["critical_ok"]:
            logger.warning("backfill_deep quality issues for %s %s: %s", symbol, resolution, provenance["quality"]["issues"][:3])
        for c in rows:
            candle_time = int(c["time"])
            if candle_time in existing:
                continue
            try:
                session.add(_row(symbol, resolution, c, provenance))
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning("skipping malformed candle %s %s %s: %s", symbol, resolution, candle_time, exc)
                continue
            existing.add(candle_time)
            fetched += 1
        oldest = min(c["time"] for c in rows)
        end = oldest - 1
        start = end - step_sec
        if (int(time.time()) - oldest) > days * 86400:
            break
        if len(rows) < MAX_ROWS_PER_REQUEST * 0.98:
            break

    if own_session:
        session.commit()
        session.close()
    return {"fetched": fetched, "chunks": chunks, "quality": quality}


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    init_db()
    parser = argparse.ArgumentParser(description="Backfill Delta candle history")
    parser.add_argument("--symbol", default=get_settings().trading_symbol)
    parser.add_argument("--resolution", default=get_settings().trading_timeframe)
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--chunk-days", type=int, default=14)
    args = parser.parse_args()
    # recent first, then deep
    n_recent = backfill(args.symbol, args.resolution, hours=48)
    deep = backfill_deep(args.symbol, args.resolution, days=args.days, chunk_days=args.chunk_days)
    print(f"recent inserted: {n_recent}; deep fetched: {deep}")