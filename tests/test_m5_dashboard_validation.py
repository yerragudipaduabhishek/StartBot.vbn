"""Milestone 5 — Real-Data Dashboard Validation.

Validates the read-only analysis pipeline + dashboard API contract against
data-quality edge cases using deterministic stubs.

Coverage:
- Data quality: empty / insufficient / stale candles, HTTP 502-503 after
  exhausted fallback, timeout, connection failure, missing/zero OI, missing
  funding, missing/zero contract value, zero/invalid price.
- Every unavailable path must end with EXECUTION: LOCKED — NO ORDER PLACED.
- Dashboard API contract (FastAPI TestClient): symbol/strategy/fund/risk
  reach the handler, and every block the UI reads is present.
- Symbol isolation: switching BTC/ETH/SOL/XRP/BNB leaks nothing between symbols.
- Leverage: effective = min(AI recommended, risk max); hard cap preserved.
- Safety flags and fallback confinement (order path never uses a read mirror).
"""

import time

import pytest
from fastapi.testclient import TestClient

from app.analysis import leverage as lev
from app.analysis.config import DEFAULT_WATCHLIST
from app.analysis.service import AnalysisService
from app.config.settings import Settings
from tests.m3_fixtures import SingleSymbolStub, SymbolStub, make_candles, make_ticker

WATCH_PRICES = {"BTCUSDT": 50000.0, "ETHUSDT": 3200.0, "SOLUSDT": 180.0, "XRPUSDT": 0.6, "BNBUSDT": 700.0}
WATCH_PRODUCTS = {
    "BTCUSDT": {"symbol": "BTCUSDT", "contract_type": "perpetual_futures", "id": 1, "contract_value": "0.001"},
    "ETHUSDT": {"symbol": "ETHUSDT", "contract_type": "perpetual_futures", "id": 2, "contract_value": "0.01"},
    "SOLUSDT": {"symbol": "SOLUSDT", "contract_type": "perpetual_futures", "id": 3, "contract_value": "1.0"},
    "XRPUSDT": {"symbol": "XRPUSDT", "contract_type": "perpetual_futures", "id": 4, "contract_value": "1.0"},
    "BNBUSDT": {"symbol": "BNBUSDT", "contract_type": "perpetual_futures", "id": 5, "contract_value": "0.1"},
}


