"""Soak feed: per-symbol, resolution-aware market-data source for the paper runner.

Reads REAL public market data from Delta (candles + ticker) over REST, with an
optional WebSocket ticker/funding stream. It only ever touches public data:
no authenticated endpoint, no API keys required, no order/account path is
reachable from here.

SoakFeed computes its history window from the actual resolution and shares the
timeframe helpers used by the rest of the app. Semantics that matter (deriving
data_status from the candle feed, websocket/rest/none source, price validity)
are kept identical to MarketData so nothing here invents new behaviour.

When the source is unreachable the feed reports data_status=unavailable and
keeps the last known candles/ticker. It NEVER fabricates a candle and never
fabricates a price: signals downstream are gated on streaming+valid data.
"""

from __future__ import annotations

import asyncio
import logging
import time

from app.config.settings import Settings, get_settings
from app.data.delta_client import DeltaClient
from app.data.websocket import DeltaWebSocket, parse_funding_message, parse_ticker_message
from app.strategy.timeframes import TIMEFRAME_SECONDS, timeframe_seconds

logger = logging.getLogger(__name__)

RESOLUTION_SECS = dict(TIMEFRAME_SECONDS)


class SoakFeed:
    def __init__(
        self,
        symbol: str,
        client: DeltaClient | None = None,
        settings: Settings | None = None,
        resolution: str = "1h",
        candle_buffer: int = 200,
        poll_secs: float = 45.0,
        fresh_secs: float | None = None,
        ws: DeltaWebSocket | None = None,
        enable_ws: bool = False,
    ) -> None:
        self.symbol = symbol.upper()
        self.settings = settings or get_settings()
        self.resolution = resolution
        self.resolution_secs = timeframe_seconds(resolution)
        self.candle_buffer = candle_buffer
        self.poll_secs = poll_secs
        # Freshness default follows the resolution: Delta's candle `time` is the
        # interval OPEN time, and its last row is the still-forming current bar.
        # For 1h candles the newest bar is therefore between 0s and 3600s old,
        # so a fixed 5-minute threshold (inherited from the 5m pipeline) labels
        # everything stale ~92% of the hour and gates the soak out of trading.
        # `fresh_secs` = one bar is the honest "live feed" signal: the feed only
        # turns stale once it misses a full bar. Callers can still override.
        self.fresh_secs = self.resolution_secs if fresh_secs is None else fresh_secs

        if client is None:
            # Public data only: built with the read-only market-data URL and no
            # credentials. Never used for orders/balances/positions.
            client = DeltaClient(
                self.settings.delta_market_data_url,
                settings=self.settings,
                read_fallback_urls=self.settings.delta_market_data_fallback_urls,
            )
        self.client = client

        if ws is None and enable_ws:
            ws = DeltaWebSocket(
                self.settings.delta_ws_url,
                channels=[
                    {"name": "ticker", "symbols": [self.symbol]},
                    {"name": "funding_rate", "symbols": [self.symbol]},
                ],
                handler=self._on_message,
            )
        self.ws = ws

        self.candles: list[dict] = []
        self.ticker: dict = {}
        self.funding_rate: dict = {}
        self._ws_ticker: dict = {}
        self._rest_ticker: dict = {}
        self._ticker_source = "none"
        self._last_data_ts: float | None = None
        self._last_ws_ts: float | None = None
        self._last_ws_ticker_ts: float | None = None
        self._last_rest_ts: float | None = None
        self._ws_ticker_messages = 0
        self._ws_funding_messages = 0
        self._ws_parse_errors = 0
        self._rest_polls = 0
        self._rest_failures = 0
        self._fallback_used = False
        self.last_poll_error: str | None = None
        self._running = False
        self._tasks: list[asyncio.Task] = []

    # ------------------------------------------------------------- ws handler

    def _symbol_matches(self, payload: dict) -> bool:
        message_symbol = str(payload.get("symbol") or "").strip().upper()
        return not message_symbol or message_symbol == self.symbol

    def _select_ticker(self, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        if (
            self._ws_ticker
            and self._last_ws_ticker_ts is not None
            and now - self._last_ws_ticker_ts <= self.fresh_secs
        ):
            self.ticker = dict(self._ws_ticker)
            self._ticker_source = "websocket"
        elif self._rest_ticker:
            self.ticker = dict(self._rest_ticker)
            self._ticker_source = "rest"
        elif self._ws_ticker:
            # Keep the last known price for observability, but its source is not
            # eligible to make a stale feed appear websocket-fresh.
            self.ticker = dict(self._ws_ticker)
            self._ticker_source = "websocket_stale"

    async def _on_message(self, msg: dict) -> bool:
        now = time.time()
        ticker = parse_ticker_message(msg)
        if ticker is not None:
            if not self._symbol_matches(ticker) or ticker.get("close") is None:
                self._ws_parse_errors += 1
                return False
            self._ws_ticker = dict(ticker)
            self._last_ws_ticker_ts = now
            self._last_ws_ts = now
            self._last_data_ts = now
            self._ws_ticker_messages += 1
            self._select_ticker(now)
            return True

        funding = parse_funding_message(msg)
        if funding is not None:
            if not self._symbol_matches(funding):
                self._ws_parse_errors += 1
                return False
            self.funding_rate = dict(funding)
            self._last_ws_ts = now
            self._last_data_ts = now
            self._ws_funding_messages += 1
            return True

        return False

    # ------------------------------------------------------------ sync fetch

    def poll(self) -> None:
        """Fetch the latest candles + ticker synchronously (public GETs only).

        Any exception propagates untouched so the caller decides what to do;
        `last_poll_error` is recorded so snapshots can report it honestly.
        """
        self.last_poll_error = None
        self._rest_polls += 1
        self._fallback_used = False
        try:
            end = int(time.time())
            start = end - self.candle_buffer * self.resolution_secs
            candles = self.client.get_candles(self.symbol, resolution=self.resolution, start=start, end=end)
            if candles:
                self.candles = list(candles[-self.candle_buffer:])
            candle_meta = dict(getattr(self.client, "last_read_meta", {}) or {})
            tk = self.client.get_ticker(self.symbol)
            if tk:
                self._rest_ticker = dict(tk)
                self._last_rest_ts = time.time()
            read_meta = dict(getattr(self.client, "last_read_meta", {}) or {})
            self._fallback_used = bool(candle_meta.get("fallback_used") or read_meta.get("fallback_used"))
            self._select_ticker()
            self._last_data_ts = time.time()
        except Exception as e:
            self._rest_failures += 1
            self.last_poll_error = str(e)
            raise

    # -------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        tasks = [asyncio.create_task(self._poll_loop())]
        if self.ws is not None:
            tasks.append(asyncio.create_task(self.ws.run()))
        self._tasks = tasks

    async def stop(self) -> None:
        self._running = False
        if self.ws is not None:
            await self.ws.stop()
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await asyncio.to_thread(self.poll)
            except Exception as e:
                self.last_poll_error = str(e)
                logger.warning("soak feed %s poll failed: %s", self.symbol, e)
            await asyncio.sleep(self.poll_secs)

    # ---------------------------------------------------------------- status

    def snapshot(self) -> dict:
        """Per-feed health snapshot following MarketData's exact semantics."""
        now = time.time()

        if not self.candles:
            data_status = "unavailable"
        elif (now - float(self.candles[-1]["time"])) <= self.fresh_secs:
            data_status = "streaming"
        else:
            data_status = "stale"

        last_candle_time = int(self.candles[-1]["time"]) if self.candles else None
        last_candle_age_secs = round(now - float(self.candles[-1]["time"])) if self.candles else None

        if self.ws is not None:
            ws_status = dict(self.ws.status())
        else:
            ws_status = {"running": False, "connected": False, "reconnects": 0, "failures": 0, "last_message_age_secs": None}
        ws_status.update(
            {
                "last_ticker_ts": int(self._last_ws_ticker_ts) if self._last_ws_ticker_ts else None,
                "last_ticker_age_secs": round(now - self._last_ws_ticker_ts, 1) if self._last_ws_ticker_ts else None,
                "ticker_messages": self._ws_ticker_messages,
                "funding_messages": self._ws_funding_messages,
                "parse_errors": self._ws_parse_errors,
            }
        )
        ticker_age = ws_status["last_ticker_age_secs"]
        # Keep old injected WS test doubles working without allowing the real
        # DeltaWebSocket's heartbeat/ack timestamp to count as ticker data.
        if ticker_age is None and "messages_received" not in ws_status:
            ticker_age = ws_status.get("last_message_age_secs")
        ws_fresh = (
            ws_status["connected"]
            and ticker_age is not None
            and ticker_age <= self.fresh_secs
        )
        if data_status == "streaming" and ws_fresh:
            data_source = "websocket"
        elif data_status == "streaming":
            data_source = "rest"
        elif data_status == "stale":
            data_source = "rest"
        else:
            data_source = "none"

        ticker_price = self.ticker.get("close")
        try:
            ticker_price = float(ticker_price)
        except (TypeError, ValueError):
            ticker_price = None
        candle_price = float(self.candles[-1]["close"]) if self.candles else None
        price = ticker_price if (ticker_price is not None and ticker_price > 0) else candle_price
        price_valid = price is not None and price > 0
        price_source = "ticker" if (ticker_price is not None and ticker_price > 0) else ("candle" if price_valid else None)

        oi = self.ticker.get("oi_contracts")
        if oi is None:
            oi = self.ticker.get("oi_value_usd")
        funding = self.funding_rate.get("funding_rate") if self.funding_rate else None
        if funding is None:
            funding = self.ticker.get("funding_rate")
        if funding is None:
            funding = self.ticker.get("funding_rate_8h")

        return {
            "symbol": self.symbol,
            "price": price,
            "price_valid": price_valid,
            "price_source": price_source,
            "candles": list(self.candles),
            "candles_count": len(self.candles),
            "last_candle_time": last_candle_time,
            "last_candle_age_secs": last_candle_age_secs,
            "data_status": data_status,
            "data_source": data_source,
            "ticker_source": self._ticker_source,
            "last_ws_message_age_secs": ws_status.get("last_message_age_secs"),
            "last_ws_ticker_age_secs": ticker_age,
            "last_rest_update_age_secs": round(now - self._last_rest_ts, 1) if self._last_rest_ts else None,
            "last_transport_age_secs": ticker_age if ws_fresh else (round(now - self._last_rest_ts, 1) if self._last_rest_ts else None),
            "rest_polls": self._rest_polls,
            "rest_failures": self._rest_failures,
            "fallback_used": self._fallback_used,
            "ws_status": ws_status,
            "open_interest": oi,
            "funding_rate": funding,
            "last_error": self.last_poll_error,
            "updated_at": now,
        }
