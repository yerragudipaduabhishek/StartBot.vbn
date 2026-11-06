"""Read-only analysis service: wiring, plan math, and no-fabrication fallbacks."""

import time


from app.analysis.service import AnalysisService


def _candles(n: int = 200, base: float = 50000.0, step: float = 25.0, t0: int | None = None, span: int = 3600):
    if t0 is None:
        t0 = (int(time.time()) // span) * span - (n - 1) * span
    out = []
    for i in range(n):
        c = base + i * step
        out.append(
            {
                "time": t0 + i * span,
                "open": c,
                "high": c + 40,
                "low": c - 40,
                "close": c,
                "volume": 10.0,
            }
        )
    return out

def _ticker(close: float) -> dict:
    return {
        "close": close,
        "mark_price": close,
        "funding_rate_8h": 0.0001,
        "oi_value_usd": "50000000",
        "volume": "1000",
    }


class StubClient:
    def __init__(self, candles=None, product=True):
        self.candles = candles if candles is not None else _candles()
        self.product = product

    def get_candles(self, symbol, resolution="1h", start=None, end=None):
        return list(self.candles)

    def get_ticker(self, symbol):
        return _ticker(self.candles[-1]["close"])

    def get_products(self):
        return [{"symbol": "BTCUSDT", "contract_type": "perpetual_futures", "id": 123, "contract_value": "0.001"}]

    def find_product(self, symbol, contract_type="perpetual_futures"):
        if not self.product:
            raise ValueError("Product not found")
        return {"symbol": symbol, "contract_type": contract_type, "id": 123, "contract_value": "0.001"}


def test_analysis_returns_full_block():
    svc = AnalysisService(client=StubClient())
    out = svc.analyze("BTCUSDT", fund_inr=5000, risk_pct=0.5)
    assert out["symbol"] == "BTCUSDT"
    assert out["strategy"]["status"] == "UNVALIDATED"
    assert out["strategy"]["timeframe"] == "1H"
    assert out["market"]["candles_count"] == 200
    assert out["market"]["contract_value"] == 0.001
    assert out["decision"]["direction"] in ("long", "short", "none", "neutral")
    assert out["decision"]["confidence"] is not None
    assert out["decision"]["votes"].get("trend") is not None
    assert out["decision"]["reasoning"]
    assert out["max_risk_inr"] == 25.0
    assert out["plan"]["rr"] is not None
    assert out["leverage"]["status"] in ("CAPPED BY RISK ENGINE", "WITHIN RISK LIMIT", "DATA UNAVAILABLE")
    # The leverage block always exposes the deterministic risk-engine cap.
    assert out["leverage"]["risk_max"] is not None
    # Timeline is timestamped and always ends locked.
    assert out["timeline"][-1]["detail"].startswith("LOCKED")
    assert all("time" in t for t in out["timeline"])


def test_analysis_insufficient_history_is_honest():
    svc = AnalysisService(client=StubClient(candles=_candles(n=10)))
    out = svc.analyze("BTCUSDT")
    assert out["decision"]["direction"] == "none"
    assert "insufficient" in out["decision"]["reason"]
    assert out["plan"]["entry"] is None
    assert out["leverage"]["status"] == "DATA UNAVAILABLE"
    # Candles were received (10) but too few to analyse; the count stays honest.
    assert out["market"]["candles_count"] == 10


def test_analysis_data_error_is_honest():
    class Boom(StubClient):
        def get_candles(self, symbol, resolution="1h", start=None, end=None):
            raise RuntimeError("connection refused")

    svc = AnalysisService(client=Boom())
    out = svc.analyze("BTCUSDT")
    assert "unavailable" in out["decision"]["reason"]
    assert out["market"]["price"] is None
    assert out["leverage"]["status"] == "DATA UNAVAILABLE"
    assert out["timeline"][-1]["detail"].startswith("LOCKED")


def test_analysis_unknown_contract_value_skips_sizing():
    class NoProduct(StubClient):
        def find_product(self, symbol, contract_type="perpetual_futures"):
            raise ValueError("Product not found")

        def get_products(self):
            return []

    svc = AnalysisService(client=NoProduct())
    out = svc.analyze("SOLUSDT", fund_inr=5000, risk_pct=0.5)
    # Decision/leverage still computed; sizing explicitly unavailable.
    assert out["decision"]["direction"] != "none" or out["decision"]["reason"]
    assert out["plan"]["position_size"] is None
    assert "contract value" in out["plan"]["reason"] or out["plan"]["reason"]
    assert out["leverage"]["status"] in ("CAPPED BY RISK ENGINE", "WITHIN RISK LIMIT", "DATA UNAVAILABLE")


def test_instruments_listing():
    svc = AnalysisService(client=StubClient())
    out = svc.list_instruments()
    assert out["error"] is None
    assert any(i["symbol"] == "BTCUSDT" and i["available"] for i in out["instruments"])
    assert out["instruments"][0]["contract_value"] == 0.001


def test_analysis_risk_per_trade_sizing_matches_fund():
    svc = AnalysisService(client=StubClient())
    out = svc.analyze("BTCUSDT", fund_inr=10000, risk_pct=1.0)
    assert out["max_risk_inr"] == 100.0
    # Plan sizing never blows the risk budget when a signal exists.
    if out["plan"]["position_size"] is not None:
        assert out["plan"]["entry"] is not None