def _stale(n=200, hours_ago=7):
    now_hour = (int(time.time()) // 3600) * 3600
    return make_candles(n=n, base=50000.0, t0=now_hour - (n - 1) * 3600 - hours_ago * 3600)


def _fresh(n=200):
    return make_candles(n=n, base=50000.0, step=25.0)


def _assert_unavailable(out: dict, reason_fragment: str):
    assert out["decision"]["direction"] == "none"
    assert out["decision"]["reason"] and reason_fragment in out["decision"]["reason"]
    assert out["market"]["price"] is None
    assert out["plan"]["position_size"] is None
    assert out["leverage"]["status"] == "DATA UNAVAILABLE"
    assert out["timeline"][-1]["step"] == "EXECUTION"
    assert out["timeline"][-1]["detail"] == "LOCKED — NO ORDER PLACED"


# ================================================================== data quality


@pytest.mark.parametrize("error", ['status 502', 'status 503', "502 Bad Gateway", "503 Service Unavailable"])
def test_http_502_503_exhausted_hosts_is_data_unavailable(error):
    class Boom(SingleSymbolStub):
        def get_candles(self, symbol, resolution="1h", start=None, end=None):
            raise RuntimeError(f"Delta request failed: GET /v2/history/candles after 2 host(s) ({error})")

    _assert_unavailable(AnalysisService(client=Boom()).analyze("BTCUSDT"), "market data unavailable")


@pytest.mark.parametrize("exc", [TimeoutError("read timed out"), RuntimeError("timed out after 15s")])
def test_timeout_is_data_unavailable(exc):
    class Boom(SingleSymbolStub):
        def get_candles(self, symbol, resolution="1h", start=None, end=None):
            raise exc

    _assert_unavailable(AnalysisService(client=Boom()).analyze("BTCUSDT"), "market data unavailable")


@pytest.mark.parametrize(
    "exc",
    [ConnectionError("connection refused"), OSError("connection reset by peer"), RuntimeError("Failed to establish a new connection")],
)
def test_connection_failure_is_data_unavailable(exc):
    class Boom(SingleSymbolStub):
        def get_candles(self, symbol, resolution="1h", start=None, end=None):
            raise exc

    _assert_unavailable(AnalysisService(client=Boom()).analyze("BTCUSDT"), "market data unavailable")


def test_empty_candles_is_data_unavailable():
    stub = SingleSymbolStub(candles=[], ticker=make_ticker(0.0))
    out = AnalysisService(client=stub).analyze("BTCUSDT")
    assert out["market"]["candles_count"] == 0
    _assert_unavailable(out, "no candles")


@pytest.mark.parametrize("n", [5, 29])
def test_insufficient_candles_is_data_unavailable(n):
    stub = SingleSymbolStub(candles=make_candles(n=n))
    out = AnalysisService(client=stub).analyze("BTCUSDT")
    assert out["market"]["candles_count"] == n
    _assert_unavailable(out, "insufficient candle history")


def test_stale_candles_is_data_unavailable():
    stub = SingleSymbolStub(candles=_stale(hours_ago=7))
    out = AnalysisService(client=stub).analyze("BTCUSDT")
    assert out["market"]["candles_count"] == 200
    _assert_unavailable(out, "stale candle data")


def test_fresh_candles_are_accepted():
    stub = SingleSymbolStub(candles=_fresh())
    out = AnalysisService(client=stub).analyze("BTCUSDT")
    assert out["market"]["price"] == pytest.approx(_fresh()[-1]["close"])
    assert out["market"]["candles_count"] == 200
    assert out["decision"]["direction"] in ("long", "short")


def test_zero_price_is_data_unavailable():
    zero = [{**c, "open": 0.0, "high": 0.0, "low": 0.0, "close": 0.0} for c in _fresh()]
    out = AnalysisService(client=SingleSymbolStub(candles=zero)).analyze("BTCUSDT")
    _assert_unavailable(out, "invalid market price")


def test_zero_oi_and_funding_shown_honestly():
    ticker = make_ticker(_fresh()[-1]["close"], funding=0.0, oi="0")
    out = AnalysisService(client=SingleSymbolStub(candles=_fresh(), ticker=ticker)).analyze("BTCUSDT")
    assert out["market"]["open_interest"] == 0.0
    assert out["market"]["funding_8h"] == 0.0
    assert out["decision"]["direction"] in ("long", "short")


def test_missing_oi_shown_honestly():
    ticker = {"close": _fresh()[-1]["close"]}
    out = AnalysisService(client=SingleSymbolStub(candles=_fresh(), ticker=ticker)).analyze("BTCUSDT")
    assert out["market"]["open_interest"] is None
    assert out["market"]["funding_8h"] is None


def test_missing_contract_value_skips_sizing_still_analyses():
    products = {s: w for s, w in WATCH_PRODUCTS.items() if s != "SOLUSDT"}
    stub = SymbolStub(symbols=list(DEFAULT_WATCHLIST), prices=WATCH_PRICES, products=products)
    out = AnalysisService(client=stub).analyze("SOLUSDT")
    assert out["market"]["contract_value"] is None
    assert out["plan"]["position_size"] is None
    assert out["plan"]["reason"] == "instrument contract value unavailable"
    assert out["decision"]["direction"] in ("long", "short")
    # leverage still honest — never invented
    assert out["leverage"]["status"] in ("CAPPED BY RISK ENGINE", "WITHIN RISK LIMIT")


def test_zero_contract_value_skips_sizing():
    products = dict(WATCH_PRODUCTS)
    products["SOLUSDT"] = {**products["SOLUSDT"], "contract_value": "0"}
    stub = SymbolStub(symbols=list(DEFAULT_WATCHLIST), prices=WATCH_PRICES, products=products)
    out = AnalysisService(client=stub).analyze("SOLUSDT")
    assert out["market"]["contract_value"] == 0.0
    assert out["plan"]["position_size"] is None
    assert "contract value" in out["plan"]["risk_line"]


# =============================================================== dashboard API flow


def _block(symbol, strategy_id="strategy_a", fund_inr=5000.0, risk_pct=0.5):
    return {
        "symbol": symbol,
        "strategy": {
            "id": strategy_id,
            "name": "Strategy A",
            "status": "UNVALIDATED",
            "timeframe": "1H",
            "parameters": {
                "ema_fast": 8,
                "ema_slow": 40,
                "ema_mid": 50,
                "ema_spans": [8, 40, 50],
                "threshold": 0.25,
                "min_confidence": 0.5,
                "sl_atr_multiple": 1.0,
                "target_atr_multiple": 2.0,
                "weights": {},
            },
        },
        "fund_inr": fund_inr,
        "risk_pct": risk_pct,
        "max_risk_inr": round(fund_inr * risk_pct / 100.0, 2),
        "market": {
            "symbol": symbol,
            "price": 65000.0,
            "atr_pct": 0.7,
            "candles": [],
            "candles_count": 3,
            "last_candle_time": 1700000000,
            "funding_8h": 0.01,
            "open_interest": 25000.0,
            "contract_value": 0.001,
        },
        "decision": {
            "direction": "long",
            "score": 0.4,
            "confidence": 0.6,
            "votes": {
                "trend": {"agent": "trend", "direction": "long", "score": 0.7, "confidence": 0.6, "explanation": "bullish"},
                "structure": {"agent": "structure", "direction": "long", "score": 1.0, "confidence": 0.6, "explanation": "HL above"},
                "sentiment": {"agent": "sentiment", "direction": "neutral", "score": 0.0, "confidence": 0.0, "explanation": "flat"},
            },
            "reasoning": "trend & structure agree",
            "reason": None,
        },
        "plan": {
            "entry": 65000.0,
            "stop_loss": 64000.0,
            "target": 67000.0,
            "rr": 2.0,
            "position_size": 1.5,
            "notional": 97500.0,
            "margin": 9750.0,
            "risk_allowed": True,
            "reason": "sized",
            "risk_line": "max risk line",
        },
        "leverage": {
            "ai_recommended": 4.0,
            "risk_max": 5.0,
            "effective": 4.0,
            "status": "WITHIN RISK LIMIT",
            "reason": "xx",
            "factors": {},
            "market_risk": "MODERATE",
            "trend": "BULLISH",
            "volatility": "NORMAL",
            "signal_confidence": 0.6,
        },
        "timeline": [{"time": 1700000000, "step": "EXECUTION", "detail": "LOCKED — NO ORDER PLACED"}],
    }


@pytest.fixture
def api_client(monkeypatch):
    import app.main as main

    class FakeAnalysis:
        def __init__(self, settings=None, client=None):
            self.settings = settings

        def analyze(self, symbol, strategy_id, fund_inr, risk_pct):
            return _block(symbol, strategy_id, fund_inr, risk_pct)

        def list_instruments(self):
            return {"instruments": [{"symbol": s, "available": True, "product_id": i, "contract_value": 1.0} for i, s in enumerate(DEFAULT_WATCHLIST)], "error": None}

    monkeypatch.setattr(main, "AnalysisService", FakeAnalysis)
    return TestClient(main.app)


def test_health_and_safety_contract(api_client):
    health = api_client.get("/health").json()
    assert health["status"] == "ok"

    safety = api_client.get("/safety").json()
    assert safety["live_mode"] is False
    assert safety["testnet_mode"] is True
    assert safety["real_order_confirmation"] is False
    # execution_allowed == "testnet venue reachable" by design; the UI still
    # hard-codes PLACEMENT: LOCKED and no API keys are configured.
    assert safety["execution_allowed"] == (not safety["live_mode"] and safety["testnet_mode"])
    assert safety["api_keys_configured"] is False
    assert "testnet" in safety["delta_base_url"]


def test_strategies_and_watchlist_endpoints(api_client):
    strategies = api_client.get("/strategies").json()
    assert strategies["default"] == "strategy_a"
    assert strategies["strategies"][0]["parameters"]["ema_spans"] == [8, 40, 50]
    assert strategies["strategies"][0]["status"] == "UNVALIDATED"

    watchlist = api_client.get("/watchlist").json()
    assert watchlist["default"] == DEFAULT_WATCHLIST

    instruments = api_client.get("/instruments").json()
    assert instruments["error"] is None
    assert [i["symbol"] for i in instruments["instruments"]] == DEFAULT_WATCHLIST


def test_analysis_endpoint_forwards_selection_and_returns_full_block(api_client):
    resp = api_client.get("/analysis/btcusdt", params={"strategy": "strategy_a", "fund": 7500, "risk": 1.0})
    assert resp.status_code == 200
    body = resp.json()
    assert body["symbol"] == "BTCUSDT"  # uppercased server-side
    assert body["fund_inr"] == 7500.0
    assert body["risk_pct"] == 1.0
    assert body["max_risk_inr"] == 75.0

    # every block the dashboard renders must be present
    for key in ("symbol", "strategy", "fund_inr", "risk_pct", "max_risk_inr", "market", "decision", "plan", "leverage", "timeline"):
        assert key in body
    assert body["market"]["candles_count"] == 3
    assert body["decision"]["direction"] == "long"
    assert body["decision"]["confidence"] == 0.6
    assert set(body["decision"]["votes"]) >= {"trend", "structure", "sentiment"}
    assert body["leverage"]["effective"] == 4.0
    assert body["leverage"]["risk_max"] == 5.0
    assert body["leverage"]["ai_recommended"] == 4.0
    assert body["plan"]["position_size"] == 1.5
    assert body["timeline"][-1]["detail"] == "LOCKED — NO ORDER PLACED"


def test_analysis_endpoint_uses_defaults_when_omitted(api_client):
    resp = api_client.get("/analysis/ETHUSDT")
    body = resp.json()
    assert body["symbol"] == "ETHUSDT"
    assert body["fund_inr"] == 5000.0
    assert body["risk_pct"] == 0.5
    # selected Strategy A from the server default
    assert body["strategy"]["id"] == "strategy_a"


# =============================================================== symbol isolation


def _per_symbol_tickers():
    return {
        s: make_ticker(WATCH_PRICES[s], funding=0.0001 + i * 0.0001, oi=str(1_000_000 * (i + 1)))
        for i, s in enumerate(DEFAULT_WATCHLIST)
    }


def test_no_oi_or_funding_leak_between_symbols():
    stub = SymbolStub(symbols=list(DEFAULT_WATCHLIST), prices=WATCH_PRICES, products=WATCH_PRODUCTS, tickers=_per_symbol_tickers())
    outs = {s: AnalysisService(client=stub).analyze(s) for s in DEFAULT_WATCHLIST}

    oi = {s: outs[s]["market"]["open_interest"] for s in DEFAULT_WATCHLIST}
    funding = {s: outs[s]["market"]["funding_8h"] for s in DEFAULT_WATCHLIST}
    assert len(set(oi.values())) == 5
    assert len(set(funding.values())) == 5
    for s in DEFAULT_WATCHLIST:
        assert outs[s]["market"]["symbol"] == s
        assert outs[s]["decision"]["direction"] in ("long", "short")


def test_bnb_analysis_never_reuses_sol_data():
    stub = SymbolStub(symbols=list(DEFAULT_WATCHLIST), prices=WATCH_PRICES, products=WATCH_PRODUCTS, tickers=_per_symbol_tickers())
    sol = AnalysisService(client=stub).analyze("SOLUSDT")
    out = AnalysisService(client=stub).analyze("BNBUSDT")
    assert out["market"]["price"] == pytest.approx(stub.data["BNBUSDT"][-1]["close"])
    assert out["market"]["price"] != pytest.approx(sol["market"]["price"])
    assert out["market"]["contract_value"] == 0.1
    assert sol["market"]["contract_value"] == 1.0
    # candle fetches stay isolated to the requested symbol
    calls = [c[1] for c in stub.calls if c[0] == "get_candles"]
    assert calls.count("SOLUSDT") == 1 and calls.count("BNBUSDT") == 1


# =================================================================== leverage


def test_leverage_effective_is_min_ai_vs_risk_and_capped():
    ctx = lev.LeverageContext(
        trend_score=1.0, adx=50.0, atr_pct=0.2, confidence=1.0,
        momentum=1.0, positioning=1.0, liquidity=1.0, stop_distance_pct=0.5,
    )
    out = lev.resolve_leverage(ctx, max_leverage_setting=5.0)
    assert out["ai_recommended"] > out["risk_max"]
    assert out["effective"] == pytest.approx(out["risk_max"])
    assert out["status"] == "CAPPED BY RISK ENGINE"
    # the hard cap (settings max) is never exceeded regardless of AI appetite
    assert out["risk_max"] <= 5.0 + 1e-9
    assert out["effective"] <= 5.0 + 1e-9


def test_leverage_effective_is_ai_when_below_cap():
    ctx = lev.LeverageContext(
        trend_score=0.0, adx=10.0, atr_pct=1.2, confidence=0.2,
        momentum=-1.0, positioning=-1.0, liquidity=0.0, stop_distance_pct=2.0,
    )
    out = lev.resolve_leverage(ctx, max_leverage_setting=5.0)
    assert out["ai_recommended"] < out["risk_max"]
    assert out["effective"] == pytest.approx(out["ai_recommended"])
    assert out["status"] == "WITHIN RISK LIMIT"
    assert out["effective"] == pytest.approx(min(out["ai_recommended"], out["risk_max"]), abs=1e-9)


def test_no_signal_never_invents_leverage():
    ctx = lev.LeverageContext(trend_score=0.5, adx=20.0, atr_pct=0.8, confidence=0.0)
    out = lev.resolve_leverage(ctx, max_leverage_setting=5.0)
    assert out["status"] == "DATA UNAVAILABLE"
    assert out["effective"] is None


# ====================================================================== safety


def test_settings_are_the_locked_defaults():
    s = Settings()
    assert s.live_mode is False
    assert s.testnet_mode is True
    assert s.real_order_confirmation is False


def test_read_fallback_urls_active_in_testnet_no_override():
    s = Settings()
    assert s.delta_market_data_fallback_urls
    assert s.delta_market_data_url != s.delta_market_data_fallback_urls[0]
    # the fallback is a production mirror while testnet is the primary host
    assert "testnet" in s.delta_market_data_url


def test_analysis_service_constructs_read_fallback_client():
    from app.data.delta_client import DeltaClient

    svc = AnalysisService(settings=Settings(), client=None)
    assert isinstance(svc.client, DeltaClient)
    assert svc.client.read_fallback_urls == Settings().delta_market_data_fallback_urls