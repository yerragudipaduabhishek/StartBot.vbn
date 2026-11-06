"""Adaptive Instrument Registry + Dynamic Watchlist.

Read-only architecture: registry metadata, watchlist persistence and provider
adapters. Nothing in this package places an order, touches an authenticated
account, or fabricates market data.
"""

from app.instruments.model import Instrument
from app.instruments.registry import InstrumentRegistry
from app.instruments.providers import DeltaProvider, MarketDataProvider, UnavailableProvider
from app.instruments.strategy_compat import StrategyCompatibility

__all__ = [
    "DeltaProvider",
    "Instrument",
    "InstrumentRegistry",
    "MarketDataProvider",
    "StrategyCompatibility",
    "UnavailableProvider",
]