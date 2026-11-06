"""Milestone 6 — 24/7 market-data health monitoring + monitoring endpoints.

Validates the mult-symbol MarketHealthMonitor against deterministic stubs:
streaming / stale / unavailable / invalid-price classification, Delta 502s and
timeouts surfacing honestly, missing/zero OI and funding, read-only behaviour,
and the FastAPI contract for /monitor, /paper/account, /paper/position,
extended /paper and extended /health — all guarded when the engine is absent.
"""

import time

import pytest
from fastapi.testclient import TestClient

from app.data.market_health import MarketHealthMonitor, _num
from app.database.database import SessionLocal
from app.trading.paper_trader import PaperTrader
from tests.m3_fixtures import make_candles, make_ticker


class MonitorStub:
    """Delta-shaped read-only stub: candles + ticker only, records its calls."""

    def __init__(
        self,
        candles=None,
        tickers=None,
        fails=None,
        ticker_fails=None,
    ) -> None:
        self.candles = candles if isinstance(candles, dict) else {"*": candles or make_candles()}
        self.tickers = tickers if tickers is not None else self._default_tickers()
        self.fails = set(fails or [])
        self.ticker_fails = set(ticker_fails or [])
        self.calls: list[tuple[str, str]] = []

    def _default_tickers(self) -> dict:
        out = {}
        for sym, arr in self.candles.items():
            close = arr[-1]["close"] if arr else 0.0
            out[sym] = make_ticker(close)
        return out

    def get_candles(self, symbol, resolution="1h", start=None, end=None):
        self.calls.append(("get_candles", symbol))
        if symbol in self.fails:
            raise RuntimeError("Delta request failed: GET /v2/history/candles after 2 host(s) (status 502)")
        return list(self.candles.get(symbol, self.candles.get("*", [])))

    def get_ticker(self, symbol):
        self.calls.append(("get_ticker", symbol))
        if symbol in self.ticker_fails:
            raise TimeoutError("ticker request timed out")
        tk = self.tickers.get(symbol, {})
        return None if tk is None else dict(tk)


def _fresh(n=60) -> list[dict]:
    return make_candles(n=n, base=50000.0)


