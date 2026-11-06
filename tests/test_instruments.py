"""Adaptive Instrument Registry + Dynamic Watchlist — validation.

Covers the normalized instrument model (crypto / Indian equity / future /
option), the registry catalog + search, the persistent watchlist service
(add / remove / duplicate protection / selection / persistence), provider
adapter DATA-UNAVAILABLE honesty, and that Strategy A remains BTCUSDT-only.
"""

import pytest
from fastapi.testclient import TestClient

from app.analysis.config import DEFAULT_WATCHLIST
from app.database.database import SessionLocal
from app.instruments.model import (
    ASSET_EQUITY,
    ASSET_FUTURE,
    ASSET_INDEX,
    ASSET_OPTION,
    ASSET_PERPETUAL,
    ASSET_SPOT,
    DATA_STATUS_UNAVAILABLE,
    EXCHANGE_DELTA,
    EXCHANGE_NSE,
    MARKET_CRYPTO,
    MARKET_INDIA,
    OPTION_CE,
    Instrument,
)
from app.instruments.providers import DeltaProvider, UnavailableProvider
from app.instruments.registry import InstrumentRegistry
from app.instruments.strategy_compat import StrategyCompatibility
from app.instruments.watchlist import WatchlistService
from tests.m3_fixtures import SingleSymbolStub, make_candles

BTC_ID = "crypto:delta:BTCUSDT:perpetual"
RELIANCE_ID = "nse:RELIANCE:equity"


# ================================================================== model


def test_instrument_creation_minimal():
    inst = Instrument(
        id="x:1",
        symbol="X",
        display_name="Example",
        exchange=EXCHANGE_DELTA,
        market=MARKET_CRYPTO,
        asset_type=ASSET_SPOT,
        base_asset="X",
        quote_currency="USDT",
        tick_size=0.01,
        lot_size=1.0,
        contract_size=1.0,
    )
    assert inst.symbol == "X"
    assert inst.expiry is None and inst.strike is None and inst.option_type is None
    d = inst.to_dict()
    assert d["id"] == "x:1"
    assert d["market"] == MARKET_CRYPTO
    assert d["expiry"] is None and d["strike"] is None
    assert d["data_status"] == DATA_STATUS_UNAVAILABLE
    assert d["available"] is False


def test_crypto_instrument_fields():
    inst = InstrumentRegistry().by_symbol("BTCUSDT")
    assert inst is not None
    assert inst.market == MARKET_CRYPTO
    assert inst.asset_type == ASSET_PERPETUAL
    assert inst.exchange == EXCHANGE_DELTA
    assert inst.base_asset == "BTC"
    assert inst.quote_currency == "USDT"
    assert "ticker" in inst.capabilities and "candles" in inst.capabilities


def test_india_equity_instrument_fields():
    inst = InstrumentRegistry().by_symbol("RELIANCE")
    assert inst is not None
    assert inst.market == MARKET_INDIA
    assert inst.asset_type == ASSET_EQUITY
    assert inst.exchange == EXCHANGE_NSE
    assert inst.quote_currency == "INR"
    assert inst.data_provider is None
    assert inst.capabilities == ()
    assert inst.data_status == DATA_STATUS_UNAVAILABLE


def test_future_instrument_fields():
    inst = InstrumentRegistry().by_symbol("NIFTYFUT")
    assert inst is not None
    assert inst.market == MARKET_INDIA
    assert inst.asset_type == ASSET_FUTURE
    assert inst.underlying == "NIFTY"
    assert inst.expiry is None  # no fabricated contract metadata


def test_option_instrument_fields():
    inst = InstrumentRegistry().by_symbol("NIFTYCE")
    assert inst is not None
    assert inst.market == MARKET_INDIA
    assert inst.asset_type == ASSET_OPTION
    assert inst.option_type == OPTION_CE
    assert inst.underlying == "NIFTY"
    # contract specifics stay null until a real provider supplies them
    assert inst.strike is None and inst.expiry is None


