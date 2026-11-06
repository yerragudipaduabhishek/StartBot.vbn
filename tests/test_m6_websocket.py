"""Milestone 6 — WebSocket resilience: status telemetry, auto-reconnect,
re-subscribe, reconnect-failure budget, and REST fallback inside MarketData.

Uses asyncio.run (no pytest-asyncio dependency) with a stubbed connect() so the
socket never touches the network.
"""

import asyncio
import json
import time

import websockets

from app.config.settings import get_settings
from app.data.market_data import MarketData
from app.data.websocket import DeltaWebSocket, parse_funding_message, parse_ticker_message
from app.soak.feed import SoakFeed
from tests.m3_fixtures import make_candles


async def _noop_sleep(_secs):
    return None


async def _noop_handler(_msg):
    return None


def _fresh_now(n=60, base=50000.0) -> list[dict]:
    candles = make_candles(n=n, base=base)
    candles[-1]["time"] = int(time.time())  # guarantee freshness regardless of hour
    return candles


def test_status_before_start():
    ws = DeltaWebSocket("wss://fake", channels=[{"name": "v2/ticker", "symbols": ["BTCUSDT"]}], handler=_noop_handler)
    s = ws.status()
    assert s["running"] is False
    assert s["connected"] is False
    assert s["reconnects"] == 0
    assert s["failures"] == 0
    assert s["last_message_ts"] is None
    assert s["last_message_age_secs"] is None


def test_reconnect_and_resubscribe(monkeypatch):
    sent: list[str] = []
    stall = asyncio.Event()

    class FakeConn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def send(self, raw):
            sent.append(raw)

        async def recv(self):
            if len(sent) <= 1:
                raise websockets.exceptions.ConnectionClosed(1006, None)
            await stall.wait()
            return json.dumps({"type": "ticker"})

    fake = FakeConn()

    def fake_connect(url, **kw):
        return fake

    monkeypatch.setattr(websockets, "connect", fake_connect)
    ws = DeltaWebSocket(
        "wss://fake",
        channels=[{"name": "v2/ticker", "symbols": ["BTCUSDT"]}],
        handler=_noop_handler,
        max_reconnect_failures=5,
    )

    async def scenario():
        task = asyncio.create_task(ws.run())
        for _ in range(600):
            if ws._reconnects >= 1 and ws._connected:
                break
            await asyncio.sleep(0.005)
        status = ws.status()
        await ws.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return status

    status = asyncio.run(scenario())
    assert status["connected"] is True
    assert status["reconnects"] == 1
    # resubscribe happened: one subscribe per connect attempt
    assert len(sent) == 2
    subscribe = json.loads(sent[0])
    assert subscribe["type"] == "subscribe"


def test_gives_up_after_reconnect_budget(monkeypatch):
    async def noop(_secs):
        return None

    monkeypatch.setattr(asyncio, "sleep", noop)

    def always_broken(url, **kw):
        raise OSError("connection refused")

    monkeypatch.setattr(websockets, "connect", always_broken)
    ws = DeltaWebSocket(
        "wss://fake",
        channels=[{"name": "v2/ticker", "symbols": ["BTCUSDT"]}],
        handler=_noop_handler,
        max_reconnect_failures=1,
    )

    async def scenario():
        await ws.run()  # returns once budget exhausted
        return ws.status()

    status = asyncio.run(scenario())
    assert status["failures"] == 2  # budget exceeded -> gave up
    assert status["connected"] is False


def test_ws_failure_falls_back_to_rest_for_source(monkeypatch):
    md = MarketData(settings=get_settings(), client=_DummyClient())
    md.ticker = {"close": 50123.0}
    md.candles = _fresh_now()

    class FakeWS:
        def status(self):
            return {
                "url": "wss://fake",
                "running": True,
                "connected": False,
                "reconnects": 3,
                "failures": 1,
                "last_message_ts": None,
                "last_message_age_secs": None,
                "connected_secs": None,
            }

    monkeypatch.setattr(md, "_ws", FakeWS())
    snap = md.snapshot()
    assert snap["data_status"] == "streaming"
    assert snap["data_source"] == "rest"
    assert snap["price_valid"] is True
    assert snap["price"] == 50123.0
    assert snap["ws_status"]["connected"] is False


def test_ws_fresh_gives_websocket_source(monkeypatch):
    md = MarketData(settings=get_settings(), client=_DummyClient())
    md.ticker = {"close": 50123.0}
    md.candles = _fresh_now()

    class FakeWS:
        def status(self):
            return {
                "url": "wss://fake",
                "running": True,
                "connected": True,
                "reconnects": 0,
                "failures": 0,
                "last_message_ts": int(time.time()),
                "last_message_age_secs": 1.5,
                "connected_secs": 12.0,
            }

    monkeypatch.setattr(md, "_ws", FakeWS())
    snap = md.snapshot()
    assert snap["data_source"] == "websocket"
    assert snap["ws_status"]["connected"] is True
    assert snap["ws_status"]["last_message_age_secs"] < 5


def test_no_data_anywhere_is_none_source(monkeypatch):
    md = MarketData(settings=get_settings(), client=_DummyClient())
    snap = md.snapshot()
    assert snap["data_status"] == "unavailable"
    assert snap["data_source"] == "none"
    assert snap["price_valid"] is False
    assert snap["ws_status"]["connected"] is False


