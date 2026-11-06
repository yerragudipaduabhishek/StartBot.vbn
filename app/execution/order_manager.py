"""Single idempotent order entry point for paper and testnet execution."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from app.contracts import OrderRequest, OrderResult, RiskDecision
from app.database.models import AuditEvent
from app.execution.adapters import ExecutionRouter
from app.execution.broker import BrokerError
from app.execution.order_store import OrderRepository
from app.observability import record_risk_decision

logger = logging.getLogger(__name__)


class OrderManager:
    """Enforces one order lifecycle and client-order idempotency.

    Live mode is rejected by the router before this manager can submit. The
    manager is deliberately usable with an injected router for deterministic
    tests and paper execution.
    """

    def __init__(self, router: ExecutionRouter, *, session_factory=None) -> None:
        self.router = router
        self.session_factory = session_factory
        self.repository = OrderRepository(session_factory) if session_factory is not None else None
        self._states: dict[str, str] = {}

    def state(self, client_order_id: str) -> str | None:
        if self.repository is not None:
            order = self.repository.get(client_order_id)
            if order is not None:
                return str(order.status)
        return self._states.get(client_order_id)

    def place(self, request: OrderRequest, decision: RiskDecision) -> OrderResult:
        key = request.client_order_id
        prior = self.state(key)
        if prior in {"accepted", "partially_filled", "filled"}:
            raise BrokerError(f"duplicate order intent: {key}")
        if prior in {"rejected", "cancelled", "expired"}:
            raise BrokerError(f"order intent is terminal: {key}")
        self._states[key] = "created"
        self._persist_risk_decision(request, decision)
        self._audit("order_created", request, {"decision": decision.model_dump()})
        try:
            self._create_intent(request, decision)
            result = self.router.submit(request, decision)
            self._apply_result(request, result)
            state = result.order_state or result.status or "accepted"
            self._states[key] = state
            self._audit("order_filled" if state == "filled" else "order_accepted", request, result.model_dump())
            return result
        except Exception as exc:
            self._states[key] = "rejected"
            if self.repository is not None:
                try:
                    current = self.repository.get(key)
                    if current is not None and current.status not in {"filled", "rejected", "cancelled", "expired"}:
                        self.repository.transition(
                            key,
                            "rejected",
                            idempotency_key=f"{key}:rejected",
                            reason=str(exc),
                        )
                except Exception:
                    logger.exception("order manager: failed to persist rejection")
            self._audit("order_rejected", request, {"error": str(exc)})
            raise

    def _persist_risk_decision(self, request: OrderRequest, decision: RiskDecision) -> None:
        if self.session_factory is None:
            return
        try:
            with self.session_factory() as db:
                record_risk_decision(
                    db,
                    decision,
                    execution_mode=str(getattr(request.execution_mode, "value", request.execution_mode)),
                    signal_key=request.risk_decision_key,
                    payload={"client_order_id": request.client_order_id},
                )
        except Exception as exc:
            logger.exception("order manager: risk decision persistence failed")
            raise BrokerError(f"risk decision could not be persisted: {exc}") from exc

    def _create_intent(self, request: OrderRequest, decision: RiskDecision) -> None:
        if self.repository is None:
            return
        size = decision.size.quantities if decision.size is not None else request.size
        router_mode = getattr(self.router, "mode", "paper")
        self.repository.create_intent(
            client_order_id=request.client_order_id,
            venue=str(getattr(router_mode, "value", router_mode)),
            execution_mode=str(getattr(request.execution_mode, "value", request.execution_mode)),
            symbol=request.symbol,
            side=str(getattr(request.side, "value", request.side)),
            size=float(size),
            order_type=str(getattr(request.order_type, "value", request.order_type)),
            expected_price=request.expected_price,
            signal_key=request.risk_decision_key,
            strategy_id=request.strategy_id,
            strategy_version=request.strategy_version,
            risk_decision_key=request.risk_decision_key,
            idempotency_key=f"{request.client_order_id}:intent",
            payload=request.model_dump(mode="json"),
        )

    def _apply_result(self, request: OrderRequest, result: OrderResult) -> None:
        if self.repository is None:
            return
        key = request.client_order_id
        order = self.repository.get(key)
        if order is None:
            return
        if order.status == "created":
            self.repository.transition(
                key,
                "accepted",
                idempotency_key=f"{key}:accepted",
                payload=result.model_dump(mode="json"),
            )
        state = str(result.order_state or result.status or "accepted")
        fill_size = float(result.size or 0.0)
        if state in {"filled", "partially_filled"} and fill_size > 0 and result.fill_price:
            self.repository.record_fill(
                key,
                fill_key=f"{key}:fill:1",
                price=float(result.fill_price),
                size=fill_size,
                fee=float(result.fees or 0.0),
                slippage=float(result.slippage or 0.0),
                venue=str(result.venue or ""),
                payload=result.model_dump(mode="json"),
            )
        elif state in {"rejected", "cancelled", "expired"}:
            self.repository.transition(
                key,
                state,
                idempotency_key=f"{key}:{state}",
                reason=str(result.message or ""),
                payload=result.model_dump(mode="json"),
            )

    def place_signal(self, signal, decision: RiskDecision, *, price: float | None = None,
                     product_id: int | None = None) -> OrderResult:
        """Managed compatibility path for validated strategy signals."""
        key = str(getattr(signal, "key", ""))
        if not key:
            raise BrokerError("signal is missing an idempotency key")
        prior = self._states.get(key)
        if prior in {"accepted", "filled", "open"}:
            raise BrokerError(f"duplicate order intent: {key}")
        self._states[key] = "created"
        try:
            result = self.router.submit_signal(signal, decision, price=price, product_id=product_id)
            self._states[key] = result.order_state or result.status or "accepted"
            return result
        except Exception:
            self._states[key] = "rejected"
            raise

    def _audit(self, event_type: str, request: OrderRequest, payload: dict) -> None:
        if self.session_factory is None:
            return
        try:
            with self.session_factory() as db:
                db.add(AuditEvent(
                    created_at=datetime.now(timezone.utc),
                    event_type=event_type,
                    actor="order_manager",
                    correlation_id=request.client_order_id,
                    severity="warning" if event_type == "order_rejected" else "info",
                    payload=json.dumps(payload, default=str, sort_keys=True),
                ))
                db.commit()
        except Exception:
            logger.exception("order manager: audit persistence failed")
