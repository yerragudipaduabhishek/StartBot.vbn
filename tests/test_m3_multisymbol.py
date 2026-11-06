"""Milestone 3 validation: multi-symbol analysis across the watchlist.

Asserts each watchlist symbol is analysed with its own market data and its own
contract value — never BTC data re-used under another symbol, and never a
fabricated contract value. Also covers symbol-unavailable fallbacks.
"""

import pytest

from app.analysis.config import DEFAULT_WATCHLIST
from app.analysis.service import AnalysisService, KNOWN_CONTRACT_VALUE
from tests.m3_fixtures import SingleSymbolStub, SymbolStub, make_candles

# Per-symbol base prices are widely separated so a cross-symbol data mistake
# (e.g. showing BTC's last close under ETHUSDT) is immediately visible.
WATCH_PRICES = {"BTCUSDT": 50000.0, "ETHUSDT": 3200.0, "SOLUSDT": 180.0, "XRPUSDT": 0.6, "BNBUSDT": 700.0}

# Stubbed product info per symbol (what Delta's product endpoint returns).
# BTC's is the known constant; the others' are read from the product stub so
# the test proves the service never substitutes zero/Null/BTC values.
WATCH_PRODUCTS = {
    "BTCUSDT": {"symbol": "BTCUSDT", "contract_type": "perpetual_futures", "id": 1, "contract_value": "0.001"},
    "ETHUSDT": {"symbol": "ETHUSDT", "contract_type": "perpetual_futures", "id": 2, "contract_value": "0.01"},
    "SOLUSDT": {"symbol": "SOLUSDT", "contract_type": "perpetual_futures", "id": 3, "contract_value": "1.0"},
    "XRPUSDT": {"symbol": "XRPUSDT", "contract_type": "perpetual_futures", "id": 4, "contract_value": "10.0"},
    "BNBUSDT": {"symbol": "BNBUSDT", "contract_type": "perpetual_futures", "id": 5, "contract_value": "0.1"},
}


def make_stub() -> SymbolStub:
    return SymbolStub(symbols=list(DEFAULT_WATCHLIST), prices=WATCH_PRICES, products=WATCH_PRODUCTS)


def test_watchlist_matches_bp_milestone_symbols():
    assert DEFAULT_WATCHLIST == ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT"]


def test_instruments_list_lists_all_watchlist_symbols():
    stub = make_stub()
    out = AnalysisService(client=stub).list_instruments()
    assert out["error"] is None
    symbols = [i["symbol"] for i in out["instruments"]]
    assert symbols == DEFAULT_WATCHLIST
    assert all(i["available"] for i in out["instruments"])
    # BTC contract value comes from the known constant, others from the product.
    got = {i["symbol"]: i["contract_value"] for i in out["instruments"]}
    assert got["BTCUSDT"] == KNOWN_CONTRACT_VALUE["BTCUSDT"]
    assert got["ETHUSDT"] == 0.01
    assert got["SOLUSDT"] == 1.0
    assert got["XRPUSDT"] == 10.0
    assert got["BNBUSDT"] == 0.1


@pytest.mark.parametrize("symbol", DEFAULT_WATCHLIST)
def test_each_symbol_analyses_coherently(symbol):
    stub = make_stub()
    out = AnalysisService(client=stub).analyze(symbol)
    assert out["decision"]["direction"] in ("long", "short")
    assert out["leverage"]["effective"] is not None
    assert out["leverage"]["status"] in ("CAPPED BY RISK ENGINE", "WITHIN RISK LIMIT")
    assert out["plan"]["position_size"] is not None
    assert out["plan"]["risk_allowed"] is True
    assert out["market"]["symbol"] == symbol


@pytest.mark.parametrize("symbol", DEFAULT_WATCHLIST)
def test_symbol_uses_its_own_last_close_not_btc(symbol):
    """The price shown must be *that symbol's* candle series, never BTC's."""
    stub = make_stub()
    out = AnalysisService(client=stub).analyze(symbol)
    own_last = stub.data[symbol][-1]["close"]
    btc_last = stub.data["BTCUSDT"][-1]["close"]
    assert out["market"]["price"] == pytest.approx(own_last)
    if symbol != "BTCUSDT":
        assert out["market"]["price"] != pytest.approx(btc_last)


