"""Authenticated testnet order feed (WebSocket).

Wraps the raw socket with:
* reconnection + resubscription on every drop (linear -> capped backoff),
* an `on_reconnect` hook that triggers REST reconciliation so order/position
  state is never missed because a WS blowout swallowed an event (req #16),
* routing of order vs position messages to separate callbacks.

Defaults to REST-pull reconciliation on reconnect; the feed is an accelerator,
never the source of truth.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from collections.abc import Awaitable, Callable
from urllib.parse import urlencode, urlsplit, urlunsplit

import websockets

logger = logging.getLogger(__name__)

MessageHandler = Callable[[dict], Awaitable[None] | None]

DEFAULT_CHANNELS = ["v2/user/orders", "v2/user/positions"]


def ws_auth_params(api_key: str, api_secret: str, path: str = "/") -> dict:
    """Delta authenticates private sockets via query params; the signature uses
    the same scheme as REST over `GET <ts> <path>` with an empty body."""
    ts = str(int(time.time()))
    message = "GET" + ts + path
    sig = hmac.new(api_secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return {"api-key": api_key, "timestamp": ts, "signature": sig}


class TestnetOrderFeed:
    def __init__(
        self,
        ws_url: str,
        *,
        api_key: str = "",
        api_secret: str = "",
        channels: list[str] | None = None,
        on_order: MessageHandler | None = None,
        on_position: MessageHandler | None = None,
        on_reconnect: Callable[[], Awaitable[None] | None] | None = None,
        ping_interval: int = 20,
        max_reconnect_failures: int = 20,
        max_backoff: int = 30,
    ) -> None:
        self.ws_url = ws_url
        self.api_key = api_key
        self.api_secret = api_secret
        self.channels = channels or DEFAULT_CHANNELS
        self.on_order = on_order
        self.on_position = on_position
        self.on_reconnect = on_reconnect
        self.ping_interval = ping_interval
        self.max_reconnect_failures = max_reconnect_failures
        self.max_backoff = max_backoff
        self._running = False
        self._failures = 0

    # ------------------------------------------------------------- connection

    def _connect_url(self) -> str:
        parts = urlsplit(self.ws_url)
        query = {}
        if parts.query:
            for pair in parts.query.split("&"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    query[k] = v
        query.update(ws_auth_params(self.api_key, self.api_secret, parts.path or "/"))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

    def _subscribe_message(self) -> str:
        return json.dumps({"type": "subscribe", "payload": {"channels": self.channels}})

    async def _route(self, msg: dict) -> None:
        channel = str(msg.get("channel") or msg.get("type") or "")
        if any(k in channel for k in ("order", "Order")) and self.on_order:
            result = self.on_order(msg)
            if asyncio.iscoroutine(result):
                await result
        elif any(k in channel for k in ("position", "Position")) and self.on_position:
            result = self.on_position(msg)
            if asyncio.iscoroutine(result):
                await result

    async def _receive_loop(self) -> None:
        async with websockets.connect(self._connect_url(), ping_interval=self.ping_interval, max_size=None) as ws:
            self._failures = 0
            await ws.send(self._subscribe_message())
            logger.info("order feed connected to %s (channels=%s)", self.ws_url, self.channels)
            while self._running:
                raw = await ws.recv()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    logger.debug("order feed: skipping non-JSON: %s", str(raw)[:100])
                    continue
                asyncio.create_task(self._route(msg))

    # ------------------------------------------------------------------ loop

    async def run(self) -> None:
        self._running = True
        backoff = 1
        while self._running:
            try:
                await self._receive_loop()
            except (websockets.ConnectionClosed, OSError, asyncio.TimeoutError) as e:
                self._failures += 1
                if self._failures > self.max_reconnect_failures:
                    logger.error("order feed: too many reconnect failures, giving up")
                    break
                logger.warning("order feed connection lost: %s (retry %d, reconnect in %ds)", e, self._failures, backoff)
                # Notify so the caller can REST-reconcile what we may have missed.
                if self.on_reconnect is not None:
                    try:
                        result = self.on_reconnect()
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception:
                        logger.exception("order feed: on_reconnect hook failed")
            finally:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.max_backoff)

    async def stop(self) -> None:
        self._running = False