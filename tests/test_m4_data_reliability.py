"""Milestone 4 — Market Data Reliability.

The Delta *testnet* REST host intermittently returns 502 on the public
products/candles routes while the public production mirror serves real data.
DeltaClient therefore fans public reads (products/tickers/candles) across an
ordered list of read-only hosts, failing fast on 5xx and moving to the next
host. The order/account path must *never* consult the fallback hosts.

These tests use a scripted fake session — no network traffic.
"""

import time

import pytest

from app.config.settings import PROD_BASE, Settings
from app.data.delta_client import DeltaClient
from app.data.market_data import MarketData
from tests.m3_fixtures import make_ticker

PRIMARY = "https://testnet-api.delta.exchange"
MIRROR = "https://api.delta.exchange"


class _Resp:
    def __init__(self, payload, status=200, content_type="application/json"):
        self._payload = payload
        self.status_code = status
        self.content = self._payload if isinstance(self._payload, bytes) else b"{}"
        self.headers = {"content-type": content_type}

    def json(self):
        return self._payload


class FakeSession:
    """Scripted session: maps full URL -> handler that returns (response, exc)."""

    def __init__(self):
        self.handlers: dict[str, tuple[object, Exception | None]] = {}
        self.urls: list[str] = []
        self.bodies: list[object] = []

    def add(self, url: str, payload=None, status=200, exc: Exception | None = None, content_type="application/json"):
        self.handlers[url] = (_Resp(payload, status, content_type), exc)

    def request(self, method, url, headers=None, data=None, timeout=None):
        self.urls.append(url)
        self.bodies.append(data)
        resp, exc = self.handlers.get(url.split("?")[0], (_Resp({"error": "no route"}, 404), None))
        if exc is not None:
            raise exc
        return resp


def _paths(urls: list[str]) -> list[str]:
    return [u.split("?")[0] for u in urls]


def _client(session: FakeSession, read_fallback_urls=(MIRROR,), retries=3, with_keys=False) -> DeltaClient:
    c = DeltaClient(
        PRIMARY,
        api_key=("k" if with_keys else ""),
        api_secret=("s" if with_keys else ""),
        retries=retries,
        read_fallback_urls=read_fallback_urls,
    )
    c.session = session  # type: ignore[assignment]
    return c


CANDLES_URL = PRIMARY + "/v2/history/candles"
MIRROR_CANDLES_URL = MIRROR + "/v2/history/candles"
PRODUCTS_URL = PRIMARY + "/v2/products"


# ------------------------------------------------------------------ settings


def test_settings_fallback_urls_testnet_mode():
    s = Settings()
    assert s.testnet_mode is True
    assert s.delta_market_data_fallback_urls == (PROD_BASE["global"],)


def test_settings_fallback_urls_empty_in_live_mode():
    s = Settings(testnet_mode=False)
    assert s.delta_market_data_fallback_urls == ()


def test_settings_fallback_urls_empty_with_override():
    s = Settings(testnet_mode=True, delta_data_url_override="https://primary.example")
    assert s.delta_market_data_fallback_urls == ()

# ------------------------------------------------------------- candle fallback


def test_candles_falls_back_on_5xx():
    sess = FakeSession()
    sess.add(CANDLES_URL, {"result": []}, status=502)  # nginx 502 -> fail fast
    sess.add(MIRROR_CANDLES_URL, {"result": [{"time": 1, "close": 1}]})
    c = _client(sess)

    out = c.get_candles("BTCUSDT", resolution="1h")

    assert out == [{"time": 1, "close": 1}]
    assert _paths(sess.urls) == [CANDLES_URL, MIRROR_CANDLES_URL]
    # fail-fast: only one attempt against the broken primary (retries=3 unused)
    assert _paths(sess.urls).count(CANDLES_URL) == 1


def test_candles_falls_back_on_connection_error():
    sess = FakeSession()
    sess.add(CANDLES_URL, exc=ConnectionError("refused"))
    sess.add(MIRROR_CANDLES_URL, {"result": [{"time": 1, "close": 1}]})
    c = _client(sess, retries=2)

    out = c.get_candles("BTCUSDT", resolution="1h")

    assert out == [{"time": 1, "close": 1}]
    assert _paths(sess.urls).count(CANDLES_URL) == 2  # exhausted retries on primary
    assert _paths(sess.urls)[-1] == MIRROR_CANDLES_URL


def test_candles_falls_back_on_timeout():
    sess = FakeSession()
    sess.add(CANDLES_URL, exc=TimeoutError("timed out"))
    sess.add(MIRROR_CANDLES_URL, {"result": [{"time": 1, "close": 1}]})
    c = _client(sess, retries=1)

    out = c.get_candles("BTCUSDT", resolution="1h")

    assert out == [{"time": 1, "close": 1}]
    assert _paths(sess.urls)[-1] == MIRROR_CANDLES_URL


