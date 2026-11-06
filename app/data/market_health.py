"""24/7 multi-symbol market-data health monitor (read-only).

Polls real REST candles + tickers/OI/funding for every watchlist symbol and
classifies each feed honestly: streaming / stale / unavailable. It reports only
values that actually came from Delta public endpoints - it never fabricates a
price, an OI figure or a funding rate. It never places an order and never
touches the authenticated Delta client.
"""

import asyncio
import logging
import time

from app.analysis.config import DEFAULT_WATCHLIST
from app.config.settings import Settings, get_settings
from app.data.delta_client import DeltaClient

logger = logging.getLogger(__name__)


def _num(value) -> float | None:
    try:
        if value is None or value == "":
            return None
        v = float(value)
        return v if v == v else None  # reject NaN
    except (TypeError, ValueError):
        return None


class SymbolHealth:
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self.status = "unavailable"
        self.status_reason: str | None = None
        self.price: float | None = None
        self.price_valid = False
        self.candles_count = 0
        self.last_candle_time: int | None = None
        self.last_candle_age_secs: int | None = None
        self.open_interest: float | None = None
        self.funding: float | None = None
        self.oi_missing = True
        self.funding_missing = True
        self.feed_ok = False
        self.ticker_ok = False
        self.source = "none"
        self.error: str | None = None
        self.checked_at: int | None = None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "status": self.status,
            "status_reason": self.status_reason,
            "price": self.price,
            "price_valid": self.price_valid,
            "candles_count": self.candles_count,
            "last_candle_time": self.last_candle_time,
            "last_candle_age_secs": self.last_candle_age_secs,
            "open_interest": self.open_interest,
            "funding": self.funding,
            "oi_missing": self.oi_missing,
            "funding_missing": self.funding_missing,
            "feed_ok": self.feed_ok,
            "ticker_ok": self.ticker_ok,
            "source": self.source,
            "error": self.error,
            "checked_at": self.checked_at,
        }


class MarketHealthMonitor:
    def __init__(
        self,
        settings: Settings | None = None,
        symbols: list[str] | None = None,
        resolution: str = "1h",
        fresh_secs: int = 3 * 3600,
        poll_secs: float = 60.0,
        client: DeltaClient | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.symbols = list(symbols or DEFAULT_WATCHLIST)
        self.resolution = resolution
        self.fresh_secs = fresh_secs
        self.poll_secs = poll_secs
        self.client = client or DeltaClient(
            self.settings.delta_market_data_url,
            settings=self.settings,
            read_fallback_urls=self.settings.delta_market_data_fallback_urls,
        )
        self.state: dict[str, SymbolHealth] = {s: SymbolHealth(s) for s in self.symbols}
        self._running = False

    # -------------------------------------------------------------- polling

    def check_once(self) -> dict:
        """One synchronous sweep over every watchlist symbol."""
        now = time.time()
        for symbol in self.symbols:
            h = self.state[symbol]
            candles: list[dict] = []
            ticker: dict = {}
            try:
                end = int(now)
                candles = self.client.get_candles(symbol, resolution=self.resolution, start=end - 30 * 24 * 3600, end=end)
                h.feed_ok = True
                h.error = None
            except Exception as e:  # noqa: BLE001 - surfaced honestly below
                h.feed_ok = False
                h.error = str(e)[:200]
            try:
                ticker = self.client.get_ticker(symbol)
                h.ticker_ok = bool(ticker)
            except Exception as e:  # noqa: BLE001
                h.ticker_ok = False
                h.error = f"{h.error}; ticker: {e}" if h.error else f"ticker: {e}"
            self._apply(symbol, candles, ticker, now)
        return self.snapshot()

    def _apply(self, symbol: str, candles: list[dict], ticker: dict, now: float) -> None:
        h = self.state[symbol]
        h.candles_count = len(candles)
        h.checked_at = int(time.time())
        h.source = "none"
        h.status_reason = None

        last = candles[-1] if candles else {}
        last_time = _num(last.get("time"))
        close = _num(last.get("close")) if candles else None

        if candles:
            h.last_candle_time = int(last_time) if last_time else None
            h.last_candle_age_secs = int(now - last_time) if last_time else None
            h.price = close
            h.price_valid = bool(close is not None and close > 0)
            h.source = "rest"

        if h.candles_count == 0:
            h.status = "unavailable"
            h.status_reason = h.error or "no candles returned"
            h.source = "none"
        elif not h.price_valid:
            h.status = "invalid_price"
            h.status_reason = "last close is missing/zero (rejected as fabricated)"
        elif h.last_candle_age_secs is not None and h.last_candle_age_secs <= self.fresh_secs:
            h.status = "streaming"
        else:
            h.status = "stale"
            h.status_reason = f"last candle age {h.last_candle_age_secs}s > {self.fresh_secs}s"

        if ticker is not None:
            oi = ticker.get("oi_value_usd")
            if oi is None:
                oi = ticker.get("oi_contracts")
            h.open_interest = _num(oi)
            h.oi_missing = h.open_interest is None

            funding = ticker.get("funding_rate_8h")
            if funding is None:
                funding = ticker.get("funding_rate")
            h.funding = _num(funding)
            h.funding_missing = h.funding is None

    # --------------------------------------------------------------- lifecycle

    async def run(self) -> None:
        self._running = True
        while self._running:
            try:
                self.check_once()
            except Exception as e:  # noqa: BLE001
                logger.error("market health sweep failed: %s", e)
            await asyncio.sleep(self.poll_secs)

    async def start(self) -> None:
        self._running = True
        self.check_once()
        asyncio.create_task(self.run())

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------ reads

    def snapshot(self) -> dict:
        rows = [h.to_dict() for h in self.state.values()]
        statuses = {r["status"] for r in rows}
        if statuses == {"streaming"}:
            overall = "streaming"
        elif "streaming" in statuses:
            overall = "degraded"
        elif statuses == {"unavailable"}:
            overall = "unavailable"
        else:
            overall = "degraded"
        return {
            "overall": overall,
            "symbols": rows,
            "poll_secs": self.poll_secs,
            "resolution": self.resolution,
            "fresh_secs": self.fresh_secs,
        }