def test_roundtrip_from_dict():
    inst = InstrumentRegistry().by_symbol("BTCUSDT")
    rebuilt = Instrument.from_dict(inst.to_dict())
    assert rebuilt == inst


# =============================================================== registry


def test_registry_catalog_contains_all_types():
    reg = InstrumentRegistry()
    markets = {r["market"] for r in reg.list_all()}
    assert markets == {MARKET_CRYPTO, MARKET_INDIA}
    types = {r["asset_type"] for r in reg.list_all()}
    assert {ASSET_PERPETUAL, ASSET_EQUITY, ASSET_INDEX, ASSET_FUTURE, ASSET_OPTION} <= types


def test_registry_search_by_query():
    rows = InstrumentRegistry().search(q="nifty")
    symbols = {r["symbol"] for r in rows}
    assert {"NIFTY50", "NIFTYFUT", "NIFTYCE", "NIFTYPE"} <= symbols


def test_registry_search_filters_market_and_type():
    reg = InstrumentRegistry()
    india = reg.search(market=MARKET_INDIA)
    assert india and all(r["market"] == MARKET_INDIA for r in india)
    options = reg.search(asset_type=ASSET_OPTION)
    assert options and all(r["asset_type"] == ASSET_OPTION for r in options)
    india_options = reg.search(market=MARKET_INDIA, asset_type=ASSET_OPTION)
    assert india_options and all(r["market"] == MARKET_INDIA and r["asset_type"] == ASSET_OPTION for r in india_options)


def test_resolve_by_id_or_symbol():
    reg = InstrumentRegistry()
    assert reg.resolve(BTC_ID).symbol == "BTCUSDT"
    assert reg.resolve("btcusdt").symbol == "BTCUSDT"
    assert reg.resolve("bogus:1") is None


def test_enrich_listing_preserves_five_symbol_contract():
    base = [{"symbol": s, "available": True, "product_id": i, "contract_value": 0.001} for i, s in enumerate(DEFAULT_WATCHLIST)]
    rows = InstrumentRegistry.enrich_listing(base)
    assert [r["symbol"] for r in rows] == DEFAULT_WATCHLIST
    assert all(r["available"] for r in rows)
    assert all(r["market"] == MARKET_CRYPTO for r in rows)
    assert rows[0]["product_id"] == 0 and rows[0]["contract_value"] == 0.001


# ============================================================== providers


class _NoDataClient:
    def get_ticker(self, symbol):
        return {}

    def get_candles(self, symbol, resolution="1h", start=None, end=None, limit=None):
        return []


def test_unavailable_provider_is_honest():
    inst = InstrumentRegistry().by_symbol("RELIANCE")
    p = UnavailableProvider()
    assert p.get_ticker(inst) == {}
    assert p.get_candles(inst) == []
    assert p.get_open_interest(inst) is None
    assert p.get_funding(inst) is None
    assert p.get_order_book(inst) is None
    assert p.get_options_chain(inst) is None


def test_delta_provider_unsupported_fields_return_unavailable():
    inst = InstrumentRegistry().by_symbol("BTCUSDT")
    p = DeltaProvider(client=_NoDataClient())
    # order book + options chain are not published by this adapter
    assert p.get_order_book(inst) is None
    assert p.get_options_chain(inst) is None
    # empty ticker -> OI / funding honest nulls
    assert p.get_open_interest(inst) is None
    assert p.get_funding(inst) is None
    # no candles -> empty list, never fabricated
    assert p.get_candles(inst) == []


def test_delta_provider_delegates_real_path():
    stub = SingleSymbolStub(candles=make_candles())
    p = DeltaProvider(client=stub)
    inst = InstrumentRegistry().by_symbol("BTCUSDT")
    candles = p.get_candles(inst, resolution="1h", start=0, end=2**31)
    assert candles and candles[-1]["close"] > 0

    class TickerOnly:
        def get_ticker(self, symbol):
            return {"close": 50123.0, "oi_value_usd": "50000000", "funding_rate_8h": "0.0001"}

    p2 = DeltaProvider(client=TickerOnly())
    assert p2.get_open_interest(inst) == 50_000_000.0
    assert p2.get_funding(inst) == 0.0001