def test_candles_empty_result_is_not_an_error():
    sess = FakeSession()
    sess.add(CANDLES_URL, {"result": []})
    c = _client(sess)

    assert c.get_candles("BTCUSDT", resolution="1h") == []
    assert _paths(sess.urls) == [CANDLES_URL]


def test_candles_uses_all_hosts_then_raises():
    sess = FakeSession()
    sess.add(CANDLES_URL, {"result": []}, status=500)
    sess.add(MIRROR_CANDLES_URL, {"result": []}, status=503)
    c = _client(sess)

    with pytest.raises(RuntimeError, match="Delta request failed"):
        c.get_candles("BTCUSDT", resolution="1h")
    assert _paths(sess.urls) == [CANDLES_URL, MIRROR_CANDLES_URL]


def test_get_products_falls_back_on_5xx():
    sess = FakeSession()
    sess.add(PRODUCTS_URL, {"result": []}, status=502)
    sess.add(MIRROR + "/v2/products", {"result": [{"symbol": "BTCUSDT", "contract_type": "perpetual_futures"}]})
    c = _client(sess)

    out = c.get_products()

    assert out == [{"symbol": "BTCUSDT", "contract_type": "perpetual_futures"}]


def test_ticker_falls_back_on_5xx():
    sess = FakeSession()
    sess.add(PRIMARY + "/v2/tickers/BTCUSDT", {"result": {}}, status=502)
    sess.add(MIRROR + "/v2/tickers/BTCUSDT", {"result": {"close": 81174.0}})
    c = _client(sess)

    assert c.get_ticker("BTCUSDT") == {"close": 81174.0}

# ---------------------------------------------------------- order path safety


def test_place_order_never_uses_read_fallback():
    sess = FakeSession()
    sess.add(PRIMARY + "/v2/orders", {"error": "boom"}, status=500)
    c = _client(sess, with_keys=True, retries=1)

    # order path fails CLOSED on primary 500 — it never falls back to a mirror
    with pytest.raises(RuntimeError, match="Delta request failed"):
        c.place_order(product_id=1, size=1.0, side="sell", order_type="market")

    assert _paths(sess.urls) == [PRIMARY + "/v2/orders"]


def test_place_order_raises_when_primary_down():
    sess = FakeSession()
    sess.add(PRIMARY + "/v2/orders", exc=ConnectionError("refused"))
    c = _client(sess, with_keys=True, retries=1)

    with pytest.raises(RuntimeError, match="Delta request failed"):
        c.place_order(product_id=1, size=1.0, side="sell", order_type="market")
    # fallback mirror never touched for an order
    assert all(MIRROR not in url for url in sess.urls)


def test_balances_never_use_read_fallback():
    sess = FakeSession()
    sess.add(PRIMARY + "/v2/wallet/balances", exc=ConnectionError("refused"))
    sess.add(MIRROR + "/v2/wallet/balances", {"result": []})
    c = _client(sess, with_keys=True, retries=1)

    with pytest.raises(RuntimeError, match="Delta request failed"):
        c.get_balances()
    assert _paths(sess.urls) == [PRIMARY + "/v2/wallet/balances"]


def test_public_reads_send_no_signed_body():
    """Fallback-capable reads must be bare GETs (no JSON body)."""
    sess = FakeSession()
    sess.add(CANDLES_URL, {"result": []}, status=502)
    sess.add(MIRROR_CANDLES_URL, {"result": []})
    c = _client(sess, with_keys=True, retries=1)

    c.get_candles("BTCUSDT", resolution="5m")

    assert sess.bodies == [None, None]

# ------------------------------------------------------------- MarketData


def _md(fresh_candle_time: float | None, extra_error: str | None = None) -> MarketData:
    md = MarketData(symbol="BTCUSDT", client=_StubClient())
    md.candles = [{"time": fresh_candle_time, "close": 1.0}] if fresh_candle_time is not None else []
    md.ticker = make_ticker(close=1.0)
    md._last_poll_error = extra_error
    return md


class _StubClient:
    def find_product(self, symbol, contract_type="perpetual_futures"):
        return {"symbol": symbol, "contract_type": "perpetual_futures", "id": 1, "contract_value": "0.001"}

    def get_candles(self, symbol, **kw):
        return []

    def get_ticker(self, symbol):
        return make_ticker(1.0)


def test_snapshot_unavailable_without_candles():
    md = _md(None)
    snap = md.snapshot()
    assert snap["data_status"] == "unavailable"
    assert snap["candles_count"] == 0
    assert snap["last_update_unix"] is None


def test_snapshot_streaming_when_fresh():
    md = _md(time.time() - 60)
    assert md.snapshot()["data_status"] == "streaming"


def test_snapshot_stale_when_candles_old():
    md = _md(time.time() - 3600 * 4)
    assert md.snapshot()["data_status"] == "stale"


def test_snapshot_reports_last_error():
    md = _md(None, extra_error="status 502")
    assert md.snapshot()["last_error"] == "status 502"