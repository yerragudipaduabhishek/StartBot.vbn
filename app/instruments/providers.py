"""Market-data provider adapter interface.

The existing Delta pipeline (MarketData / AnalysisService / MarketHealthMonitor)
keeps running through its current delta_client code path untouched. This module
adds a *normalized* adapter contract so future providers (Indian equity etc.)
can be plugged in later. Providers that cannot supply a field must return a
"DATA UNAVAILABLE" value - prices, OI, funding, volume and contract metadata are
never fabricated.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from app.data.delta_client import DeltaClient
from app.instruments.model import Instrument

logger = logging.getLogger(__name__)


def _num(value) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


class MarketDataProvider(ABC):
    """Normalized public market-data adapter interface.

    Core capabilities (instrument metadata, ticker, candles) are required.
    Optional capabilities (open interest, funding, order book, options chain)
    default to a truthful DATA UNAVAILABLE response.
    """

    name: str = "provider"

    @abstractmethod
    def get_instrument_metadata(self, instrument: Instrument) -> dict:
        """Normalized instrument metadata (registry fields + provider extras)."""

    @abstractmethod
    def get_ticker(self, instrument: Instrument) -> dict:
        """Latest ticker as a dict of normalized keys; {} if unavailable."""

    @abstractmethod
    def get_candles(
        self,
        instrument: Instrument,
        resolution: str = "1h",
        start: int | None = None,
        end: int | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """OHLCV candles; [] if unavailable."""

    # ---- optional capabilities (default: DATA UNAVAILABLE) --------------

    def get_open_interest(self, instrument: Instrument) -> float | None:
        return None

    def get_funding(self, instrument: Instrument) -> float | None:
        return None

    def get_order_book(self, instrument: Instrument) -> dict | None:
        return None

    def get_options_chain(self, instrument: Instrument) -> dict | None:
        return None

    def supports(self, instrument: Instrument, capability: str) -> bool:
        """True when the instrument/provider combination declares a capability.
        Undeclared capabilities are treated as truthful DATA UNAVAILABLE."""
        return capability in instrument.capabilities


class UnavailableProvider(MarketDataProvider):
    """Honest sink for instruments with no connected provider. Every field is
    reported as unavailable - never a guessed number."""

    name = "unavailable"

    def get_instrument_metadata(self, instrument: Instrument) -> dict:
        return instrument.to_dict()

    def get_ticker(self, instrument: Instrument) -> dict:
        return {}

    def get_candles(self, instrument: Instrument, resolution="1h", start=None, end=None, limit=None) -> list[dict]:
        return []

    def get_open_interest(self, instrument: Instrument) -> float | None:
        return None

    def get_funding(self, instrument: Instrument) -> float | None:
        return None

    def get_order_book(self, instrument: Instrument) -> dict | None:
        return None

    def get_options_chain(self, instrument: Instrument) -> dict | None:
        return None


class DeltaProvider(MarketDataProvider):
    """Thin adapter over the existing DeltaClient public reads.

    Delegates to the validated client code path; never re-implements exchange
    specifics. Order book / options chain are not published by Delta's public
    REST feed used here, so they honestly return None.
    """

    name = "delta"

    def __init__(self, client: DeltaClient | None = None) -> None:
        from app.instruments.registry import InstrumentRegistry

        self.client = client or InstrumentRegistry.delta_client()

    def get_instrument_metadata(self, instrument: Instrument) -> dict:
        return instrument.to_dict()

    def get_ticker(self, instrument: Instrument) -> dict:
        try:
            return self.client.get_ticker(instrument.symbol) or {}
        except Exception as e:  # noqa: BLE001 - surfaced as unavailable
            logger.warning("delta ticker unavailable for %s: %s", instrument.symbol, e)
            return {}

    def get_candles(self, instrument: Instrument, resolution="1h", start=None, end=None, limit=None) -> list[dict]:
        try:
            kwargs: dict = {"resolution": resolution, "start": start, "end": end}
            if limit is not None:
                kwargs["limit"] = limit
            candles = self.client.get_candles(instrument.symbol, **kwargs)
            return list(candles or [])
        except Exception as e:  # noqa: BLE001
            logger.warning("delta candles unavailable for %s: %s", instrument.symbol, e)
            return []

    def get_open_interest(self, instrument: Instrument) -> float | None:
        ticker = self.get_ticker(instrument)
        return _num(ticker.get("oi_value_usd") if ticker.get("oi_value_usd") is not None else ticker.get("oi_contracts"))

    def get_funding(self, instrument: Instrument) -> float | None:
        ticker = self.get_ticker(instrument)
        return _num(ticker.get("funding_rate_8h") if ticker.get("funding_rate_8h") is not None else ticker.get("funding_rate"))

    def get_order_book(self, instrument: Instrument) -> dict | None:
        return None

    def get_options_chain(self, instrument: Instrument) -> dict | None:
        return None