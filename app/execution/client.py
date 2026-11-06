"""Testnet-only execution client.

Strict testnet safety guard, duplicate-order protection, latency + fee capture
and full request/response/fill/rejection/error logging. Wraps the generic
DeltaClient but refuses to run against anything but approved testnet hosts
while TESTNET_MODE is on, and refuses to initialise at all if the mode flags
are unsafe (requirement #5, #6).
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import Any

from app.config.settings import Settings, get_settings
from app.data.delta_client import (
    ORDER_CANCELLED,
    ORDER_FILLED,
    ORDER_FINAL_STATES,
    ORDER_OPEN,
    ORDER_REJECTED,
    ORDER_TYPE_MARKET,
    DeltaClient,
)
from app.execution.recovery import UsedOrderIdStore
from app.execution.report import ExecutionJournal, slippage_between
from app.execution.safety import DuplicateOrderError, ExecutionSafetyError, host_of, validate_execution_host
from app.trading.orders import OrderResult, OrderSpec

logger = logging.getLogger(__name__)


class TestnetConnectivityError(RuntimeError):
    """Raised when an expected exchange interaction cannot be completed."""


def _num(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def normalize_order_state(state: str | None) -> str:
    state = (state or "").lower()
    if state in ("filled", "closed"):
        return ORDER_FILLED
    if state in ("cancelled", "canceled"):
        return ORDER_CANCELLED
    if state == "rejected":
        return ORDER_REJECTED
    if state == "open":
        return ORDER_OPEN
    return state or ORDER_OPEN


class TestnetExecutionClient:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        rest: DeltaClient | None = None,
        store: UsedOrderIdStore | None = None,
        journal: ExecutionJournal | None = None,
        extra_allowed_hosts: set[str] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        if self.settings.live_mode:
            raise ExecutionSafetyError("LIVE_MODE is on: execution client refuses to initialise")
        if not self.settings.testnet_mode:
            raise ExecutionSafetyError("TESTNET_MODE is off: execution client refuses to initialise")

        self.base_url = self.settings.delta_base_url
        validate_execution_host(host_of(self.base_url), True, extra_allowed_hosts)

        self.rest = rest or DeltaClient(
            self.base_url,
            api_key=self.settings.delta_api_key,
            api_secret=self.settings.delta_api_secret,
            settings=self.settings,
        )
        validate_execution_host(host_of(self.rest.base_url), True, extra_allowed_hosts)

        self.store = store if store is not None else UsedOrderIdStore(
            path=self.settings.delta_execution_state_file,
            prefix=self.settings.delta_client_id_prefix,
        )
        self.journal = journal or ExecutionJournal(
            journal_path=self.settings.delta_execution_journal,
            state_path=self.settings.delta_execution_state_file,
        )
        self.product_id_prefix = self.settings.delta_client_id_prefix
        self._product: dict | None = None

    # ----------------------------------------------------------------- guard

    def assert_order_safe(self) -> None:
        if not self.settings.execution_allowed:
            raise ExecutionSafetyError(
                "order path refuses to run: live_mode=%s testnet_mode=%s "
                "(orders require testnet_mode on and live_mode off)",
                self.settings.live_mode,
                self.settings.testnet_mode,
            )
        validate_execution_host(host_of(self.base_url), True)

    # ------------------------------------------------------------ data reads

    def discover_product(self, symbol: str | None = None, contract_type: str = "perpetual_futures") -> dict:
        if self._product is None:
            self._product = self.rest.find_product(symbol or self.settings.trading_symbol, contract_type=contract_type)
            self.journal.event(kind="product_discovered", result=self._product)
        return self._product

    def fetch_balances(self) -> list[dict]:
        return self._call("GET", "/v2/wallet/balances", self.rest.get_balances)[0]

    def fetch_positions(self, product_id: int | None = None) -> list[dict]:
        return self._call("GET", "/v2/positions", self.rest.get_positions, product_id)[0]

    def fetch_open_orders(self, product_id: int) -> list[dict]:
        return self._call("GET", "/v2/orders", self.rest.get_orders, product_id, "open")[0]

    def fetch_order(self, order_id: int) -> dict:
        return self._call("GET", f"/v2/orders/{order_id}", self.rest.get_order, order_id)[0]

    def fetch_order_trades(self, order_id: int) -> list[dict]:
        return self._call("GET", f"/v2/orders/{order_id}/trades", self.rest.get_order_trades, order_id)[0]

    def mark_price(self, symbol: str | None = None) -> float | None:
        try:
            ticker = self.rest.get_ticker(symbol or self.settings.trading_symbol)
        except Exception:
            logger.exception("mark_price: ticker fetch failed")
            return None
        for key in ("mark_price", "close", "last"):
            if ticker.get(key) is not None:
                return _num(ticker[key])
        return None

    def is_own_order(self, order: dict) -> bool:
        cid = str(order.get("client_order_id") or "")
        return cid.startswith(self.product_id_prefix)

    # -------------------------------------------------------------- ordering

    def place_order(self, spec: OrderSpec) -> OrderResult:
        """Place an order behind the safety guard. Never hits production."""
        self.assert_order_safe()

        client_order_id = spec.client_order_id or self._fresh_id(spec.product_id)
        if self.store.contains(client_order_id):
            raise DuplicateOrderError(client_order_id)
        self.store.reserve(client_order_id)
        spec = spec.model_copy(update={"client_order_id": client_order_id})

        resp, latency_ms = self._call(
            "POST",
            "/v2/orders",
            self.rest.place_order,
            product_id=spec.product_id,
            size=spec.size,
            side=spec.side,
            order_type=spec.order_type,
            price=spec.limit_price,
            stop_price=spec.stop_price,
            reduce_only=spec.reduce_only,
            close_on_trigger=spec.close_on_trigger,
            client_order_id=client_order_id,
            time_in_force=spec.time_in_force,
        )
        result = resp.get("result") if isinstance(resp, dict) else resp
        if isinstance(resp, dict) and resp.get("success") is False:
            self.journal.event(kind="rejection", client_order_id=client_order_id, response=resp)
            raise TestnetConnectivityError(f"order rejected by testnet: {resp.get('error', resp)}")

        order = result if isinstance(result, dict) else {}
        fill = _num(order.get("average_filled_price") or order.get("filled_price"))
        expected = spec.expected_price
        slip, slip_bps = slippage_between(expected, fill or None, "long" if spec.side == "buy" else "short")
        fees = _num(order.get("quote_fees")) + _num(order.get("contract_fees"))

        out = OrderResult(
            order_id=order.get("id"),
            client_order_id=order.get("client_order_id") or client_order_id,
            product_id=spec.product_id,
            side=spec.side,
            size=spec.size,
            price=fill or None,
            fill_price=fill or None,
            fees=fees,
            latency_ms=round(latency_ms, 3),
            expected_price=expected,
            slippage=round(slip, 6),
            slippage_bps=round(slip_bps, 3),
            order_state=normalize_order_state(order.get("state")),
            venue="delta-testnet",
            status="accepted",
            message="placed on delta testnet",
        )
        self.journal.event(kind="order_placed", order_id=out.order_id, client_order_id=client_order_id,
                           order_state=out.order_state, fill_price=fill, fees=fees, latency_ms=out.latency_ms)
        return out

    def cancel_order(self, order_id: int) -> OrderResult:
        self.assert_order_safe()
        resp, latency_ms = self._call("POST", "/v2/orders/cancel", self.rest.cancel_order, order_id)
        result = resp.get("result") if isinstance(resp, dict) else resp
        order = result if isinstance(result, dict) else {}
        return OrderResult(
            order_id=order.get("id") or order_id,
            product_id=0,
            side=str(order.get("side") or ""),
            size=_num(order.get("size")),
            order_state=normalize_order_state(order.get("state")),
            latency_ms=round(latency_ms, 3),
            venue="delta-testnet",
            status="cancelled",
            message="cancelled on delta testnet",
        )

    def watch_order(self, order_id: int, max_polls: int | None = None, poll_secs: float | None = None) -> dict:
        """Poll until the order reaches a final state (filled/cancelled/rejected),
        journaling every snapshot. Raises on repeated rejection/timeout."""
        max_polls = max_polls or self.settings.delta_execution_max_polls
        poll_secs = poll_secs if poll_secs is not None else self.settings.delta_execution_poll_secs
        last: dict = {}
        for _ in range(max_polls):
            order = self.fetch_order(order_id)
            if not isinstance(order, dict):
                raise TestnetConnectivityError(f"unexpected order payload for {order_id}")
            state = normalize_order_state(order.get("state"))
            self.journal.event(kind="order_snapshot", order_id=order_id, state=state, result=order)
            if state in ORDER_FINAL_STATES:
                return order
            last = order
            time.sleep(max(0.0, poll_secs))
        raise TestnetConnectivityError(f"order {order_id} did not reach a final state in {max_polls} polls (last={last})")

    def reduce_position(self, spec: OrderSpec) -> OrderResult:
        """Place a reduce-only market order to flatten a position (SL/TP exit)."""
        return self.place_order(spec.model_copy(update={"reduce_only": True, "order_type": ORDER_TYPE_MARKET}))

    # --------------------------------------------------------------- helpers

    def _fresh_id(self, product_id: int) -> str:
        return f"{self.product_id_prefix}-{product_id}-{int(time.time() * 1000)}-{secrets.token_hex(3)}"

    def _call(self, method: str, path: str, fn, *args, **kwargs) -> tuple[Any, float]:
        """Execute one interaction with request/response/error journaling + latency."""
        payload = _jsonable((args, kwargs))
        self.journal.event(kind="request", method=method, path=path, payload=payload)
        started = time.perf_counter()
        try:
            result = fn(*args, **kwargs)
        except Exception as e:
            self.journal.event(kind="error", method=method, path=path, error=str(e))
            raise
        latency_ms = (time.perf_counter() - started) * 1000.0
        self.journal.event(kind="response", method=method, path=path, latency_ms=round(latency_ms, 3), result=_jsonable(result))
        return result, latency_ms


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    try:
        return repr(value)
    except Exception:
        return str(value)
