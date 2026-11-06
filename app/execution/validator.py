"""Delta testnet execution validation sweep.

Walks the full checklist (req #7..#13, #17..#20) against a STRICT testnet-only
client and produces one ExecutionReport per placed order:

  mode safety -> auth/signature -> product discovery -> balances/positions ->
  order creation -> order status -> cancellation -> position state after exec
  -> report + journal persist

Order placement is opt-in (``place_orders=True``) and is only possible through
an :class:`ApprovedTestnetOrder`: a validated strategy Signal, an approved
RiskDecision, an OrderRequest derived from that decision, and a TradeGuard
approval. There is no raw/manual order path. ``diagnostic_only=True`` performs
the read-only checks and never submits or cancels an order.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.config.settings import Settings, get_settings
from app.contracts import (
    ExecutionMode,
    OrderRequest,
    OrderSide,
    OrderType,
)
from app.execution.adapters import ExecutionRouter, ExecutionValidationError
from app.execution.client import TestnetExecutionClient
from app.execution.guard import ExecutionGuardError, GuardResult, TradeGuard
from app.execution.order_manager import OrderManager
from app.execution.modes import normalize_mode
from app.execution.report import ExecutionJournal, ExecutionReport, execution_report_from_order
from app.execution.recovery import build_client_order_id
from app.execution.safety import ExecutionSafetyError
from app.risk.decision import RiskDecision
from app.strategy.signal import Signal

logger = logging.getLogger(__name__)


@dataclass
class ApprovedTestnetOrder:
    """The complete, auditable authorisation chain for one testnet order."""

    signal: Signal
    decision: RiskDecision
    request: OrderRequest
    guard: TradeGuard
    requested_by: str
    reason: str

    @classmethod
    def from_signal(
        cls,
        signal: Signal | dict,
        guard: TradeGuard,
        *,
        product_id: int,
        requested_by: str,
        reason: str,
        client_order_prefix: str = "btc",
        now: datetime | None = None,
    ) -> "ApprovedTestnetOrder":
        if not str(requested_by or "").strip():
            raise ExecutionSafetyError("testnet order requires an authenticated user identifier")
        if not str(reason or "").strip():
            raise ExecutionSafetyError("testnet order requires a reason")

        raw = signal.model_dump() if isinstance(signal, Signal) else dict(signal)
        requested_size = raw.get("accepted_size")
        try:
            guarded = guard.check(raw, requested_size=requested_size, now=now)
        except ExecutionGuardError as exc:
            raise ExecutionSafetyError(f"testnet order not approved: {exc}") from exc
        cls._require_protection(guarded)
        cls._require_decision(guarded)

        signal_obj = guarded.signal
        side = OrderSide.BUY if signal_obj.direction == "long" else OrderSide.SELL
        client_order_id = build_client_order_id(
            str(client_order_prefix or "btc"),
            int(product_id),
            int(signal_obj.candle_time),
        )
        request_data: dict[str, Any] = {
            "client_order_id": client_order_id,
            "symbol": signal_obj.symbol,
            "timeframe": signal_obj.timeframe,
            "strategy_id": signal_obj.strategy_id,
            "strategy_version": signal_obj.strategy_version,
            "side": side,
            "size": guarded.size,
            "expected_price": signal_obj.entry,
            "order_type": OrderType.MARKET,
            "stop_loss": signal_obj.stop_loss,
            "take_profit": signal_obj.target,
            "product_id": int(product_id),
            "execution_mode": ExecutionMode.TESTNET,
            "risk_decision_key": guarded.decision.signal_key,
            "risk_approved": True,
            "confidence": signal_obj.confidence,
            "data_source": signal_obj.data_source,
            "candle_time": signal_obj.candle_time,
            "requested_by": requested_by,
            "reason": reason,
        }
        order_type = str(raw.get("order_type") or "market").strip().lower()
        if order_type in ("limit", "limit_order"):
            limit_price = raw.get("limit_price")
            if limit_price is None:
                raise ExecutionSafetyError("approved limit order requires a limit_price")
            request_data["order_type"] = "limit"
            request_data["limit_price"] = limit_price
        request = OrderRequest(**request_data)
        return cls(
            signal=signal_obj,
            decision=guarded.decision,
            request=request,
            guard=guard,
            requested_by=requested_by,
            reason=reason,
        )

    @staticmethod
    def _require_protection(guarded: GuardResult) -> None:
        if guarded.signal.stop_loss is None:
            raise ExecutionSafetyError("testnet order rejected: stop loss is required")
        if guarded.signal.target is None:
            raise ExecutionSafetyError("testnet order rejected: take profit is required")

    @staticmethod
    def _require_decision(guarded: GuardResult) -> None:
        decision = guarded.decision
        if not decision.approved or decision.size is None:
            raise ExecutionSafetyError(
                f"testnet order rejected: risk decision is not approved ({decision.reason})"
            )
        if normalize_mode(decision.execution_mode) != ExecutionMode.TESTNET:
            raise ExecutionSafetyError("testnet order rejected: risk decision is not testnet")
        if not math.isclose(decision.size.quantities, guarded.size, rel_tol=1e-9, abs_tol=1e-12):
            raise ExecutionSafetyError("testnet order rejected: decision size mismatch")
        if decision.signal_key != guarded.signal.key:
            raise ExecutionSafetyError("testnet order rejected: decision signal mismatch")


class TestnetValidator:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: TestnetExecutionClient | None = None,
        journal: ExecutionJournal | None = None,
        symbol: str | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.client = client or TestnetExecutionClient(settings=self.settings)
        self.journal = journal or self.client.journal
        self.symbol = symbol or self.settings.trading_symbol

    # ------------------------------------------------------------- mode gate

    def check_mode(self) -> dict:
        problems: list[str] = []
        if self.settings.live_mode:
            problems.append("LIVE_MODE is on (must stay off)")
        if not self.settings.testnet_mode:
            problems.append("TESTNET_MODE is off (must stay on)")
        if self.settings.real_order_confirmation:
            problems.append("REAL_ORDER_CONFIRMATION is true (must stay false)")
        if not self.settings.delta_api_key or not self.settings.delta_api_secret:
            problems.append("Delta API key/secret not configured")
        return {
            "ok": not problems,
            "problems": problems,
            "live_mode": self.settings.live_mode,
            "testnet_mode": self.settings.testnet_mode,
            "real_order_confirmation": self.settings.real_order_confirmation,
        }

    # ----------------------------------------------------------------- flow

    def validate(
        self,
        *,
        approved_order: ApprovedTestnetOrder | None = None,
        place_orders: bool = False,
        cancel_test: bool = False,
        diagnostic_only: bool = False,
    ) -> dict:
        """Run the testnet validation sweep. Returns step-by-step results.

        ``place_orders`` requires an approved strategy order. ``diagnostic_only``
        cannot be combined with order placement and never touches order APIs.
        """
        if place_orders and diagnostic_only:
            raise ExecutionSafetyError("diagnostic-only mode cannot place orders")
        if place_orders and approved_order is None:
            raise ExecutionSafetyError(
                "place_orders requires an approved Signal + RiskDecision + OrderRequest + TradeGuard"
            )
        mode = self.check_mode()
        steps: dict[str, Any] = {"mode": mode}
        if not mode["ok"]:
            raise ExecutionSafetyError("validation aborted: " + "; ".join(mode["problems"]))

        self.journal.event(
            kind="validation_started",
            mode=mode,
            place_orders=place_orders,
            diagnostic_only=diagnostic_only,
        )

        steps["auth"] = self._validate_auth()

        product = self.client.discover_product(self.symbol)
        steps["product"] = {
            "symbol": product.get("symbol"),
            "product_id": product.get("id"),
            "contract_type": product.get("contract_type"),
            "contract_unit_size": product.get("contract_unit_size"),
            "is_perpetual": product.get("contract_type") == "perpetual_futures",
        }

        steps["positions_before"] = self._state_summary()

        steps["order"] = None
        steps["cancel_test"] = None
        if place_orders and approved_order is not None:
            lifecycle = self._order_lifecycle(product, approved_order, cancel_test=cancel_test)
            steps.update(lifecycle)
            steps["positions_after"] = self._state_summary()
        elif diagnostic_only:
            steps["diagnostic_only"] = {
                "ok": True,
                "order_placed": False,
                "cancel_attempted": False,
            }

        steps["mode"]["ok"] = True
        self.journal.event(
            kind="validation_finished",
            ok=True,
            order_placed=bool(steps.get("order")),
            diagnostic_only=diagnostic_only,
        )
        return steps

    # -------------------------------------------------------------- helpers

    def _validate_auth(self) -> dict:
        started = time.perf_counter()
        balances = self.client.fetch_balances()
        latency_ms = (time.perf_counter() - started) * 1000.0
        ok = isinstance(balances, list)
        self.journal.event(kind="auth_validated", ok=ok, latency_ms=round(latency_ms, 3))
        return {
            "ok": ok,
            "latency_ms": round(latency_ms, 3),
            "currency_count": len(balances) if ok else 0,
            "sample_balances": balances[:5],
        }

    def _state_summary(self) -> dict:
        positions = self.client.fetch_positions()
        open_pos = [p for p in positions if float(p.get("size") or 0) != 0.0]
        return {"positions_found": len(positions), "open_positions": open_pos}

    def _authorize(
        self,
        approved: ApprovedTestnetOrder,
        product: dict,
        *,
        now: datetime | None = None,
    ) -> GuardResult:
        """Re-run every gate immediately before submission."""
        if not isinstance(approved, ApprovedTestnetOrder):
            raise ExecutionSafetyError("testnet orders require an ApprovedTestnetOrder")
        if not str(approved.requested_by or "").strip():
            raise ExecutionSafetyError("testnet order rejected: missing user identity")
        if not str(approved.reason or "").strip():
            raise ExecutionSafetyError("testnet order rejected: missing reason")
        if not isinstance(approved.guard, TradeGuard):
            raise ExecutionSafetyError("testnet order rejected: missing TradeGuard")
        profile = getattr(approved.guard, "profile", None)
        if profile is None or normalize_mode(profile.mode) != ExecutionMode.TESTNET:
            raise ExecutionSafetyError("testnet order rejected: guard is not a testnet guard")
        if not self.settings.testnet_mode:
            raise ExecutionSafetyError("testnet order rejected: validator is not configured for testnet")

        try:
            guarded = approved.guard.check(
                approved.signal,
                requested_size=approved.request.size,
                now=now,
            )
        except ExecutionGuardError as exc:
            raise ExecutionSafetyError(f"testnet order rejected: {exc}") from exc
        ApprovedTestnetOrder._require_protection(guarded)
        ApprovedTestnetOrder._require_decision(guarded)

        request = approved.request
        decision = guarded.decision
        stored = approved.decision
        if normalize_mode(request.execution_mode) != ExecutionMode.TESTNET:
            raise ExecutionSafetyError("testnet order rejected: request is not testnet")
        if normalize_mode(stored.execution_mode) != ExecutionMode.TESTNET:
            raise ExecutionSafetyError("testnet order rejected: stored decision is not testnet")
        if not request.risk_approved:
            raise ExecutionSafetyError("testnet order rejected: request is not risk approved")
        if request.risk_decision_key != decision.signal_key or stored.signal_key != decision.signal_key:
            raise ExecutionSafetyError("testnet order rejected: risk decision key mismatch")
        if request.client_order_id != build_client_order_id(
            str(self.settings.delta_client_id_prefix or "btc"),
            int(request.product_id),
            int(request.candle_time),
        ):
            raise ExecutionSafetyError("testnet order rejected: non-deterministic client_order_id")
        if int(product.get("id") or 0) != int(request.product_id):
            raise ExecutionSafetyError("testnet order rejected: product mismatch")
        if request.symbol != profile.symbol or request.timeframe != profile.timeframe:
            raise ExecutionSafetyError("testnet order rejected: symbol/timeframe mismatch")
        if request.strategy_id != profile.strategy_id or request.strategy_version != profile.strategy_version:
            raise ExecutionSafetyError("testnet order rejected: strategy mismatch")
        if request.side != (OrderSide.BUY if approved.signal.direction == "long" else OrderSide.SELL):
            raise ExecutionSafetyError("testnet order rejected: side does not match signal")
        if not math.isclose(request.size, decision.size.quantities, rel_tol=1e-9, abs_tol=1e-12):
            raise ExecutionSafetyError("testnet order rejected: size does not match risk decision")
        if not math.isclose(stored.size.quantities, decision.size.quantities, rel_tol=1e-9, abs_tol=1e-12):
            raise ExecutionSafetyError("testnet order rejected: stored decision size mismatch")
        if not _same(approved.signal.stop_loss, request.stop_loss) or not _same(
            approved.signal.target, request.take_profit
        ):
            raise ExecutionSafetyError("testnet order rejected: protective prices do not match signal")
        if not _same(decision.stop_loss, request.stop_loss) or not _same(
            decision.take_profit, request.take_profit
        ):
            raise ExecutionSafetyError("testnet order rejected: protective prices do not match risk decision")
        if not _same(approved.signal.entry, request.expected_price):
            raise ExecutionSafetyError("testnet order rejected: entry price does not match signal")
        if stored.execution_mode is not None and normalize_mode(stored.execution_mode) != normalize_mode(
            decision.execution_mode
        ):
            raise ExecutionSafetyError("testnet order rejected: stored decision mode mismatch")
        return guarded

    def _order_lifecycle(
        self,
        product: dict,
        approved: ApprovedTestnetOrder,
        *,
        cancel_test: bool,
    ) -> dict:
        product_id = int(product["id"])
        guarded = self._authorize(approved, product)
        request = approved.request
        if int(request.product_id) != product_id:
            raise ExecutionSafetyError("testnet order rejected: product id does not match discovery")

        try:
            approved.guard.reserve(guarded.signal, "delta-testnet")
        except ExecutionGuardError as exc:
            self.journal.event(
                kind="testnet_order_rejected",
                user=approved.requested_by,
                reason=f"reservation failed: {exc}",
                signal_id=guarded.signal.key,
                risk_decision=guarded.decision.model_dump(),
                order_id=request.client_order_id,
            )
            raise ExecutionSafetyError(f"testnet order rejected: {exc}") from exc

        self.journal.event(
            kind="testnet_order_requested",
            user=approved.requested_by,
            reason=approved.reason,
            signal_id=guarded.signal.key,
            risk_decision=guarded.decision.model_dump(),
            order_id=request.client_order_id,
            order=request.model_dump(),
        )

        manager = OrderManager(
            ExecutionRouter(
                approved.guard.profile,
                settings=self.settings,
                execution_client=self.client,
            ),
            session_factory=None,
        )
        try:
            result = manager.place(request, guarded.decision)
        except (ExecutionValidationError, Exception) as exc:
            self.journal.event(
                kind="testnet_order_rejected",
                user=approved.requested_by,
                reason=str(exc),
                signal_id=guarded.signal.key,
                risk_decision=guarded.decision.model_dump(),
                order_id=request.client_order_id,
            )
            if isinstance(exc, ExecutionSafetyError):
                raise
            raise ExecutionSafetyError(f"testnet order rejected: {exc}") from exc

        self.journal.event(
            kind="testnet_order_placed",
            user=approved.requested_by,
            reason=approved.reason,
            signal_id=guarded.signal.key,
            risk_decision=guarded.decision.model_dump(),
            order_id=result.order_id,
            client_order_id=result.client_order_id,
        )

        snapshot: dict[str, Any] = {}
        if result.order_id is not None:
            if str(request.order_type) == "limit":
                snapshot = self.client.fetch_order(result.order_id)
            else:
                snapshot = self.client.watch_order(result.order_id)
        result.order_state = snapshot.get("state") or result.order_state
        raw_fill = snapshot.get("average_filled_price") or snapshot.get("filled_price")
        if raw_fill not in (None, ""):
            result.fill_price = float(raw_fill)
        result.fees = float(snapshot.get("quote_fees") or 0.0) + float(
            snapshot.get("contract_fees") or 0.0
        )

        report = execution_report_from_order(
            signal_time=int(guarded.signal.candle_time),
            side=guarded.signal.direction,
            size=float(request.size),
            expected_entry=float(request.expected_price),
            result=result,
            expected_sl=request.stop_loss,
            expected_tp=request.take_profit,
        )
        report.position_status = self._position_status(product_id)

        cancel_result: dict | None = None
        if cancel_test:
            cancel_result = self._cancel_approved_order(result.order_id, result.order_state, guarded.signal.key)

        self.journal.write_report(report)
        logger.info("validation %s", report.summary())
        self._audit_report(report, approved, guarded, result, cancel_result)
        return {
            "order": {"result": result.model_dump(), "snapshot": snapshot, "authorized": True},
            "report": {**report.to_dict(), "summary": report.summary()},
            "cancel_test": cancel_result,
        }

    def _cancel_approved_order(self, order_id: Any, state: str, signal_id: str) -> dict:
        if order_id is None:
            return {"skipped": True, "cancel_ok": False, "reason": "order id unavailable"}
        if state != "open":
            return {"skipped": True, "cancel_ok": False, "reason": f"order is {state}, not open"}
        try:
            created = self.client.fetch_order(order_id)
            cancelled = self.client.cancel_order(order_id)
            final = self.client.fetch_order(order_id)
            return {
                "order_id": order_id,
                "signal_id": signal_id,
                "created_state": created.get("state"),
                "cancelled_state": cancelled.order_state,
                "final_state": final.get("state"),
                "cancel_ok": cancelled.order_state == "cancelled",
            }
        except Exception as exc:  # pragma: no cover - defensive client failure
            logger.exception("approved-order cancellation failed")
            return {"order_id": order_id, "signal_id": signal_id, "error": str(exc), "cancel_ok": False}

    def _audit_report(
        self,
        report: ExecutionReport,
        approved: ApprovedTestnetOrder,
        guarded: GuardResult,
        result: Any,
        cancel_result: dict | None,
    ) -> None:
        self.journal.event(
            kind="testnet_order_audit",
            user=approved.requested_by,
            reason=approved.reason,
            signal_id=guarded.signal.key,
            risk_decision=guarded.decision.model_dump(),
            order_id=result.order_id,
            client_order_id=result.client_order_id,
            order_state=result.order_state,
            cancel=cancel_result,
            report=report.to_dict(),
        )

    def _position_status(self, product_id: int) -> str:
        positions = self.client.fetch_positions(product_id)
        open_pos = [p for p in positions if float(p.get("size") or 0) != 0.0]
        return "open" if open_pos else "flat"


def _same(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return left is None and right is None
    return math.isclose(float(left), float(right), rel_tol=1e-9, abs_tol=1e-9)
