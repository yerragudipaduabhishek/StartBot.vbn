"""Instrument registry: a curated, extensible catalog plus search.

The catalog is *metadata only*. Crypto perpetuals are real Delta Exchange
instruments; the on-chain availability, product id and contract value are
overlaid by the caller (AnalysisService) from real Delta products. Indian
instruments are registered with exchange/market/asset-type metadata so a future
provider can light them up - they carry no provider today and are never given
fabricated market data.
"""

from __future__ import annotations

import logging

from app.analysis.config import DEFAULT_WATCHLIST
from app.data.delta_client import DeltaClient
from app.instruments.model import (
    ASSET_EQUITY,
    ASSET_FUTURE,
    ASSET_INDEX,
    ASSET_OPTION,
    ASSET_PERPETUAL,
    CAP_CANDLES,
    CAP_FUNDING,
    CAP_OPEN_INTEREST,
    CAP_TICKER,
    DATA_STATUS_AVAILABLE,
    DATA_STATUS_UNAVAILABLE,
    EXCHANGE_DELTA,
    EXCHANGE_NSE,
    MARKET_CRYPTO,
    MARKET_INDIA,
    OPTION_CE,
    OPTION_PE,
    SETTLEMENT_CASH,
    Instrument,
)

logger = logging.getLogger(__name__)

# Capabilities the Delta exchange provides today for perpetual-futures products.
DELTA_PERP_CAPABILITIES = (CAP_TICKER, CAP_CANDLES, CAP_OPEN_INTEREST, CAP_FUNDING)


def _crypto_perp(symbol: str, base: str, display: str) -> Instrument:
    return Instrument(
        id=f"crypto:{EXCHANGE_DELTA.lower()}:{symbol}:perpetual",
        symbol=symbol,
        display_name=display,
        exchange=EXCHANGE_DELTA,
        market=MARKET_CRYPTO,
        asset_type=ASSET_PERPETUAL,
        base_asset=base,
        quote_currency="USDT",
        tick_size=None,
        lot_size=None,
        contract_size=None,
        settlement="USDT",
        data_provider="delta",
        capabilities=DELTA_PERP_CAPABILITIES,
        data_status=DATA_STATUS_UNAVAILABLE,
    )


def _india_equity(symbol: str, display: str) -> Instrument:
    return Instrument(
        id=f"nse:{symbol}:equity",
        symbol=symbol,
        display_name=display,
        exchange=EXCHANGE_NSE,
        market=MARKET_INDIA,
        asset_type=ASSET_EQUITY,
        base_asset=symbol,
        quote_currency="INR",
        tick_size=None,
        lot_size=1.0,
        contract_size=None,
        settlement=SETTLEMENT_CASH,
        data_provider=None,
        capabilities=(),
        data_status=DATA_STATUS_UNAVAILABLE,
    )


def default_catalog() -> list[Instrument]:
    """Curated registry catalog. Crypto entries follow DEFAULT_WATCHLIST order;
    Indian entries demonstrate the extensible market types (no live provider)."""
    crypto = [
        _crypto_perp("BTCUSDT", "BTC", "Bitcoin Perpetual"),
        _crypto_perp("ETHUSDT", "ETH", "Ethereum Perpetual"),
        _crypto_perp("SOLUSDT", "SOL", "Solana Perpetual"),
        _crypto_perp("XRPUSDT", "XRP", "XRP Perpetual"),
        _crypto_perp("BNBUSDT", "BNB", "BNB Perpetual"),
    ]
    india = [
        _india_equity("RELIANCE", "Reliance Industries"),
        _india_equity("TCS", "Tata Consultancy Services"),
        _india_equity("INFY", "Infosys"),
        _india_equity("HDFCBANK", "HDFC Bank"),
        _india_equity("SBI", "State Bank of India"),
        Instrument(
            id="nse:NIFTY50:index",
            symbol="NIFTY50",
            display_name="NIFTY 50 Index",
            exchange=EXCHANGE_NSE,
            market=MARKET_INDIA,
            asset_type=ASSET_INDEX,
            base_asset="NIFTY",
            quote_currency="INR",
            tick_size=None,
            lot_size=None,
            contract_size=None,
            settlement=SETTLEMENT_CASH,
            data_provider=None,
            capabilities=(),
            data_status=DATA_STATUS_UNAVAILABLE,
        ),
        Instrument(
            id="nse:NIFTY:future",
            symbol="NIFTYFUT",
            display_name="NIFTY Futures",
            exchange=EXCHANGE_NSE,
            market=MARKET_INDIA,
            asset_type=ASSET_FUTURE,
            base_asset="NIFTY",
            quote_currency="INR",
            tick_size=None,
            lot_size=None,
            contract_size=None,
            underlying="NIFTY",
            settlement=SETTLEMENT_CASH,
            data_provider=None,
            capabilities=(),
            data_status=DATA_STATUS_UNAVAILABLE,
        ),
        Instrument(
            id="nse:NIFTY:option:ce",
            symbol="NIFTYCE",
            display_name="NIFTY Call Option",
            exchange=EXCHANGE_NSE,
            market=MARKET_INDIA,
            asset_type=ASSET_OPTION,
            base_asset="NIFTY",
            quote_currency="INR",
            tick_size=None,
            lot_size=None,
            contract_size=None,
            underlying="NIFTY",
            option_type=OPTION_CE,
            settlement=SETTLEMENT_CASH,
            data_provider=None,
            capabilities=(),
            data_status=DATA_STATUS_UNAVAILABLE,
        ),
        Instrument(
            id="nse:NIFTY:option:pe",
            symbol="NIFTYPE",
            display_name="NIFTY Put Option",
            exchange=EXCHANGE_NSE,
            market=MARKET_INDIA,
            asset_type=ASSET_OPTION,
            base_asset="NIFTY",
            quote_currency="INR",
            tick_size=None,
            lot_size=None,
            contract_size=None,
            underlying="NIFTY",
            option_type=OPTION_PE,
            settlement=SETTLEMENT_CASH,
            data_provider=None,
            capabilities=(),
            data_status=DATA_STATUS_UNAVAILABLE,
        ),
    ]
    return crypto + india


