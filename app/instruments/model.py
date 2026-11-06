"""Normalized instrument model shared by the registry, watchlist and providers.

Every instrument is described with the same field set. Fields that a given
instrument type does not use are left nullable (None) so the schema is
extensible without inventing values:

- crypto spot        market=crypto   asset_type=spot
- crypto perpetual   market=crypto   asset_type=perpetual
- Indian equity      market=india    asset_type=equity
- Indian index       market=india    asset_type=index
- Indian future      market=india    asset_type=future
- Indian option      market=india    asset_type=option
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MARKET_CRYPTO = "crypto"
MARKET_INDIA = "india"

ASSET_SPOT = "spot"
ASSET_PERPETUAL = "perpetual"
ASSET_EQUITY = "equity"
ASSET_INDEX = "index"
ASSET_FUTURE = "future"
ASSET_OPTION = "option"

OPTION_CE = "CE"
OPTION_PE = "PE"

EXCHANGE_DELTA = "Delta"
EXCHANGE_NSE = "NSE"

SETTLEMENT_CASH = "cash"

DATA_STATUS_AVAILABLE = "available"
DATA_STATUS_UNAVAILABLE = "unavailable"

# Capabilities a provider may declare for an instrument. Absent capabilities
# mean the data is honestly reported as unavailable (see app/instruments/providers).
CAP_TICKER = "ticker"
CAP_CANDLES = "candles"
CAP_OPEN_INTEREST = "open_interest"
CAP_FUNDING = "funding"
CAP_ORDER_BOOK = "order_book"
CAP_OPTIONS_CHAIN = "options_chain"


@dataclass(frozen=True)
class Instrument:
    """A single tradeable/analysable instrument with normalized metadata."""

    id: str
    symbol: str
    display_name: str
    exchange: str
    market: str
    asset_type: str
    base_asset: str | None
    quote_currency: str | None
    tick_size: float | None
    lot_size: float | None
    contract_size: float | None
    expiry: str | None = None
    strike: float | None = None
    option_type: str | None = None
    underlying: str | None = None
    settlement: str | None = None
    data_provider: str | None = None
    capabilities: tuple[str, ...] = field(default_factory=tuple)
    data_status: str = DATA_STATUS_UNAVAILABLE

    def to_dict(self) -> dict[str, Any]:
        """Public dict form. Unsupported fields are explicitly null - the
        registry never fills them with invented values."""
        return {
            "id": self.id,
            "symbol": self.symbol,
            "display_name": self.display_name,
            "exchange": self.exchange,
            "market": self.market,
            "asset_type": self.asset_type,
            "base_asset": self.base_asset,
            "quote_currency": self.quote_currency,
            "tick_size": self.tick_size,
            "lot_size": self.lot_size,
            "contract_size": self.contract_size,
            "expiry": self.expiry,
            "strike": self.strike,
            "option_type": self.option_type,
            "underlying": self.underlying,
            "settlement": self.settlement,
            "data_provider": self.data_provider,
            "capabilities": list(self.capabilities),
            "data_status": self.data_status,
            "available": self.data_status == DATA_STATUS_AVAILABLE,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Instrument":
        return cls(
            id=str(data["id"]),
            symbol=str(data["symbol"]),
            display_name=str(data.get("display_name") or data["symbol"]),
            exchange=str(data.get("exchange") or ""),
            market=str(data.get("market") or ""),
            asset_type=str(data.get("asset_type") or ""),
            base_asset=data.get("base_asset"),
            quote_currency=data.get("quote_currency"),
            tick_size=data.get("tick_size"),
            lot_size=data.get("lot_size"),
            contract_size=data.get("contract_size"),
            expiry=data.get("expiry"),
            strike=data.get("strike"),
            option_type=data.get("option_type"),
            underlying=data.get("underlying"),
            settlement=data.get("settlement"),
            data_provider=data.get("data_provider"),
            capabilities=tuple(data.get("capabilities") or ()),
            data_status=data.get("data_status", DATA_STATUS_UNAVAILABLE),
        )