"""Executor: the compatibility entry point for signal execution.

The executor can route only to paper or strict testnet. Live is never routed:
even if every legacy live flag is set, execution falls back to paper. Actual
order placement is delegated to ``ExecutionRouter`` and its adapters, behind the
mandatory ``TradeGuard`` risk boundary.
"""

import logging

from app.config.settings import get_settings
from app.config.trading import TradingProfile, profile_from_settings
from app.contracts import ExecutionMode
from app.data.delta_client import DeltaClient
from app.execution.adapters import ExecutionError, ExecutionRouter
from app.execution.guard import ExecutionGuardError, TradeGuard
from app.execution.modes import normalize_mode
from app.execution.order_manager import OrderManager
from app.risk.risk_manager import RiskManager
from app.strategy.strategy_a import STRATEGY_A_CONTRACT_VALUE
from app.trading.orders import OrderResult, OrderSpec, build_market_order
from app.trading.paper_trader import PaperTrader

logger = logging.getLogger(__name__)


class Executor:
    def __init__(
        self,
        settings=None,
        paper: PaperTrader | None = None,
        order_client: DeltaClient | None = None,
        execution_client=None,
        contract_value: float = STRATEGY_A_CONTRACT_VALUE,
        *,
        profile: TradingProfile | None = None,
        risk_manager: RiskManager | None = None,
        kill_switch=None,
        session_factory=None,
    ) -> None:
        self.settings = settings or get_settings()
        requested_mode = normalize_mode(profile.mode) if profile is not None else None
        credentials_ready = bool(self.settings.delta_api_key and self.settings.delta_api_secret)
        may_use_testnet = bool(
            requested_mode in (None, ExecutionMode.TESTNET)
            and self.settings.testnet_mode
            and not self.settings.live_mode
            and credentials_ready
        )
        if order_client is None and may_use_testnet:
            order_client = DeltaClient(
                self.settings.delta_base_url,
                api_key=self.settings.delta_api_key,
                api_secret=self.settings.delta_api_secret,
            )
        self.order_client = order_client

        testnet_route = bool(may_use_testnet and self.order_client is not None)
        if requested_mode == ExecutionMode.TESTNET:
            route_mode = ExecutionMode.TESTNET if testnet_route else ExecutionMode.PAPER
        elif requested_mode == ExecutionMode.PAPER:
            route_mode = ExecutionMode.PAPER
        else:
            route_mode = ExecutionMode.TESTNET if testnet_route else ExecutionMode.PAPER
        self.route_mode = route_mode

        if profile is not None and normalize_mode(profile.mode) == route_mode:
            self.profile = profile
        elif profile is not None:
            self.profile = profile.model_copy(
                update={"mode": route_mode, "profile_id": f"strategy_a_{route_mode.value}"}
            )
        else:
            self.profile = profile_from_settings(self.settings, mode=route_mode)

        self.paper = paper or PaperTrader()
        self.paper.profile = self.profile
        self.contract_value = contract_value or self.profile.contract_value
        self.execution_client = execution_client
        if risk_manager is None:
            risk_manager = RiskManager(
                settings=self.profile.risk,
                starting_equity=self.paper.starting_equity,
                kill_switch=kill_switch,
                execution_mode=route_mode,
            )
            risk_manager.sync_from_paper(self.paper)
        self.risk_manager = risk_manager
        self.paper.risk_manager = risk_manager
        self.guard = TradeGuard(
            self.profile,
            risk_manager,
            kill_switch=kill_switch or risk_manager.kill_switch,
            session_factory=session_factory,
        )
        self.last_guard_error = ""
        self.router = ExecutionRouter(
            self.profile,
            paper=self.paper,
            settings=self.settings,
            execution_client=execution_client,
            risk_manager=risk_manager,
        )
        self.order_manager = OrderManager(self.router, session_factory=session_factory)

    @property
    def _has_keys(self) -> bool:
        return bool(self.settings.delta_api_key and self.settings.delta_api_secret and self.order_client is not None)

    @property
    def can_trade_testnet(self) -> bool:
        """Testnet execution requires TESTNET_MODE on, LIVE_MODE off + keys."""
        return bool(self.settings.testnet_mode and not self.settings.live_mode and self._has_keys)

    @property
    def can_trade_live(self) -> bool:
        """Legacy compatibility only; live routing is permanently disabled."""
        return False

    @property
    def can_trade_real(self) -> bool:
        return self.can_trade_live

    def execute(
        self,
        signal: dict,
        size: float | None = None,
        price: float | None = None,
        confirm_real_orders: bool = False,
    ) -> OrderResult | None:
        direction = signal.get("direction") if isinstance(signal, dict) else getattr(signal, "direction", None)
        if direction not in ("long", "short"):
            return None

        try:
            guarded = self.guard.check(signal, requested_size=size)
        except ExecutionGuardError as exc:
            self.last_guard_error = str(exc)
            logger.warning("executor: order rejected by guard: %s", exc)
            return None

        validated = guarded.signal
        side = "buy" if validated.direction == "long" else "sell"
        order = self._build_order(side, guarded.size, validated)
        entry_price = price if price is not None else validated.entry

        venue = "delta-testnet" if self.route_mode == ExecutionMode.TESTNET else "paper"
        try:
            self.guard.reserve(validated, venue)
        except ExecutionGuardError as exc:
            self.last_guard_error = str(exc)
            logger.warning("executor: order rejected by duplicate guard: %s", exc)
            return None

        try:
            result = self.order_manager.place_signal(
                validated,
                guarded.decision,
                price=entry_price,
                product_id=order.product_id,
            )
        except (ExecutionError, ExecutionGuardError) as exc:
            self.last_guard_error = str(exc)
            logger.warning("executor: execution adapter rejected order: %s", exc)
            return None
        result.venue = venue
        return result

    def _build_order(self, side: str, size: float, signal) -> OrderSpec:
        extra = getattr(signal, "model_extra", None) or {}
        product_id = int(extra.get("product_id") or 0)
        return build_market_order(
            product_id=product_id,
            side=side,
            size=size,
            stop_loss_price=signal.stop_loss,
            expected_price=signal.entry,
        )

    def _place_testnet(self, order: OrderSpec) -> OrderResult:
        if self.execution_client is None:
            from app.execution.client import TestnetExecutionClient

            self.execution_client = TestnetExecutionClient(settings=self.settings)
        return self.execution_client.place_order(order)
