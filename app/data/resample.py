"""Resample stored candles into higher resolutions (5m -> 15m/1h/4h)."""

import logging

import pandas as pd
from sqlalchemy import or_, select

from app.config.settings import get_settings
from app.database.database import SessionLocal
from app.database.models import Candle

logger = logging.getLogger(__name__)

RESOLUTION_MINUTES = {"5m": 5, "15m": 15, "1h": 60, "4h": 240}


def _load_symbol(symbol: str, resolution: str) -> pd.DataFrame:
    with SessionLocal() as s:
        q = (
            select(Candle)
            .where(
                Candle.symbol == symbol,
                Candle.resolution == resolution,
                or_(Candle.quality_status.is_(None), Candle.quality_status != "quarantined"),
            )
            .order_by(Candle.time)
        )
        rows = s.execute(q).scalars().all()
    return pd.DataFrame(
        [
            {"time": c.time, "open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.volume}
            for c in rows
        ]
    )


def _resample(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    d = df.copy()
    d["bucket"] = d["time"] // (minutes * 60) * (minutes * 60)
    g = d.groupby("bucket")
    return pd.DataFrame(
        {
            "time": g["bucket"].first().astype(int),
            "open": g["open"].first(),
            "high": g["high"].max(),
            "low": g["low"].min(),
            "close": g["close"].last(),
            "volume": g["volume"].sum(),
        }
    ).reset_index(drop=True)


def resample_candles(
    symbol: str,
    source_resolution: str = "5m",
    targets: tuple[str, ...] = ("15m", "1h"),
) -> dict[str, int]:
    """Aggregate `source_resolution` candles into each target and insert any
    missing (idempotent via the unique candle constraint). Returns inserted
    counts per target."""
    if source_resolution not in RESOLUTION_MINUTES:
        raise ValueError(f"unsupported source resolution: {source_resolution}")
    src = _load_symbol(symbol, source_resolution)
    if src.empty:
        raise ValueError(f"no {source_resolution} candles in DB for {symbol}")

    out: dict[str, int] = {}
    with SessionLocal() as s:
        for target in targets:
            if target not in RESOLUTION_MINUTES:
                raise ValueError(f"unsupported target resolution: {target}")
            resampled = _resample(src, RESOLUTION_MINUTES[target])
            existing = set(
                s.execute(
                    select(Candle.time).where(Candle.symbol == symbol, Candle.resolution == target)
                ).scalars()
            )
            new = 0
            for _, r in resampled.iterrows():
                if r["time"] in existing:
                    continue
                s.add(
                    Candle(
                        symbol=symbol,
                        resolution=target,
                        time=int(r["time"]),
                        open=float(r["open"]),
                        high=float(r["high"]),
                        low=float(r["low"]),
                        close=float(r["close"]),
                        volume=float(r["volume"]),
                        source="resample",
                        venue="local-db",
                        transport="derived",
                        fallback_used=False,
                        is_closed=True,
                    )
                )
                new += 1
            out[target] = new
        s.commit()
    return out


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Resample stored candles to higher resolutions")
    ap.add_argument("--symbol", default=get_settings().trading_symbol)
    ap.add_argument("--source", default="5m")
    ap.add_argument("--targets", default="15m,1h")
    args = ap.parse_args()
    res = resample_candles(args.symbol, args.source, tuple(t.strip() for t in args.targets.split(",")))
    print(f"inserted: {res}")