def test_current_compact_ticker_is_parsed_as_data_not_a_transport_ack():
    message = {
        "type": "ticker",
        "sy": "BTCUSDT",
        "ts": 1760000000000,
        "m": 50125.5,
        "d": [{"s": "BTCUSDT", "ohlc": [50000, 50200, 49900, 50100.25], "oi": [1234, 5], "to": [10, 20]}],
    }
    normalized = parse_ticker_message(message)
    assert normalized is not None
    assert normalized["symbol"] == "BTCUSDT"
    assert normalized["close"] == 50100.25
    assert normalized["oi_contracts"] == 1234
    assert normalized["turnover_usd"] == 20

    funding = parse_funding_message({"type": "funding_rate", "sy": "BTCUSDT", "fr": 0.0001, "ts": 1760000000000})
    assert funding is not None
    assert funding["symbol"] == "BTCUSDT"
    assert funding["funding_rate_8h"] == 0.0001


def test_soak_feed_ignores_subscription_ack_and_counts_real_ticker():
    class ConnectedWs:
        def status(self):
            return {
                "running": True,
                "connected": True,
                "reconnects": 0,
                "failures": 0,
                "last_message_age_secs": 0.1,
                "messages_received": 1,
            }

    feed = SoakFeed("BTCUSDT", client=_DummyClient(), enable_ws=False, ws=ConnectedWs())
    feed.candles = _fresh_now()

    async def scenario():
        assert await feed._on_message({"type": "subscriptions", "channels": []}) is False
        assert await feed._on_message(
            {
                "type": "ticker",
                "sy": "BTCUSDT",
                "ts": 1760000000000,
                "d": [{"ohlc": [1, 2, 3, 50123.0], "oi": [99, 0], "to": [1, 2]}],
            }
        ) is True

    asyncio.run(scenario())
    snap = feed.snapshot()
    assert snap["data_source"] == "websocket"
    assert snap["ticker_source"] == "websocket"
    assert snap["price"] == 50123.0
    assert snap["ws_status"]["ticker_messages"] == 1
    assert snap["ws_status"]["funding_messages"] == 0


def test_market_data_manager_uses_the_same_compact_ticker_parser():
    md = MarketData(settings=get_settings(), client=_DummyClient())
    md.candles = _fresh_now()

    class ConnectedWs:
        def status(self):
            return {
                "running": True,
                "connected": True,
                "reconnects": 0,
                "failures": 0,
                "last_message_age_secs": 0.1,
                "messages_received": 1,
            }

    md._ws = ConnectedWs()

    async def receive():
        return await md._on_message(
            {
                "type": "ticker",
                "sy": "BTCUSDT",
                "ts": 1760000000000,
                "d": [{"ohlc": [1, 2, 3, 50200.0], "oi": [101, 0], "to": [1, 2]}],
            }
        )

    assert asyncio.run(receive()) is True
    snap = md.snapshot()
    assert snap["data_source"] == "websocket"
    assert snap["price"] == 50200.0
    assert snap["ws_status"]["ticker_messages"] == 1


def test_rest_refresh_cannot_overwrite_a_fresh_websocket_ticker():
    class TickerClient(_DummyClient):
        def get_ticker(self, symbol):
            return {"close": 49000.0}

    class ConnectedWs:
        def status(self):
            return {
                "running": True,
                "connected": True,
                "reconnects": 0,
                "failures": 0,
                "last_message_age_secs": 0.1,
                "messages_received": 1,
            }

    feed = SoakFeed("BTCUSDT", client=TickerClient(), enable_ws=False, ws=ConnectedWs())
    feed.candles = _fresh_now()
    feed.poll()

    async def receive():
        return await feed._on_message(
            {
                "type": "ticker",
                "sy": "BTCUSDT",
                "ts": 1760000000000,
                "d": [{"ohlc": [1, 2, 3, 50123.0]}],
            }
        )

    assert asyncio.run(receive()) is True
    feed.poll()  # REST still returns the older price.
    snap = feed.snapshot()
    assert snap["price"] == 50123.0
    assert snap["ticker_source"] == "websocket"
    assert snap["data_source"] == "websocket"


def test_soak_poll_does_not_block_the_async_event_loop():
    class SlowClient(_DummyClient):
        def get_candles(self, *args, **kwargs):
            time.sleep(0.15)
            return _fresh_now()

        def get_ticker(self, symbol):
            time.sleep(0.15)
            return {"close": 50123.0}

    feed = SoakFeed("BTCUSDT", client=SlowClient(), enable_ws=False, poll_secs=60)

    async def scenario():
        feed._running = True
        poll_task = asyncio.create_task(feed._poll_loop())
        heartbeat = asyncio.create_task(asyncio.sleep(0.02))
        try:
            await asyncio.wait_for(heartbeat, timeout=0.08)
        finally:
            feed._running = False
            poll_task.cancel()
            await asyncio.gather(poll_task, return_exceptions=True)

    asyncio.run(scenario())


class _DummyClient:
    def find_product(self, symbol):
        return {"id": 1, "symbol": symbol, "contract_value": "0.001"}

    def get_candles(self, symbol, resolution="1h", start=None, end=None):
        return []

    def get_ticker(self, symbol):
        return {}