def _stale(n=60, age_hours=10) -> list[dict]:
    now_hour = (int(time.time()) // 3600) * 3600
    return make_candles(n=n, base=50000.0, t0=now_hour - (n - 1) * 3600 - age_hours * 3600)


def _monitor(**kw) -> MarketHealthMonitor:
    kwargs = dict(symbols=["BTCUSDT", "ETHUSDT"])
    kwargs.update(kw)
    return MarketHealthMonitor(**kwargs)


# ================================================================== health data


def test_fresh_symbols_are_all_streaming():
    mon = _monitor(client=MonitorStub(candles={"BTCUSDT": _fresh(), "ETHUSDT": _fresh()}))
    snap = mon.check_once()
    assert snap["overall"] == "streaming"
    for row in snap["symbols"]:
        assert row["status"] == "streaming"
        assert row["price"] == pytest.approx(50000.0)
        assert row["price_valid"] is True
        assert row["oi_missing"] is False
        assert row["funding_missing"] is False
        assert row["feed_ok"] is True


def test_stale_candles_marked_stale():
    mon = _monitor(client=MonitorStub(candles={"BTCUSDT": _stale()}))
    row = mon.check_once()["symbols"][0]
    assert row["status"] == "stale"
    assert row["status_reason"] and row["status_reason"].startswith("last candle age")


def test_delta_502_surfaces_error_honestly():
    mon = _monitor(client=MonitorStub(fails={"BTCUSDT"}, candles={"BTCUSDT": _fresh()}))
    row = mon.check_once()["symbols"][0]
    assert row["status"] == "unavailable"
    assert row["error"] and "502" in row["error"]
    assert row["feed_ok"] is False
    assert row["price"] is None


def test_timeout_surfaces_error():
    mon = _monitor(client=MonitorStub(ticker_fails={"BTCUSDT"}, candles={"BTCUSDT": _fresh()}))
    row = mon.check_once()["symbols"][0]
    # candles were fine, so the feed is live but the ticker timed out
    assert row["status"] == "streaming"
    assert "timed out" in (row["error"] or "")
    assert row["ticker_ok"] is False


def test_empty_candles_are_unavailable():
    mon = _monitor(client=MonitorStub(candles={"BTCUSDT": []}))
    row = mon.check_once()["symbols"][0]
    assert row["status"] == "unavailable"
    assert row["candles_count"] == 0


def test_zero_close_price_rejected_invalid_price():
    candles = _fresh()
    candles[-1]["close"] = 0
    mon = _monitor(client=MonitorStub(candles={"BTCUSDT": candles}))
    row = mon.check_once()["symbols"][0]
    assert row["status"] == "invalid_price"
    assert row["price_valid"] is False
    assert row["price"] == 0


def test_missing_oi_and_funding_flagged():
    mon = _monitor(client=MonitorStub(candles={"BTCUSDT": _fresh()}, tickers={"BTCUSDT": {"close": 50000}}))
    row = mon.check_once()["symbols"][0]
    assert row["oi_missing"] is True
    assert row["funding_missing"] is True
    assert row["open_interest"] is None
    assert row["funding"] is None


def test_none_ticker_treated_as_unavailable_data():
    mon = _monitor(client=MonitorStub(candles={"BTCUSDT": _fresh()}, tickers={"BTCUSDT": None}))
    row = mon.check_once()["symbols"][0]
    assert row["status"] == "streaming"
    assert row["ticker_ok"] is False
    assert row["open_interest"] is None
    assert row["funding"] is None
    assert row["oi_missing"] is True
    assert row["funding_missing"] is True
    assert row["price"] == pytest.approx(50000.0)


def test_none_ticker_without_candles_surfaces_unavailable():
    mon = _monitor(client=MonitorStub(candles={"BTCUSDT": []}, tickers={"BTCUSDT": None}))
    row = mon.check_once()["symbols"][0]
    assert row["status"] == "unavailable"
    assert row["ticker_ok"] is False
    assert row["oi_missing"] is True
    assert row["funding_missing"] is True


def test_fresh_secs_controls_streaming_window():
    candles = _stale(n=2, age_hours=1)  # last candle 1h old
    mon = _monitor(client=MonitorStub(candles={"BTCUSDT": candles}), fresh_secs=3 * 3600)
    assert mon.check_once()["symbols"][0]["status"] == "streaming"
    mon2 = _monitor(client=MonitorStub(candles={"BTCUSDT": candles}), fresh_secs=30 * 60)
    assert mon2.check_once()["symbols"][0]["status"] == "stale"


def test_monitor_uses_only_public_reads():
    stub = MonitorStub(candles={"BTCUSDT": _fresh()})
    mon = _monitor(client=stub)
    mon.check_once()
    methods = {c[0] for c in stub.calls}
    assert methods <= {"get_candles", "get_ticker"}  # order endpoints never touched


def test_monitor_never_fabricates_values():
    """When OI/funding are absent the monitor must NOT invent numbers."""
    mon = _monitor(client=MonitorStub(candles={"BTCUSDT": _fresh()}, tickers={"BTCUSDT": {"close": 50000}}))
    row = mon.check_once()["symbols"][0]
    assert row["open_interest"] is None
    assert row["funding"] is None
    assert row["price"] == pytest.approx(50000.0)


def test_num_helper_rejects_garbage():
    assert _num(None) is None
    assert _num("") is None
    assert _num("abc") is None
    assert _num("12.5") == 12.5
    assert _num(float("nan")) is None


# ============================================================== API contract


@pytest.fixture
def client(monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "state", {})
    return TestClient(main.app)


def test_health_reports_paper_and_monitor_when_running(client, monkeypatch):
    import app.main as main

    pt = PaperTrader(starting_equity=5000.0, session_factory=SessionLocal)
    pt.open_position("long", 5, 50000, 49500, 51000)
    mon = MarketHealthMonitor(symbols=["BTCUSDT"], client=MonitorStub(candles={"BTCUSDT": _fresh()}))
    mon.check_once()

    class FakeMarket:
        def snapshot(self):
            return {"data_status": "streaming", "candles_count": 60, "data_source": "websocket", "price_valid": True, "last_candle_age_secs": 12, "ws_status": {"connected": True}}

    monkeypatch.setattr(main, "state", {"market": FakeMarket(), "paper": pt, "monitor": mon})
    body = client.get("/health").json()
    assert body["ready"] is True
    assert body["data_source"] == "websocket"
    assert body["paper"]["mode"] == "paper"
    assert body["paper"]["persisted"] is True
    assert body["paper"]["position_open"] is True
    assert body["monitor"]["overall"] == "streaming"
    assert body["monitor"]["covered_symbols"] == 1


def test_monitor_endpoint_running(client, monkeypatch):
    import app.main as main

    mon = MarketHealthMonitor(symbols=["BTCUSDT", "ETHUSDT"], client=MonitorStub(candles={"BTCUSDT": _fresh(), "ETHUSDT": []}))
    mon.check_once()
    monkeypatch.setattr(main, "state", {"monitor": mon})
    body = client.get("/monitor").json()
    assert body["overall"] == "degraded"
    assert {s["symbol"] for s in body["symbols"]} == {"BTCUSDT", "ETHUSDT"}


def test_monitor_endpoint_guarded(client):
    body = client.get("/monitor").json()
    assert body["overall"] is None
    assert body["symbols"] == []


def test_paper_account_endpoint(client, monkeypatch):
    import app.main as main

    pt = PaperTrader(starting_equity=5000.0, fee_pct=0.05, slippage_pct=0.02, session_factory=SessionLocal)
    monkeypatch.setattr(main, "state", {"paper": pt})
    body = client.get("/paper/account").json()
    assert body["starting_balance"] == 5000.0
    assert body["balance"] == 5000.0
    assert body["currency"] == "INR"
    assert body["fee_pct"] == 0.05
    assert body["slippage_pct"] == 0.02
    assert body["fund_separate"] is True


def test_paper_position_endpoint(client, monkeypatch):
    import app.main as main

    pt = PaperTrader(starting_equity=5000.0, session_factory=SessionLocal)
    pt.open_position("long", 5, 50000, 49500, 51000)
    monkeypatch.setattr(main, "state", {"paper": pt})
    body = client.get("/paper/position").json()
    assert body["position"] is not None
    assert body["position"]["entry_price"] == 50000
    assert body["position"]["take_profit"] == 51000
    assert "_persisted_price" not in body["position"]


def test_paper_endpoint_extended(client, monkeypatch):
    import app.main as main

    pt = PaperTrader(starting_equity=5000.0, session_factory=SessionLocal)
    pt.open_position("long", 5, 50000, 49500, 51000)
    monkeypatch.setattr(main, "state", {"paper": pt})
    body = client.get("/paper").json()
    assert body["mode"] == "paper"
    assert "SIMULATED MONEY" in body["note"]
    assert "never the real Trading Fund" in body["note"]
    assert body["balance"] == 5000.0
    assert body["total_equity"] == pytest.approx(5000.0)


def test_paper_and_monitor_endpoints_guarded(client):
    acct = client.get("/paper/account").json()
    assert acct["error"] == "paper engine not running"
    pos = client.get("/paper/position").json()
    assert pos["error"] == "paper engine not running"
    paper = client.get("/paper").json()
    assert paper["mode"] == "paper"
    assert paper["error"] == "paper engine not running"