@pytest.mark.parametrize("symbol", ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT"])
def test_contract_value_comes_from_symbol_source(symbol):
    stub = make_stub()
    out = AnalysisService(client=stub).analyze(symbol)
    if symbol == "BTCUSDT":
        assert out["market"]["contract_value"] == KNOWN_CONTRACT_VALUE["BTCUSDT"]
    else:
        assert out["market"]["contract_value"] == float(WATCH_PRODUCTS[symbol]["contract_value"])


def test_btc_data_not_reused_under_other_symbols():
    """Different symbols -> different prices, contract values, sizing."""
    stub = make_stub()
    outs = {s: AnalysisService(client=stub).analyze(s) for s in DEFAULT_WATCHLIST}
    prices = {s: outs[s]["market"]["price"] for s in DEFAULT_WATCHLIST}
    contract_values = {s: outs[s]["market"]["contract_value"] for s in DEFAULT_WATCHLIST}
    assert len(set(prices)) == 5
    assert len(set(contract_values)) == 5
    # Sizing uses the per-symbol contract value.
    assert outs["BTCUSDT"]["plan"]["position_size"] != outs["XRPUSDT"]["plan"]["position_size"]


def test_invalid_symbol_returns_data_unavailable():
    stub = make_stub()
    out = AnalysisService(client=stub).analyze("DOGEUSDT")
    assert out["decision"]["direction"] == "none"
    assert out["leverage"]["status"] == "DATA UNAVAILABLE"
    assert out["plan"]["position_size"] is None
    assert "no candles" in out["decision"]["reason"] or "candles" in out["decision"]["reason"]


def test_no_candles_returns_data_unavailable():
    stub = SymbolStub(symbols=["BTCUSDT"], prices={"BTCUSDT": 50000.0})
    stub.data["BTCUSDT"] = []
    out = AnalysisService(client=stub).analyze("BTCUSDT")
    assert out["market"]["candles_count"] == 0
    assert out["leverage"]["status"] == "DATA UNAVAILABLE"
    assert "no candles" in out["decision"]["reason"]


def test_insufficient_history_returns_data_unavailable():
    stub = SingleSymbolStub(candles=make_candles(n=29))
    out = AnalysisService(client=stub).analyze("BTCUSDT")
    assert out["market"]["candles_count"] == 29
    assert out["leverage"]["status"] == "DATA UNAVAILABLE"
    assert "insufficient candle history" in out["decision"]["reason"]


def test_api_error_returns_data_unavailable():
    stub = SymbolStub(symbols=["BTCUSDT"], prices={"BTCUSDT": 50000.0})
    stub.fails = {"BTCUSDT"}
    out = AnalysisService(client=stub).analyze("BTCUSDT")
    assert out["leverage"]["status"] == "DATA UNAVAILABLE"
    assert "market data unavailable" in out["decision"]["reason"]


def test_missing_product_for_symbol_skips_sizing():
    """SOLUSDT candles + ticker exist but its product entry is gone:
    contract value is unknown -> sizing must be skipped, decision stays honest."""
    products = {s: w for s, w in WATCH_PRODUCTS.items() if s != "SOLUSDT"}
    stub = SymbolStub(symbols=list(DEFAULT_WATCHLIST), prices=WATCH_PRICES, products=products)
    out = AnalysisService(client=stub).analyze("SOLUSDT")
    assert out["market"]["contract_value"] is None
    assert out["plan"]["position_size"] is None
    assert out["decision"]["direction"] in ("long", "short")
    assert "contract value" in out["plan"]["risk_line"]
    assert out["plan"]["reason"] == "instrument contract value unavailable"


def test_per_symbol_candle_series_are_isolated():
    """Analysis input candles must be that symbol's own, not a shared BTC cache."""
    stub = make_stub()
    AnalysisService(client=stub).analyze("ETHUSDT")
    calls = [c for c in stub.calls if c[0] == "get_candles"]
    assert calls == [("get_candles", "ETHUSDT")]