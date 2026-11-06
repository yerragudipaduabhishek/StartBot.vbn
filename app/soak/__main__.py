"""CLI entry point for the Phase 7A local paper-trading soak test.

Usage:
    python -m app.soak                        # indefinite, default settings
    python -m app.soak --duration 3600        # exactly one hour
    python -m app.soak --duration 0           # indefinite (explicit)
    python -m app.soak --no-ws                # REST candles+ticker only
    python -m app.soak --out data/soak.jsonl  # custom telemetry file

Always paper-only: the banner prints PAPER TRADING / NOT REAL FUNDS / NO
EXCHANGE ORDERS / LIVE OFF so a long-running session cannot be mistaken for a
live one. Ctrl+C shuts everything down cleanly (feeds stopped, telemetry
flushed).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from app.analysis.config import DEFAULT_WATCHLIST
from app.config.settings import get_settings
from app.database.database import init_db
from app.soak.runner import BANNER, SoakRunner

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="app.soak",
        description="Phase 7A local paper-trading soak test: real public market data, PAPER execution only, no exchange orders.",
    )
    p.add_argument("--symbol", default=None, help="trading symbol (default: settings.paper_symbol = BTCUSDT)")
    p.add_argument("--watchlist", default="", help="comma-separated watchlist symbols (default: BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,BNBUSDT)")
    p.add_argument("--duration", type=float, default=0.0, help="run for N seconds; 0/omitted = run indefinitely")
    p.add_argument("--tick", type=float, default=30.0, help="telemetry/signal poll interval in seconds")
    p.add_argument("--poll", type=float, default=45.0, help="market-data REST poll interval per feed in seconds")
    p.add_argument("--resolution", default="1h", help="candle resolution (default 1h = Strategy A)")
    p.add_argument("--out", default="data/soak_telemetry.jsonl", help="telemetry JSONL output file")
    p.add_argument("--no-ws", action="store_true", help="disable the websocket stream (REST candles + ticker only)")
    return p


def run() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(module)s: %(message)s", datefmt="%H:%M:%S")
    settings = get_settings()
    init_db()

    symbol = (args.symbol or settings.paper_symbol).upper()
    watchlist = [s.strip().upper() for s in args.watchlist.split(",") if s.strip()]
    if not watchlist:
        watchlist = list(DEFAULT_WATCHLIST)

    duration_note = f"{args.duration:g}s" if args.duration and args.duration > 0 else "INDEFINITE"
    print(BANNER)
    print(f"  symbol    : {symbol}")
    print(f"  watchlist : {', '.join(watchlist)}")
    print(f"  duration  : {duration_note}")
    print(f"  tick      : {args.tick:g}s  poll: {args.poll:g}s  resolution: {args.resolution}")
    print(f"  websocket : {'ON' if not args.no_ws else 'OFF (REST only)'}")
    print(f"  telemetry : {args.out}")
    print(BANNER)

    runner = SoakRunner(
        symbol=symbol,
        watchlist=watchlist,
        tick_secs=args.tick,
        feed_poll_secs=args.poll,
        telemetry_path=args.out,
        enable_ws=not args.no_ws,
    )
    try:
        asyncio.run(runner.run(duration=args.duration if args.duration and args.duration > 0 else None))
    except KeyboardInterrupt:
        print("\nCtrl+C received - shutting down gracefully, flushing telemetry.")
    print()
    print(runner.summary_text())
    return 0


if __name__ == "__main__":
    sys.exit(run())