class InstrumentRegistry:
    """Holds the instrument catalog and answers lookup/search questions."""

    def __init__(self, catalog: list[Instrument] | None = None) -> None:
        self._catalog = list(catalog if catalog is not None else default_catalog())
        self._by_id = {i.id: i for i in self._catalog}
        self._by_symbol: dict[str, Instrument] = {}
        for i in self._catalog:
            self._by_symbol.setdefault(i.symbol, i)

    # -------------------------------------------------------------- lookups

    def list_all(self) -> list[dict]:
        return [i.to_dict() for i in self._catalog]

    def get(self, instrument_id: str | None) -> Instrument | None:
        if not instrument_id:
            return None
        return self._by_id.get(instrument_id)

    def by_symbol(self, symbol: str | None) -> Instrument | None:
        if not symbol:
            return None
        return self._by_symbol.get(symbol.upper())

    def resolve(self, ref: str | None) -> Instrument | None:
        """Resolve a reference that may be either an instrument id or a symbol."""
        if not ref:
            return None
        return self.get(ref) or self.by_symbol(ref)

    # ---------------------------------------------------------------- search

    def search(
        self,
        q: str = "",
        market: str | None = None,
        asset_type: str | None = None,
        exchange: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        query = (q or "").strip().upper()
        out: list[Instrument] = []
        for i in self._catalog:
            if market and i.market != market:
                continue
            if asset_type and i.asset_type != asset_type:
                continue
            if exchange and i.exchange.lower() != exchange.lower():
                continue
            if query:
                haystack = f"{i.symbol} {i.display_name} {i.base_asset or ''} {i.id}".upper()
                if query not in haystack:
                    continue
            out.append(i)
        return [i.to_dict() for i in out[:limit]]

    def default_symbol_ids(self) -> list[str]:
        """Registry ids for the legacy DEFAULT_WATCHLIST symbols, in order."""
        return [i.id for s in DEFAULT_WATCHLIST if (i := self.by_symbol(s)) is not None]

    # --------------------------------------------------------------- enrich

    @staticmethod
    def enrich_listing(entries: list[dict]) -> list[dict]:
        """Overlay normalized registry metadata onto a legacy listing without
        changing the entry set or the error contract of the caller."""
        reg = InstrumentRegistry()
        out: list[dict] = []
        for entry in entries:
            inst = reg.by_symbol(entry.get("symbol"))
            if inst is None:
                out.append(entry)
                continue
            merged = inst.to_dict()
            merged["available"] = bool(entry.get("available"))
            merged["product_id"] = entry.get("product_id")
            merged["contract_value"] = entry.get("contract_value")
            merged["data_status"] = DATA_STATUS_AVAILABLE if merged["available"] else DATA_STATUS_UNAVAILABLE
            out.append(merged)
        return out

    @staticmethod
    def with_availability(rows: list[dict], availability: dict[str, bool]) -> list[dict]:
        """Truthfully annotate search/catalog rows with provider availability."""
        for row in rows:
            ok = bool(availability.get(row.get("symbol"), False))
            row["available"] = ok
            row["data_status"] = DATA_STATUS_AVAILABLE if ok else DATA_STATUS_UNAVAILABLE
        return rows

    # ------------------------------------------------------------ providers

    @staticmethod
    def delta_client() -> DeltaClient:
        from app.config.settings import get_settings

        settings = get_settings()
        return DeltaClient(
            settings.delta_market_data_url,
            settings=settings,
            read_fallback_urls=settings.delta_market_data_fallback_urls,
        )