"""Execution adapters and the single order-submission router.

Paper is a local simulation adapter. Testnet is the strict contained-exchange
adapter. Live is present only as a permanently rejecting adapter. Research and
backtest modes have no adapter at all and therefore cannot submit orders.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Protocol

from app.config.settings import Settings, get_settings
from app.config.trading import TradingProfile
from app.contracts import (
    ExecutionMode,
    OrderRequest,
    OrderResult,
    RiskDecision,
    Signal,
    SignalDirection,
)
from app.execution.modes import (
    ExecutionModeError,
    LiveTradingDisabled,
    ResearchBoundaryError,
    assert_mode_can_submit,
    assert_testnet_ready,
    mode_allows_orders,
    normalize_mode,
    venue_for_mode,
)
from app.trading.orders import OrderSpec
from app.trading.paper_trader import PaperTrader

logger = logging.getLogger(__name__)


class ExecutionError(RuntimeError):
    """Base error for an execution boundary rejection."""


class ExecutionRejected(ExecutionError):
    """A valid request was rejected by the adapter or venue."""


class ExecutionValidationError(ExecutionError):
    """The request or its risk decision failed boundary validation."""


ExecutionAdapterError = ExecutionError


class ExecutionAdapter(Protocol):
    mode: ExecutionMode
    venue: str

    def submit(self, request: OrderRequest, decision: RiskDecision) -> OrderResult: ...


def _same(left: float | None, right: float | None, *, tolerance: float = 1e-9) -> bool:
    if left is None or right is None:
        return left is None and right is None
    return math.isclose(float(left), float(right), rel_tol=1e-6, abs_tol=tolerance)


def _decision_approved(decision: RiskDecision) -> bool:
    return bool(decision.allowed and str(decision.approval_status) == "approved")


class _ValidatedAdapter:
    mode: ExecutionMode
    venue: str

    def _validate(
        self,
        request: OrderRequest,
        decision: RiskDecision,
        *,
        profile: TradingProfile | None = None,
    ) -> None:
        if not isinstance(request, OrderRequest):
            request = OrderRequest.model_validate(request)
        if not isinstance(decision, RiskDecision):
            decision = RiskDecision.model_validate(decision)
        assert_mode_can_submit(self.mode)
        if normalize_mode(request.execution_mode) != self.mode:
            raise ExecutionValidationError(
                f"request mode {request.execution_mode} does not match adapter {self.mode.value}"
            )
        if not _decision_approved(decision):
            raise ExecutionValidationError("order rejected: risk decision is not approved")
        if decision.size is None:
            raise ExecutionValidationError("order rejected: risk decision has no size")
        if not _same(request.size, decision.size.quantities, tolerance=1e-9):
            raise ExecutionValidationError("order rejected: size does not match the risk decision")
        if request.risk_decision_key != decision.signal_key:
            raise ExecutionValidationError("order rejected: risk decision key mismatch")
        if not request.risk_approved:
            raise ExecutionValidationError("order rejected: request is not risk approved")
        if str(request.order_type) == "limit" and getattr(request, "limit_price", None) is None:
            raise ExecutionValidationError("limit order requires a limit price")
        if decision.entry is not None and not _same(request.expected_price, decision.entry, tolerance=1e-6):
            raise ExecutionValidationError("order rejected: entry price does not match the risk decision")
        if decision.stop_loss is not None and not _same(request.stop_loss, decision.stop_loss, tolerance=1e-6):
            raise ExecutionValidationError("order rejected: stop loss does not match the risk decision")
        if decision.take_profit is not None and not _same(request.take_profit, decision.take_profit, tolerance=1e-6):
            raise ExecutionValidationError("order rejected: take profit does not match the risk decision")
        if decision.symbol and decision.symbol != request.symbol:
            raise ExecutionValidationError("order rejected: symbol does not match the risk decision")
        if decision.timeframe and decision.timeframe != request.timeframe:
            raise ExecutionValidationError("order rejected: timeframe does not match the risk decision")
        if decision.strategy_id and decision.strategy_id != request.strategy_id:
            raise ExecutionValidationError("order rejected: strategy does not match the risk decision")
        if decision.strategy_version and decision.strategy_version != request.strategy_version:
            raise ExecutionValidationError("order rejected: strategy version does not match the risk decision")
        if profile is not None:
            if request.symbol != profile.symbol:
                raise ExecutionValidationError(f"order symbol {request.symbol} does not match profile {profile.symbol}")
            if request.timeframe != profile.timeframe:
                raise ExecutionValidationError(
                    f"order timeframe {request.timeframe} does not match profile {profile.timeframe}"
                )
            if request.strategy_id != profile.strategy_id:
                raise ExecutionValidationError("order strategy does not match the active profile")
            if request.strategy_version != profile.strategy_version:
                raise ExecutionValidationError("order strategy version does not match the active profile")
            if decision.execution_mode is not None and normalize_mode(decision.execution_mode) != normalize_mode(profile.mode):
                raise ExecutionValidationError("order risk decision belongs to a different execution mode")

    @staticmethod
    def _require_protection(request: OrderRequest) -> None:
        if request.stop_loss is None:
            raise ExecutionValidationError("entry order requires a stop loss")
        if request.take_profit is None:
            raise ExecutionValidationError("entry order requires a take profit")


class PaperExecutionAdapter(_ValidatedAdapter):
    """Local simulated execution. It has no exchange client and no private key."""

    mode = ExecutionMode.PAPER
    venue = "paper"

    def __init__(
        self,
        paper: PaperTrader,
        profile: TradingProfile,
        *,
        risk_manager: Any | None = None,
    ) -> None:
        if normalize_mode(profile.mode) != ExecutionMode.PAPER:
            raise ExecutionModeError("PaperExecutionAdapter requires a paper profile")
        self.paper = paper
        self.profile = profile
        if risk_manager is not None:
            self.paper.risk_manager = risk_manager
        if self.paper.profile is None:
            self.paper.profile = profile

    def submit(self, request: OrderRequest, decision: RiskDecision) -> OrderResult:
        self._validate(request, decision, profile=self.profile)
        if request.stop_loss is None or request.take_profit is None:
            raise ExecutionValidationError("paper entry requires stop loss and take profit")
        if self.paper.position is not None:
            raise ExecutionRejected("paper position is already open")
        side = SignalDirection.LONG.value if request.side == "buy" else SignalDirection.SHORT.value
        opened = self.paper.open_position(
            side,
            request.size,
            request.expected_price,
            request.stop_loss,
            request.take_profit,
            reason="signal",
            symbol=request.symbol,
            bar_index=request.candle_time or None,
        )
        if not opened:
            raise ExecutionRejected("paper engine rejected the entry")
        fill_price = self.paper.position["entry_price"] if self.paper.position else request.expected_price
        return OrderResult(
            client_order_id=request.client_order_id,
            product_id=request.product_id,
            side=request.side,
            size=request.size,
            price=request.expected_price,
            fill_price=fill_price,
            expected_price=request.expected_price,
            venue=self.venue,
            status="accepted",
            order_state="filled",
            symbol=request.symbol,
            timeframe=request.timeframe,
            risk_decision_key=request.risk_decision_key,
            risk_approved=True,
            execution_mode=self.mode,
            message="paper simulation",
        )


class TestnetExecutionAdapter(_ValidatedAdapter):
    """Strict testnet adapter; the only adapter with an exchange order client."""

    mode = ExecutionMode.TESTNET
    venue = "delta-testnet"

    def __init__(
        self,
        profile: TradingProfile,
        settings: Settings | None = None,
        *,
        client: Any | None = None,
    ) -> None:
        if normalize_mode(profile.mode) != ExecutionMode.TESTNET:
            raise ExecutionModeError("TestnetExecutionAdapter requires a testnet profile")
        self.profile = profile
        self.settings = settings or get_settings()
        has_credentials = bool(self.settings.delta_api_key and self.settings.delta_api_secret)
        assert_testnet_ready(self.settings, has_credentials=has_credentials if client is None else None)
        self.client = client

    def _client(self):
        if self.client is None:
            from app.execution.client import TestnetExecutionClient

            self.client = TestnetExecutionClient(settings=self.settings)
        return self.client

    def submit(self, request: OrderRequest, decision: RiskDecision) -> OrderResult:
        self._validate(request, decision, profile=self.profile)
        self._require_protection(request)
        if request.product_id <= 0:
            raise ExecutionValidationError("testnet order requires a valid product_id")
        order = OrderSpec(
            product_id=request.product_id,
            side=request.side,
            size=request.size,
            order_type="market" if request.order_type == "market" else "limit_order",
            limit_price=getattr(request, "limit_price", None),
            stop_loss_price=request.stop_loss,
            client_order_id=request.client_order_id,
            expected_price=request.expected_price,
            reduce_only=request.reduce_only,
        )
        result = self._client().place_order(order)
        if isinstance(result, dict):
            result = OrderResult.model_validate(result)
        if not isinstance(result, OrderResult):
            raise ExecutionRejected("testnet client returned an invalid order result")
        result.venue = self.venue
        result.execution_mode = self.mode
        result.client_order_id = result.client_order_id or request.client_order_id
        result.expected_price = result.expected_price or request.expected_price
        result.symbol = result.symbol or request.symbol
        result.timeframe = result.timeframe or request.timeframe
        result.risk_decision_key = request.risk_decision_key
        result.risk_approved = True
        if result.status in ("rejected", "error") or result.order_state == "rejected":
            raise ExecutionRejected(result.message or "testnet order rejected")
        return result


class LiveExecutionAdapter:
    """A permanently disabled adapter retained for an explicit fail-closed test."""

    mode = ExecutionMode.LIVE
    venue = "delta-live"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.profile = kwargs.get("profile")
        self.client = None

    def submit(self, request: OrderRequest | None = None, decision: RiskDecision | None = None) -> OrderResult:
        raise LiveTradingDisabled("live order submission is disabled")


class ExecutionRouter:
    """The only component runners and the executor may use for order placement."""

    def __init__(
        self,
        profile: TradingProfile,
        *,
        adapters: dict[ExecutionMode | str, ExecutionAdapter] | None = None,
        paper: PaperTrader | None = None,
        settings: Settings | None = None,
        execution_client: Any | None = None,
        testnet_client: Any | None = None,
        risk_manager: Any | None = None,
    ) -> None:
        self.profile = profile
        self.mode = normalize_mode(profile.mode)
        self.settings = settings
        self.adapters: dict[str, ExecutionAdapter] = {}
        if adapters is not None:
            for key, adapter in adapters.items():
                normalized_key = normalize_mode(key)
                if normalize_mode(adapter.mode) != normalized_key:
                    raise ExecutionModeError(
                        f"adapter mode {adapter.mode} does not match registered mode {normalized_key.value}"
                    )
                self.adapters[normalized_key.value] = adapter
        if self.mode == ExecutionMode.PAPER:
            self.adapters.setdefault(
                self.mode.value,
                PaperExecutionAdapter(paper or PaperTrader(profile=profile), profile, risk_manager=risk_manager),
            )
        elif self.mode == ExecutionMode.TESTNET:
            self.adapters.setdefault(
                self.mode.value,
                TestnetExecutionAdapter(
                    profile,
                    settings,
                    client=execution_client or testnet_client,
                ),
            )
        elif self.mode == ExecutionMode.LIVE:
            self.adapters.setdefault(self.mode.value, LiveExecutionAdapter(profile=profile))
        self.adapter = self.adapters.get(self.mode.value)

    def submit(self, request: OrderRequest, decision: RiskDecision) -> OrderResult:
        if not mode_allows_orders(self.mode):
            raise ExecutionModeError(f"{self.mode.value} mode cannot submit orders")
        assert_mode_can_submit(self.mode)
        adapter = self.adapter
        if adapter is None:
            raise ExecutionModeError(f"no execution adapter is configured for {self.mode.value}")
        return adapter.submit(request, decision)

    def submit_signal(
        self,
        signal: Signal | dict,
        decision: RiskDecision,
        *,
        price: float | None = None,
        product_id: int | None = None,
    ) -> OrderResult:
        if not mode_allows_orders(self.mode):
            raise ExecutionModeError(f"{self.mode.value} mode cannot submit orders")
        if not isinstance(signal, Signal):
            from app.strategy.validation import validate_signal

            signal = validate_signal(
                signal,
                profile=self.profile,
                check_freshness=False,
            )
        if signal.direction not in (SignalDirection.LONG.value, SignalDirection.SHORT.value):
            raise ExecutionValidationError("only entry signals can be submitted")
        if signal.stop_loss is None or signal.target is None:
            raise ExecutionValidationError("entry signal requires stop loss and take profit")
        if not decision.allowed or decision.size is None:
            raise ExecutionValidationError("risk decision is not approved")
        if signal.accepted_size is None or not _same(
            signal.accepted_size, decision.size.quantities, tolerance=1e-9
        ):
            raise ExecutionValidationError("order rejected: size does not match the signal")
        expected_keys = {signal.key}
        if signal.model_extra and signal.model_extra.get("signal_key"):
            expected_keys.add(str(signal.model_extra["signal_key"]))
        if decision.signal_key not in expected_keys:
            raise ExecutionValidationError("order rejected: risk decision key does not match the signal")
        expected_price = float(price if price is not None else signal.entry)
        extra = signal.model_extra or {}
        resolved_product_id = int(
            product_id if product_id is not None else extra.get("product_id") or 0
        )
        side = "buy" if signal.direction == SignalDirection.LONG.value else "sell"
        request = OrderRequest(
            client_order_id=f"{self.mode.value}:{signal.key}",
            symbol=signal.symbol,
            timeframe=signal.timeframe,
            strategy_id=signal.strategy_id,
            strategy_version=signal.strategy_version,
            side=side,
            size=decision.size.quantities,
            expected_price=expected_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.target,
            product_id=resolved_product_id,
            execution_mode=self.mode,
            risk_decision_key=decision.signal_key or signal.key,
            risk_approved=True,
            confidence=signal.confidence,
            data_source=str(signal.data_source or signal.data_quality.source or "unknown"),
            candle_time=signal.candle_time,
        )
        return self.submit(request, decision)

    execute = submit_signal


def build_execution_router(
    profile: TradingProfile,
    *,
    paper: PaperTrader | None = None,
    settings: Settings | None = None,
    execution_client: Any | None = None,
    risk_manager: Any | None = None,
) -> ExecutionRouter:
    return ExecutionRouter(
        profile,
        paper=paper,
        settings=settings,
        execution_client=execution_client,
        risk_manager=risk_manager,
    )


__all__ = [
    "ExecutionAdapter",
    "ExecutionAdapterError",
    "ExecutionError",
    "ExecutionRejected",
    "ExecutionRouter",
    "ExecutionValidationError",
    "LiveExecutionAdapter",
    "PaperExecutionAdapter",
    "ResearchBoundaryError",
    "TestnetExecutionAdapter",
    "build_execution_router",
    "venue_for_mode",
]
