import asyncio
import json
import logging
import time
from collections import Counter
from collections.abc import Awaitable, Callable

import websockets

logger = logging.getLogger(__name__)

MessageHandler = Callable[[dict], Awaitable[object]]


def parse_ticker_message(message: dict) -> dict | None:
    """Normalize Delta's legacy and current public ticker payloads.

    The legacy private socket used ``v2/ticker`` with flat fields.  The current
    public socket uses ``ticker`` and packs the ticker snapshot in ``d``.  The
    rest of the application consumes one honest, flat representation so a
    transport acknowledgement can never be mistaken for a market tick.
    """
    message_type = message.get("type")
    if message_type not in {"ticker", "v2/ticker"}:
        return None

    if message_type == "ticker":
        entries = message.get("d")
        entry = entries[0] if isinstance(entries, list) and entries and isinstance(entries[0], dict) else {}
        ohlc = entry.get("ohlc")
        close = ohlc[3] if isinstance(ohlc, (list, tuple)) and len(ohlc) >= 4 else None
        oi = entry.get("oi")
        turnover = entry.get("to")
        return {
            "type": "ticker",
            "symbol": message.get("sy") or entry.get("s"),
            "close": close,
            "mark_price": message.get("m") or entry.get("m"),
            "oi_contracts": oi[0] if isinstance(oi, (list, tuple)) and oi else None,
            "oi_change_usd_6h": oi[1] if isinstance(oi, (list, tuple)) and len(oi) > 1 else None,
            "turnover": turnover[0] if isinstance(turnover, (list, tuple)) and turnover else None,
            "turnover_usd": turnover[1] if isinstance(turnover, (list, tuple)) and len(turnover) > 1 else None,
            "timestamp": message.get("ts"),
            "raw": message,
        }

    normalized = dict(message)
    normalized.setdefault("symbol", message.get("sy"))
    normalized.setdefault("timestamp", message.get("ts"))
    return normalized


def parse_funding_message(message: dict) -> dict | None:
    """Normalize legacy flat and current compact funding-rate messages."""
    if message.get("type") != "funding_rate":
        return None
    normalized = dict(message)
    normalized.setdefault("symbol", message.get("sy"))
    if "funding_rate" not in normalized:
        normalized["funding_rate"] = message.get("fr")
    if "funding_rate_8h" not in normalized:
        normalized["funding_rate_8h"] = message.get("fr")
    normalized.setdefault("timestamp", message.get("ts"))
    return normalized


class DeltaWebSocket:
    """Async WebSocket feed for Delta Exchange.

    Connects to the public socket, (re)subscribes on every (re)connect, and
    routes each message to a single async handler. Reconnects with linear
    backoff and survives network drops.
    """

    def __init__(
        self,
        ws_url: str,
        channels: list[dict[str, object]],
        handler: MessageHandler,
        ping_interval: int = 20,
        max_reconnect_failures: int = 20,
    ):
        self.ws_url = ws_url
        self.channels = channels
        self.handler = handler
        self.ping_interval = ping_interval
        self.max_reconnect_failures = max_reconnect_failures
        self._running = False
        self._failures = 0
        self._connected = False
        self._reconnects = 0
        self._last_message_ts: float | None = None
        self._last_processed_ts: float | None = None
        self._last_data_ts: float | None = None
        self._messages_received = 0
        self._messages_processed = 0
        self._data_messages = 0
        self._handler_errors = 0
        self._message_types: Counter[str] = Counter()
        self._connect_ts: float | None = None

    def status(self) -> dict:
        now = time.time()
        return {
            "url": self.ws_url,
            "running": self._running,
            "connected": self._connected,
            "reconnects": self._reconnects,
            "failures": self._failures,
            "last_message_ts": int(self._last_message_ts) if self._last_message_ts else None,
            "last_message_age_secs": round(now - self._last_message_ts, 1) if self._last_message_ts else None,
            "last_processed_ts": int(self._last_processed_ts) if self._last_processed_ts else None,
            "last_processed_age_secs": round(now - self._last_processed_ts, 1) if self._last_processed_ts else None,
            "last_data_ts": int(self._last_data_ts) if self._last_data_ts else None,
            "last_data_age_secs": round(now - self._last_data_ts, 1) if self._last_data_ts else None,
            "messages_received": self._messages_received,
            "messages_processed": self._messages_processed,
            "data_messages": self._data_messages,
            "handler_errors": self._handler_errors,
            "message_types": dict(self._message_types),
            "connected_secs": round(now - self._connect_ts, 1) if self._connect_ts else None,
        }

    def _subscribe_message(self) -> dict:
        return {"type": "subscribe", "payload": {"channels": list(self.channels)}}

    async def _receive_loop(self) -> None:
        async with websockets.connect(self.ws_url, ping_interval=self.ping_interval, max_size=None) as ws:
            self._failures = 0
            self._connected = True
            self._connect_ts = time.time()
            await ws.send(json.dumps(self._subscribe_message()))
            logger.info("connected to %s", self.ws_url)
            while self._running:
                raw = await ws.recv()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    logger.debug("skipping non-JSON message: %s", raw[:100])
                    continue
                if not isinstance(msg, dict):
                    logger.debug("skipping non-object message: %s", raw[:100])
                    continue
                self._last_message_ts = time.time()
                self._messages_received += 1
                message_type = str(msg.get("type") or "unknown")
                self._message_types[message_type] += 1
                try:
                    handled = await self.handler(msg)
                    self._messages_processed += 1
                    self._last_processed_ts = time.time()
                    if handled is True:
                        self._data_messages += 1
                        self._last_data_ts = self._last_processed_ts
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._handler_errors += 1
                    logger.exception("websocket message handler failed for type=%s", message_type)
            self._connected = False

    async def run(self) -> None:
        self._running = True
        backoff = 1
        while self._running:
            try:
                await self._receive_loop()
            except (websockets.ConnectionClosed, OSError, asyncio.TimeoutError) as e:
                self._connected = False
                self._failures += 1
                self._reconnects += 1
                if self._failures > self.max_reconnect_failures:
                    logger.error("too many reconnect failures, giving up")
                    break
                logger.warning("connection lost: %s (retry %d) reconnect in %ds", e, self._failures, backoff)
            finally:
                if self._running:
                    await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def stop(self) -> None:
        self._running = False
        self._connected = False
