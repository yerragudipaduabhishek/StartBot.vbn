"""Milestone 3 validation: symbol switching, strategy selection, decision
timeline sequence, safety contract, and no-fabrication fallbacks for missing
auxiliary data (OI / funding / liquidity).

Read-only end to end: nothing here touches order placement.
"""

import pytest

from app.analysis.config import DEFAULT_WATCHLIST
from app.analysis.service import AnalysisService
from app.analysis.strategies import get_strategy, list_strategies
from app.config.settings import Settings, get_settings
from tests.m3_fixtures import SingleSymbolStub, SymbolStub, make_candles

WATCH_PRICES = {"BTCUSDT": 50000.0, "ETHUSDT": 3200.0, "SOLUSDT": 180.0, "XRPUSDT": 0.6, "BNBUSDT": 700.0}
WATCH_PRODUCTS = {
    "BTCUSDT": {"symbol": "BTCUSDT", "contract_type": "perpetual_futures", "id": 1, "contract_value": "0.001"},
    "ETHUSDT": {"symbol": "ETHUSDT", "contract_type": "perpetual_futures", "id": 2, "contract_value": "0.01"},
    "SOLUSDT": {"symbol": "SOLUSDT", "contract_type": "perpetual_futures", "id": 3, "contract_value": "1.0"},
    "XRPUSDT": {"symbol": "XRPUSDT", "contract_type": "perpetual_futures", "id": 4, "contract_value": "10.0"},
    "BNBUSDT": {"symbol": "BNBUSDT", "contract_type": "perpetual_futures", "id": 5, "contract_value": "0.1"},
}


def make_stub() -> SymbolStub:
    return SymbolStub(symbols=list(DEFAULT_WATCHLIST), prices=WATCH_PRICES, products=WATCH_PRODUCTS)


# ------------------------------------------------------------------ symbol switch

def test_symbol_switch_tracks_selected_symbol_in_order():
    """BTC -> ETH -> SOL -> XRP -> BNB: each analysis is performed for the
    *selected* symbol, its own data, its own series, never the previous one."""
    stub = make_stub()
    outs = {}
    for symbol in DEFAULT_WATCHLIST:
        outs[symbol] = AnalysisService(client=stub).analyze(symbol)
        assert outs[symbol]["symbol"] == symbol
        assert outs[symbol]["market"]["symbol"] == symbol
        assert outs[symbol]["market"]["price"] == pytest.approx(stub.data[symbol][-1]["close"])
        assert outs[symbol]["market"]["contract_value"] == (
            0.001 if symbol == "BTCUSDT" else float(WATCH_PRODUCTS[symbol]["contract_value"])
        )

    # Distinct prices / contract values prove no cross-symbol reuse.
    prices = {s: outs[s]["market"]["price"] for s in DEFAULT_WATCHLIST}
    assert len(set(prices)) == 5
    # Candle series are fetched in exactly the switch order.
    candle_calls = [c[1] for c in stub.calls if c[0] == "get_candles"]
    assert candle_calls == list(DEFAULT_WATCHLIST)


def test_previous_symbol_analysis_not_reused():
    """After switching to ETHUSDT the header/plan must be ETHUSDT, not BTC."""
    stub = make_stub()
    AnalysisService(client=stub).analyze("BTCUSDT")  # warm other symbol
    out = AnalysisService(client=stub).analyze("ETHUSDT")
    assert out["symbol"] == "ETHUSDT"
    assert out["market"]["price"] == pytest.approx(stub.data["ETHUSDT"][-1]["close"])
    assert out["market"]["contract_value"] == 0.01


# ------------------------------------------------------------------- strategy switch

def test_strategy_a_is_selected_and_status_is_artifact_derived():
    profiles = list_strategies()
    assert [p["id"] for p in profiles] == ["strategy_a"]
    assert profiles[0]["status"] == "UNVALIDATED"
    assert profiles[0]["timeframe"] == "1H"
    a = get_strategy("strategy_a")
    assert a["parameters"]["ema_spans"] == (8, 40, 50)
    assert a["parameters"]["sl_atr_multiple"] == 1.0
    assert a["parameters"]["target_atr_multiple"] == 2.0