# =============================================================== watchlist


def _service():
    return WatchlistService(SessionLocal())


def test_watchlist_defaults_preserved_when_empty():
    svc = _service()
    out = svc.list_instruments()
    assert out["default"] == DEFAULT_WATCHLIST
    assert [i["symbol"] for i in out["items"]] == DEFAULT_WATCHLIST


def test_watchlist_add_and_duplicate_protection():
    svc = _service()
    before = len(svc.list_symbols())
    svc.add(RELIANCE_ID)
    assert RELIANCE_ID in svc.list_ids()
    # duplicate add is a no-op
    svc.add(RELIANCE_ID)
    assert svc.list_ids().count(RELIANCE_ID) == 1
    assert len(svc.list_symbols()) == before + 1


def test_watchlist_add_by_symbol():
    svc = _service()
    svc.add("TCS")
    assert "nse:TCS:equity" in svc.list_ids()


def test_watchlist_remove():
    svc = _service()
    svc.add(RELIANCE_ID)
    out = svc.remove(RELIANCE_ID)
    assert RELIANCE_ID not in svc.list_ids()
    assert out["error"] is None
    # removing a non-member reports honestly
    out2 = svc.remove("nse:TCS:equity")
    assert "not on watchlist" in out2["error"]


def test_watchlist_selection():
    svc = _service()
    assert svc.set_selected(BTC_ID) == BTC_ID
    assert svc.get_selected() == BTC_ID
    svc.set_selected(RELIANCE_ID)
    assert svc.get_selected() == RELIANCE_ID
    assert svc.set_selected("bogus") == RELIANCE_ID  # unknown -> keeps prior


def test_watchlist_persistence_across_sessions():
    with SessionLocal() as db:
        WatchlistService(db).add(RELIANCE_ID)
    with SessionLocal() as db:
        out = WatchlistService(db).list_ids()
    assert RELIANCE_ID in out
    assert len(out) == len(DEFAULT_WATCHLIST) + 1


def test_watchlist_never_places_orders():
    svc = _service()
    svc.add(RELIANCE_ID)
    assert not hasattr(svc, "place_order")
    assert not hasattr(svc, "execute_order")


# ======================================================== strategy compatibility


def test_strategy_a_compatible_only_with_btcusdt():
    check = StrategyCompatibility()
    reg = InstrumentRegistry()
    btc = reg.by_symbol("BTCUSDT")
    assert check.is_compatible("strategy_a", btc)
    # every other registered instrument is incompatible with Strategy A
    for row in reg.list_all():
        inst = reg.by_symbol(row["symbol"])
        if inst is not None and inst.symbol != "BTCUSDT":
            assert not check.is_compatible("strategy_a", inst)


def test_strategy_a_requirements_declared():
    req = StrategyCompatibility().requirements("strategy_a")
    assert req["symbols"] == ["BTCUSDT"]
    assert req["markets"] == ["crypto"]
    assert req["asset_types"] == ["perpetual"]
    assert "1H" in req["timeframes"]
    assert "ticker" in req["required_data"] and "candles" in req["required_data"]


def test_strategy_compat_unknown_strategy_raises():
    with pytest.raises(ValueError):
        StrategyCompatibility().requirements("strategy_b")


def test_strategy_a_detail_reasons_on_non_btc():
    check = StrategyCompatibility()
    eth = InstrumentRegistry().by_symbol("ETHUSDT")
    result = check.check("strategy_a", eth)
    assert result["compatible"] is False
    assert any("symbol" in r for r in result["reasons"])


# =========================================================== dashboard contract


