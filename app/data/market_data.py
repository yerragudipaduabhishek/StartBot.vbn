import asyncio
import logging
import threading
import time

from app.config.settings import Settings, get_settings
from app.data.delta_client import DeltaClient
from app.data.websocket import DeltaWebSocket, parse_funding_message, parse_ticker_message
from app.strategy.timeframes import timeframe_seconds

logger = logging.getLogger(__name__)


class MarketData:
    """Live BTC market state fed by the Delta WebSocket + REST candle polling.

    Holds the latest ticker snapshot, funding rate, and a rolling candle
    buffer. Designed to be run as a background service the agents read from.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        symbol: str | None = None,
        resolution: str | None = None,
        candle_poll_secs: float = 45.0,
        candle_buffer: int = 60,
        client: DeltaClient | None = None,
    ):
        self.settings = settings or get_settings()
        self.symbol = symbol or self.settings.trading_symbol
        self.resolution = resolution or self.settings.trading_timeframe
        self.candle_poll_secs = candle_poll_secs
        self.candle_buffer = candle_buffer
        self.resolution_secs = timeframe_seconds(self.resolution)
        # A closed 1h candle can legitimately be ~1h old. Freshness follows the
        # configured resolution instead of the old hard-coded 5m window.
        self._fresh_secs = float(self.resolution_secs)

        self.client = client or DeltaClient(
            self.settings.delta_market_data_url,
            settings=self.settings,
            read_fallback_urls=self.settings.delta_market_data_fallback_urls,
        )
        self.product: dict = {}
        self.ticker: dict = {}
        self.funding_rate: dict = {}
        self._ws_ticker: dict = {}
        self._rest_ticker: dict = {}
        self._ticker_source = "none"
        self.candles: list[dict] = []
        self._lock = threading.Lock()
        self._running = False
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
        self._last_poll_error: str | None = None
        self._rest_async_lock: asyncio.Lock | None = None

        self._ws = DeltaWebSocket(
            self.settings.delta_ws_url,
            channels=[
                {"name": "ticker", "symbols": [self.symbol]},
                {"name": "funding_rate", "symbols": [self.symbol]},
            ],
            handler=self._on_message,
        )

    # ------------------------------------------------------------- ws handler

    def _select_ticker(self, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        if (
            self._ws_ticker
            and self._last_ws_ticker_ts is not None
            and now - self._last_ws_ticker_ts <= self._fresh_secs
        ):
            self.ticker = dict(self._ws_ticker)
            self._ticker_source = "websocket"
        elif self._rest_ticker:
            self.ticker = dict(self._rest_ticker)
            self._ticker_source = "rest"
        elif self._ws_ticker:
            self.ticker = dict(self._ws_ticker)
            self._ticker_source = "websocket_stale"

    async def _on_message(self, msg: dict) -> bool:
        now = time.time()
        ticker = parse_ticker_message(msg)
        if ticker is not None:
            symbol = str(ticker.get("symbol") or "").strip().upper()
            if (symbol and symbol != self.symbol.upper()) or ticker.get("close") is None:
                self._ws_parse_errors += 1
                return False
            with self._lock:
                self._ws_ticker = dict(ticker)
                self._last_data_ts = now
                self._last_ws_ts = now
                self._last_ws_ticker_ts = now
                self._ws_ticker_messages += 1
                self._select_ticker(now)
            return True

        funding = parse_funding_message(msg)
        if funding is None:
            return False
        symbol = str(funding.get("symbol") or "").strip().upper()
        if symbol and symbol != self.symbol.upper():
            self._ws_parse_errors += 1
            return False
        with self._lock:
            self._last_data_ts = now
            self._last_ws_ts = now
            self.funding_rate = dict(funding)
            self._ws_funding_messages += 1
        return True

    async def _rest_call(self, func):
        if self._rest_async_lock is None:
            self._rest_async_lock = asyncio.Lock()
        async with self._rest_async_lock:
            return await asyncio.to_thread(func)

    # ------------------------------------------------------------ candle feed

    async def _candle_poller(self) -> None:
        while self._running:
            try:
                end = int(time.time())
                start = end - (self.candle_buffer * self.resolution_secs)
                candles = await self._rest_call(
                    lambda: self.client.get_candles(self.symbol, resolution=self.resolution, start=start, end=end)
                )
                with self._lock:
                    self._rest_polls += 1
                    if candles:
                        self.candles = list(candles[-self.candle_buffer :])
                        self._last_data_ts = time.time()
                    read_meta = dict(getattr(self.client, "last_read_meta", {}) or {})
                    self._fallback_used = bool(read_meta.get("fallback_used"))
                    self._last_poll_error = None
            except Exception as e:
                with self._lock:
                    self._rest_failures += 1
                    self._last_poll_error = str(e)
                logger.warning("candle poll failed: %s", e)
            await asyncio.sleep(self.candle_poll_secs)

    async def _ticker_poller(self) -> None:
        """REST ticker refresh so price/OI/funding are available even when the
        websocket feed is down or empty (read-only; never an order path)."""
        while self._running:
            try:
                tk = await self._rest_call(lambda: self.client.get_ticker(self.symbol))
                with self._lock:
                    self._rest_polls += 1
                    if tk:
                        self._rest_ticker = dict(tk)
                        self._last_rest_ts = time.time()
                        self._select_ticker()
                    read_meta = dict(getattr(self.client, "last_read_meta", {}) or {})
                    self._fallback_used = self._fallback_used or bool(read_meta.get("fallback_used"))
            except Exception as e:
                with self._lock:
                    self._rest_failures += 1
                logger.warning("ticker poll failed: %s", e)
            await asyncio.sleep(15.0)

    # -------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        self._running = True
        self.product = await asyncio.to_thread(self.client.find_product, self.symbol)
        logger.info("market data: symbol=%s product_id=%s resolution=%s", self.symbol, self.product.get("id"), self.resolution)
        asyncio.create_task(self._candle_poller())
        asyncio.create_task(self._ticker_poller())
        await self._ws.run()

    async def stop(self) -> None:
        self._running = False
        await self._ws.stop()

    # ------------------------------------------------------------------ reads

    def snapshot(self) -> dict:
        with self._lock:
            now = time.time()

            # Data status is derived strictly from the candle feed: the chart
            # (and the fresh/streaming concept) depends on candles, not on an
            # isolated ticker/funding message that may arrive while candles 502.
            if not self.candles:
                data_status = "unavailable"
            elif now - float(self.candles[-1]["time"]) <= self._fresh_secs:
                data_status = "streaming"
            else:
                data_status = "stale"

            last_update_unix: int | None = int(self.candles[-1]["time"]) if self.candles else None
            last_candle_age_secs = round(now - float(self.candles[-1]["time"])) if self.candles else None

            # Which pipeline actually moved? WebSocket messages are freshest;
            # otherwise REST candle polling. 'none' means no data at all.
            ws = dict(self._ws.status())
            ws["last_ticker_age_secs"] = round(now - self._last_ws_ticker_ts, 1) if self._last_ws_ticker_ts else None
            ws["ticker_messages"] = self._ws_ticker_messages
            ws["funding_messages"] = self._ws_funding_messages
            ws["parse_errors"] = self._ws_parse_errors
            ticker_age = ws["last_ticker_age_secs"]
            if ticker_age is None and "messages_received" not in ws:
                ticker_age = ws.get("last_message_age_secs")
            ws_fresh = bool(ws["connected"]) and ticker_age is not None and ticker_age <= self._fresh_secs
            if data_status == "streaming" and ws_fresh:
                data_source = "websocket"
            elif data_status == "streaming":
                data_source = "rest"
            elif data_status == "stale":
                data_source = "rest"
            else:
                data_source = "none"

            raw_price = self.ticker.get("close")
            try:
                price = float(raw_price) if raw_price is not None else None
            except (TypeError, ValueError):
                price = None
            price_valid = price is not None and price > 0
            read_meta = dict(getattr(self.client, "last_read_meta", {}) or {})
            fallback_used = self._fallback_used or bool(read_meta.get("fallback_used"))
            transport = "websocket" if data_source == "websocket" else ("rest" if data_source != "none" else "none")
            provenance = {
                "source": "delta",
                "venue": read_meta.get("venue") or "unknown",
                "transport": transport,
                "endpoint": read_meta.get("endpoint") or "/v2/history/candles",
                "fallback_used": fallback_used,
                "fetched_at": read_meta.get("fetched_at"),
            }

            return {
                "symbol": self.symbol,
                "product_id": self.product.get("id"),
                "price": price,
                "mark_price": self.ticker.get("mark_price"),
                "volume": self.ticker.get("volume"),
                "open_interest": self.ticker.get("oi_contracts"),
                "oi_value_usd": self.ticker.get("oi_value_usd"),
                "funding_rate": self.funding_rate.get("funding_rate"),
                "funding_8h": self.funding_rate.get("funding_rate_8h"),
                "candles": list(self.candles),
                "candles_count": len(self.candles),
                "data_status": data_status,
                "last_update_unix": last_update_unix,
                "last_candle_age_secs": last_candle_age_secs,
                "data_source": data_source,
                "ticker_source": self._ticker_source,
                "last_ws_message_age_secs": ws.get("last_message_age_secs"),
                "last_ws_ticker_age_secs": ticker_age,
                "last_rest_update_age_secs": round(now - self._last_rest_ts, 1) if self._last_rest_ts else None,
                "last_transport_age_secs": ticker_age if ws_fresh else (round(now - self._last_rest_ts, 1) if self._last_rest_ts else None),
                "rest_polls": self._rest_polls,
                "rest_failures": self._rest_failures,
                "data_provenance": provenance,
                "fallback_used": fallback_used,
                "price_valid": price_valid,
                "ws_status": ws,
                "last_error": self._last_poll_error,
                "updated_at": self.ticker.get("timestamp"),
            }
