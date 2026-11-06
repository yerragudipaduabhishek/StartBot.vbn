"""Deterministic fixtures shared by Milestone 3 validation tests.

Everything here is synthetic and reproducible. Candle shapes target specific
trend / volatility regimes so the tests validate the *existing* analysis
behaviour instead of random data.
"""

import math
import time

LEGACY_SUPPORT: list[str] = ["BTC", "ETH", "SOL", "XRP", "BNB"]


def make_candles(
    n: int = 200,
    base: float = 50000.0,
    step: float = 0.0,
    vol: float = 0.005,
    shape: str = "linear",
    t0: int | None = None,
    span: int = 3600,
) -> list[dict]:
    """OHLCV list; ATR% is ~100 * vol (verified empirically: vol=0.005 -> ~1%).

    By default the candles end at the current 1h boundary so the analysis
    service treats them as fresh (it rejects stale feeds as DATA UNAVAILABLE).
    """
    if t0 is None:
        # Align the last candle to "now" (current hour), exactly like live data.
        t0 = (int(time.time()) // span) * span - (n - 1) * span
    out = []
    for i in range(n):
        if shape == "linear":
            c = base + i * step
        elif shape == "sine":
            c = base + math.sin(i / 8.0) * base * 0.002
        elif shape == "pause_up":
            c = base + int(i / 5) * 10
        elif shape == "pause_down":
            c = base - int(i / 5) * 10
        else:
            c = base + i * step
        half = c * vol
        out.append(
            {
                "time": t0 + i * span,
                "open": c,
                "high": c + half,
                "low": c - half,
                "close": c,
                "volume": 1000.0,
            }
        )
    return out


def make_ticker(close: float, funding: float = 0.0001, oi: str = "50000000", volume: str = "1000") -> dict:
    return {
        "close": close,
        "mark_price": close,
        "funding_rate_8h": funding,
        "oi_value_usd": oi,
        "volume": volume,
    }


class SymbolStub:
    """Per-symbol Delta-style stub keyed by symbol, so cross-symbol leakage
    (e.g. wrongly showing BTC under ETH) is impossible to hide.

    `products` may omit a symbol (product unavailable) and `prices` sets the
    base price of each symbol's synthetic candles.
    """

    def __init__(
        self,
        symbols: list[str],
        prices: dict[str, float],
        products: dict[str, dict] | None = None,
        vol: float = 0.005,
        n: int = 200,
        tickers: dict[str, dict] | None = None,
        fails: set[str] | None = None,
    ) -> None:
        self.fails = fails or set()
        self.data: dict[str, list[dict]] = {
            s: make_candles(n=n, base=prices[s], step=prices[s] * 0.0005, vol=vol) for s in symbols
        }
        self.tickers = tickers if tickers is not None else {s: make_ticker(prices[s]) for s in symbols}
        self.products = products if products is not None else {
            s: {"symbol": s, "contract_type": "perpetual_futures", "id": i + 1, "contract_value": "0.001"} for i, s in enumerate(symbols)
        }
        self.calls: list[tuple[str, str]] = []

    def get_candles(self, symbol: str, resolution: str = "1h", start=None, end=None) -> list[dict]:
        self.calls.append(("get_candles", symbol))
        if symbol in self.fails:
            raise RuntimeError(f"connection refused for {symbol}")
        if symbol not in self.data:
            return []
        return list(self.data[symbol])

    def get_ticker(self, symbol: str) -> dict:
        self.calls.append(("get_ticker", symbol))
        if symbol in self.fails:
            raise RuntimeError(f"connection refused for {symbol}")
        return dict(self.tickers.get(symbol, {}))

    def get_products(self) -> list[dict]:
        self.calls.append(("get_products", "*"))
        return [dict(p) for p in self.products.values()]

    def find_product(self, symbol: str, contract_type: str = "perpetual_futures") -> dict:
        for p in self.products.values():
            if p.get("symbol") == symbol:
                self.calls.append(("find_product", symbol))
                return dict(p)
        raise ValueError(f"Product not found: {symbol} ({contract_type})")


class SingleSymbolStub:
    """Simple single-symbol stub that mimics the Milestone 2 StubClient."""

    def __init__(self, candles=None, product=None, ticker=None, fail_candles: bool = False) -> None:
        self.candles = candles if candles is not None else make_candles()
        self.product = product or {"symbol": "BTCUSDT", "contract_type": "perpetual_futures", "id": 1, "contract_value": "0.001"}
        self.ticker = ticker or make_ticker(self.candles[-1]["close"])
        self.fail_candles = fail_candles

    def get_candles(self, symbol: str, resolution: str = "1h", start=None, end=None) -> list[dict]:
        if self.fail_candles:
            raise RuntimeError("connection refused")
        return list(self.candles)

    def get_ticker(self, symbol: str) -> dict:
        return dict(self.ticker)

    def get_products(self) -> list[dict]:
        return [dict(self.product)]

    def find_product(self, symbol: str, contract_type: str = "perpetual_futures") -> dict:
        return dict(self.product)