@pytest.fixture
def api_client(monkeypatch):
    import app.main as main

    class FakeAnalysis:
        def __init__(self, settings=None, client=None):
            self.settings = settings

        def analyze(self, symbol, strategy_id, fund_inr, risk_pct):
            raise AssertionError("analysis not exercised here")

        def list_instruments(self):
            return {
                "instruments": [
                    {"symbol": s, "available": True, "product_id": i, "contract_value": 0.001}
                    for i, s in enumerate(DEFAULT_WATCHLIST)
                ],
                "error": None,
            }

    monkeypatch.setattr(main, "AnalysisService", FakeAnalysis)
    monkeypatch.setattr(main.settings, "admin_token", "test-admin-token")
    return TestClient(main.app, headers={"X-Admin-Token": "test-admin-token"})


def test_instruments_endpoint_keeps_five_symbol_contract(api_client):
    body = api_client.get("/instruments").json()
    assert body["error"] is None
    assert [i["symbol"] for i in body["instruments"]] == DEFAULT_WATCHLIST
    assert all(i["market"] == MARKET_CRYPTO for i in body["instruments"])
    assert all(i["asset_type"] == ASSET_PERPETUAL for i in body["instruments"])


def test_instruments_catalog_endpoint(api_client):
    body = api_client.get("/instruments/catalog").json()
    symbols = {i["symbol"] for i in body["instruments"]}
    assert symbols >= {"BTCUSDT", "RELIANCE", "NIFTYFUT", "NIFTYCE", "NIFTY50"}
    assert any(i["market"] == MARKET_INDIA for i in body["instruments"])


def test_instruments_search_endpoint(api_client):
    rows = api_client.get("/instruments/search", params={"q": "nifty"}).json()["instruments"]
    assert {r["symbol"] for r in rows} >= {"NIFTY50", "NIFTYFUT", "NIFTYCE"}
    filtered = api_client.get("/instruments/search", params={"market": "crypto", "asset_type": "perpetual"}).json()["instruments"]
    assert filtered and all(r["market"] == "crypto" and r["asset_type"] == "perpetual" for r in filtered)


def test_watchlist_endpoint_contract(api_client):
    body = api_client.get("/watchlist").json()
    assert body["default"] == DEFAULT_WATCHLIST
    assert body["default_symbol"] == "BTCUSDT"
    assert {i["symbol"] for i in body["items"]} == set(DEFAULT_WATCHLIST)


def test_watchlist_add_and_delete_endpoint(api_client):
    added = api_client.post("/watchlist", json={"symbol": "RELIANCE"}).json()
    assert any(i["symbol"] == "RELIANCE" for i in added["items"])
    dup = api_client.post("/watchlist", json={"instrument_id": "nse:RELIANCE:equity"}).json()
    assert sum(1 for i in dup["items"] if i["symbol"] == "RELIANCE") == 1
    removed = api_client.delete("/watchlist/nse:RELIANCE:equity").json()
    assert all(i["symbol"] != "RELIANCE" for i in removed["items"])


def test_watchlist_select_and_unknown_add(api_client):
    sel = api_client.post("/watchlist/select", json={"symbol": "BTCUSDT"}).json()
    assert sel["selected"] == BTC_ID
    bad = api_client.post("/watchlist", json={"symbol": "NOTREAL"}).json()
    assert "unknown instrument" in bad["error"]


# ========================================================== five-symbol analysis


def test_five_symbol_analysis_still_functional():
    from app.analysis.service import AnalysisService

    stub = SingleSymbolStub(candles=make_candles())
    out = AnalysisService(client=stub).analyze("BTCUSDT")
    assert out["market"]["symbol"] == "BTCUSDT"
    assert out["market"]["price"] == pytest.approx(make_candles()[-1]["close"])
    assert out["decision"]["direction"] in ("long", "short")
    assert out["plan"]["position_size"] is not None
    assert out["timeline"][-1]["detail"] == "LOCKED — NO ORDER PLACED"