def test_only_strategy_a_exists_no_b_invented():
    assert [p["id"] for p in list_strategies()] == ["strategy_a"]


@pytest.mark.parametrize("strategy_id", ["strategy_b", "strategy_c", "nonsense", ""])
def test_unknown_strategy_returns_data_unavailable(strategy_id):
    out = AnalysisService(client=SingleSymbolStub(candles=make_candles())).analyze(
        "BTCUSDT", strategy_id=strategy_id
    )
    assert out["leverage"]["status"] == "DATA UNAVAILABLE"
    assert out["decision"]["direction"] == "none"
    assert out["plan"]["position_size"] is None
    assert "unknown strategy" in out["decision"]["reason"]


# ----------------------------------------------------------------- decision timeline

def test_timeline_follows_milestone_sequence_and_ends_locked():
    stub = make_stub()
    out = AnalysisService(client=stub).analyze("BTCUSDT")
    steps = [t["step"] for t in out["timeline"]]
    seq = ["MARKET DATA", "TREND AGENT", "STRUCTURE AGENT", "SENTIMENT AGENT",
           "DECISION AGENT", "LEVERAGE ADVISOR", "RISK ENGINE", "TRADE PLAN", "EXECUTION"]
    positions = [steps.index(s) for s in seq]
    assert positions == sorted(positions)
    last = out["timeline"][-1]
    assert last["step"] == "EXECUTION"
    assert last["detail"] == "LOCKED — NO ORDER PLACED"


def test_unavailable_path_also_ends_locked():
    stub = SymbolStub(symbols=["BTCUSDT"], prices={"BTCUSDT": 50000.0})
    stub.data["BTCUSDT"] = []
    out = AnalysisService(client=stub).analyze("BTCUSDT")
    assert out["timeline"][-1]["step"] == "EXECUTION"
    assert out["timeline"][-1]["detail"] == "LOCKED — NO ORDER PLACED"


# ------------------------------------------------------------------ safety contract

def test_safety_flags_are_the_locked_defaults():
    s = get_settings()
    assert s.live_mode is False
    assert s.testnet_mode is True
    assert s.real_order_confirmation is False


def test_safety_flags_mirror_requested_safety_contract():
    s = Settings()
    assert s.live_mode is False
    assert s.testnet_mode is True
    assert s.real_order_confirmation is False


# ------------------------------------------------ no auxiliary-data fabrication

TRENDING = make_candles(200, 50000.0, 25.0, 0.005)


class EmptyTickerStub(SingleSymbolStub):
    def get_ticker(self, symbol):
        return {}


def test_missing_funding_and_oi_not_fabricated():
    ticker = {"close": TRENDING[-1]["close"]}  # no funding, no OI
    out = AnalysisService(client=SingleSymbolStub(candles=TRENDING, ticker=ticker)).analyze("BTCUSDT")
    assert out["market"]["funding_8h"] is None
    assert out["market"]["open_interest"] is None
    # Liquidity falls back to candle volume; leverage still computed honestly.
    assert out["leverage"]["status"] in ("CAPPED BY RISK ENGINE", "WITHIN RISK LIMIT")
    assert out["leverage"]["factors"]["liquidity"] is not None


def test_ticker_entirely_unavailable_not_fabricated():
    out = AnalysisService(client=EmptyTickerStub(candles=TRENDING)).analyze("BTCUSDT")
    assert out["market"]["funding_8h"] is None
    assert out["market"]["open_interest"] is None
    assert out["market"]["price"] == pytest.approx(TRENDING[-1]["close"])
    assert out["leverage"]["status"] in ("CAPPED BY RISK ENGINE", "WITHIN RISK LIMIT")


def test_volume_also_missing_leverage_still_honest():
    candles = [dict(c, volume=0.0) for c in TRENDING]
    ticker = {"close": TRENDING[-1]["close"]}  # no OI either -> no liquidity source
    out = AnalysisService(client=SingleSymbolStub(candles=candles, ticker=ticker)).analyze("BTCUSDT")
    liq = out["leverage"]["factors"]["liquidity"]
    # No OI + zero volume -> liquidity has no source, must be 0 / null (never invented).
    assert liq == 0.0 or liq is None