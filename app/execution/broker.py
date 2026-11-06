"""Broker-neutral execution interfaces for Phase 4.

The paper and Delta testnet brokers share one protocol so the order manager can
be pointed at either venue without branching. The live broker is intentionally
impossible to construct; live execution remains disabled.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, Sequence

from app.contracts import ExecutionMode, OrderRequest, OrderResult, RiskDecision


class BrokerError(RuntimeError):
    pass


class BrokerNotConfigured(BrokerError):
    pass


class LiveBrokerDisabled(BrokerError):
    pass


@dataclass(frozen=True)
class BrokerCapabilities:
    venue: str
    supports_cancel: bool = True
    supports_order_lookup: bool = True
    supports_positions: bool = True
    supports_balances: bool = True
    supports_partial_fills: bool = False
    simulated: bool = False


class Broker(Protocol):
    venue: str
    capabilities: BrokerCapabilities

    def place_order(self, request: OrderRequest, decision: RiskDecision) -> OrderResult: ...

    def cancel_order(self, order_id: str) -> OrderResult | None: ...

    def get_order(self, order_id: str) -> dict | None: ...

    def get_positions(self) -> Sequence[dict]: ...

    def get_balances(self) -> Sequence[dict]: ...


class PaperBroker:
    """Local simulated broker with no exchange client and no credentials."""

    venue = "paper"

    def __init__(
        self,
        profile: Any | None = None,
        paper: Any | None = None,
        *,
        adapter: Any | None = None,
    ) -> None:
        if adapter is None:
            if profile is None:
                raise BrokerNotConfigured("paper broker requires a trading profile")
            from app.execution.adapters import PaperExecutionAdapter
            from app.trading.paper_trader import PaperTrader

            paper = paper or PaperTrader(profile=profile)
            adapter = PaperExecutionAdapter(paper, profile)
        self.adapter = adapter
        self.paper = getattr(adapter, "paper", paper)
        self.capabilities = BrokerCapabilities(
            venue=self.venue,
            supports_partial_fills=False,
            simulated=True,
        )

    def place_order(self, request: OrderRequest, decision: RiskDecision) -> OrderResult:
        return self.adapter.submit(request, decision)

    def cancel_order(self, order_id: str) -> OrderResult | None:
        return None

    def get_order(self, order_id: str) -> dict | None:
        return None

    def get_positions(self) -> list[dict]:
        position = getattr(self.paper, "position", None)
        if not position:
            return []
        return [
            {
                "symbol": position.get("symbol"),
                "side": position.get("side"),
                "size": position.get("size"),
                "entry_price": position.get("entry_price"),
                "venue": self.venue,
            }
        ]

    def get_balances(self) -> list[dict]:
        balance = float(getattr(self.paper, "balance", 0.0) or 0.0)
        return [{"asset": "INR", "balance": balance, "venue": self.venue}]


class DeltaTestnetBroker:
    """Contained exchange broker. Refuses to exist unless testnet-only flags hold."""

    venue = "delta-testnet"

    def __init__(
        self,
        profile: Any | None = None,
        settings: Any | None = None,
        *,
        adapter: Any | None = None,
        client: Any | None = None,
    ) -> None:
        from app.config.settings import get_settings

        self.settings = settings or get_settings()
        if getattr(self.settings, "live_mode", False):
            raise BrokerError("DeltaTestnetBroker refuses live_mode")
        if not getattr(self.settings, "testnet_mode", True):
            raise BrokerError("DeltaTestnetBroker requires testnet_mode")
        if profile is not None:
            from app.execution.adapters import TestnetExecutionAdapter
            from app.execution.modes import normalize_mode
            from app.contracts import ExecutionMode

            if normalize_mode(profile.mode) != ExecutionMode.TESTNET:
                raise BrokerError("DeltaTestnetBroker requires a testnet profile")
            adapter = adapter or TestnetExecutionAdapter(profile, self.settings)
        self.adapter = adapter
        self.client = client or getattr(adapter, "client", None)
        self.capabilities = BrokerCapabilities(
            venue=self.venue,
            supports_partial_fills=True,
            simulated=False,
        )

    def place_order(self, request: OrderRequest, decision: RiskDecision) -> OrderResult:
        if self.adapter is None:
            raise BrokerNotConfigured("testnet broker has no execution adapter")
        return self.adapter.submit(request, decision)

    def _client(self):
        client = self.client
        if client is None:
            raise BrokerNotConfigured("testnet broker has no exchange client")
        return client

    def cancel_order(self, order_id: str) -> OrderResult | None:
        return self._client().cancel_order(int(order_id))

    def get_order(self, order_id: str) -> dict | None:
        return self._client().fetch_order(int(order_id))

    def get_positions(self) -> list[dict]:
        return list(self._client().fetch_positions())

    def get_balances(self) -> list[dict]:
        return list(self._client().fetch_balances())


class DeltaLiveBroker:
    """Permanently disabled live broker; constructing it is an error."""

    venue = "delta-live"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise LiveBrokerDisabled("live broker is disabled; live trading is not authorized")

    def place_order(self, request: OrderRequest, decision: RiskDecision) -> OrderResult:
        raise LiveBrokerDisabled("live broker is disabled; live trading is not authorized")


def broker_for_profile(
    profile: Any,
    *,
    paper: Any | None = None,
    settings: Any | None = None,
    adapter: Any | None = None,
) -> Broker:
    """Build the one broker allowed for a profile's execution mode.

    Live is unreachable: the live broker refuses construction before any
    network client can be created.
    """
    from app.execution.modes import normalize_mode

    mode = normalize_mode(getattr(profile, "mode", None))
    if mode == ExecutionMode.PAPER:
        return PaperBroker(profile, paper, adapter=adapter)
    if mode == ExecutionMode.TESTNET:
        return DeltaTestnetBroker(profile, settings, adapter=adapter)
    raise LiveBrokerDisabled("live broker is disabled; live trading is not authorized")


__all__ = [
    "Broker",
    "BrokerCapabilities",
    "BrokerError",
    "BrokerNotConfigured",
    "DeltaLiveBroker",
    "DeltaTestnetBroker",
    "LiveBrokerDisabled",
    "PaperBroker",
    "broker_for_profile",
]
