"""Persistent, idempotent order state machine.

Every order transition and fill is append-only. The database is authoritative:
a trade can be reconstructed from orders, order_events and fills without
consulting process memory. Invalid transitions and duplicate idempotency keys
fail closed.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.database.database import SessionLocal
from app.database.models import Fill, Order, OrderEvent


class OrderStateError(RuntimeError):
    """An order transition is invalid or conflicts with its idempotency key."""


ORDER_STATES = {
    "created",
    "accepted",
    "partially_filled",
    "filled",
    "rejected",
    "cancelled",
    "expired",
}

TERMINAL_STATES = {"filled", "rejected", "cancelled", "expired"}

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "created": {"accepted", "rejected", "cancelled"},
    "accepted": {"partially_filled", "filled", "rejected", "cancelled", "expired"},
    "partially_filled": {"partially_filled", "filled", "cancelled", "expired"},
    "filled": set(),
    "rejected": set(),
    "cancelled": set(),
    "expired": set(),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json(payload: Any) -> str:
    return json.dumps(payload or {}, sort_keys=True, default=str)


class OrderRepository:
    def __init__(self, session_factory=SessionLocal) -> None:
        self.session_factory = session_factory

    def get(self, client_order_id: str) -> Order | None:
        with self.session_factory() as db:
            return db.execute(
                select(Order).where(Order.client_order_id == str(client_order_id))
            ).scalar_one_or_none()

    def events(self, client_order_id: str) -> list[OrderEvent]:
        with self.session_factory() as db:
            order = db.execute(
                select(Order).where(Order.client_order_id == str(client_order_id))
            ).scalar_one_or_none()
            if order is None:
                return []
            return list(
                db.execute(
                    select(OrderEvent)
                    .where(OrderEvent.order_id == order.id)
                    .order_by(OrderEvent.id)
                ).scalars()
            )

    def fills(self, client_order_id: str) -> list[Fill]:
        with self.session_factory() as db:
            return list(
                db.execute(
                    select(Fill)
                    .where(Fill.client_order_id == str(client_order_id))
                    .order_by(Fill.id)
                ).scalars()
            )

    def create_intent(
        self,
        *,
        client_order_id: str,
        venue: str,
        execution_mode: str,
        symbol: str,
        side: str,
        size: float,
        order_type: str = "market",
        expected_price: float | None = None,
        signal_key: str = "",
        strategy_id: str = "",
        strategy_version: str = "",
        risk_decision_key: str = "",
        risk_decision_id: int | None = None,
        idempotency_key: str | None = None,
        payload: dict | None = None,
    ) -> tuple[Order, bool]:
        key = str(client_order_id or "").strip()
        if not key:
            raise OrderStateError("client_order_id is required")
        if not math.isfinite(float(size)) or float(size) <= 0:
            raise OrderStateError("size must be finite and positive")
        with self.session_factory() as db:
            existing = db.execute(select(Order).where(Order.client_order_id == key)).scalar_one_or_none()
            if existing is not None:
                return existing, False
            if idempotency_key:
                duplicate = db.execute(
                    select(Order).where(Order.idempotency_key == str(idempotency_key))
                ).scalar_one_or_none()
                if duplicate is not None:
                    return duplicate, False
            order = Order(
                created_at=_now(),
                updated_at=_now(),
                client_order_id=key,
                idempotency_key=str(idempotency_key) if idempotency_key else None,
                signal_key=str(signal_key or ""),
                venue=str(venue or ""),
                execution_mode=str(execution_mode or "paper"),
                symbol=str(symbol or ""),
                side=str(side or ""),
                order_type=str(order_type or "market"),
                size=float(size),
                expected_price=None if expected_price is None else float(expected_price),
                status="created",
                strategy_id=str(strategy_id or ""),
                strategy_version=str(strategy_version or ""),
                risk_decision_key=str(risk_decision_key or ""),
                risk_decision_id=risk_decision_id,
                raw=_json(payload),
            )
            db.add(order)
            db.flush()
            db.add(
                OrderEvent(
                    created_at=_now(),
                    order_id=order.id,
                    client_order_id=key,
                    from_state="",
                    to_state="created",
                    event_type="created",
                    idempotency_key=f"{key}:created",
                    payload=_json(payload),
                )
            )
            db.commit()
            db.refresh(order)
            return order, True

    def transition(
        self,
        client_order_id: str,
        to_state: str,
        *,
        idempotency_key: str | None = None,
        reason: str = "",
        filled_size: float | None = None,
        avg_fill_price: float | None = None,
        fees_delta: float = 0.0,
        slippage_delta: float = 0.0,
        payload: dict | None = None,
    ) -> Order:
        target = str(to_state or "").strip().lower()
        if target not in ORDER_STATES:
            raise OrderStateError(f"unknown order state: {to_state!r}")
        key = str(client_order_id or "").strip()
        with self.session_factory() as db:
            order = db.execute(select(Order).where(Order.client_order_id == key)).scalar_one_or_none()
            if order is None:
                raise OrderStateError(f"order not found: {key}")
            if idempotency_key:
                seen = db.execute(
                    select(OrderEvent).where(OrderEvent.idempotency_key == str(idempotency_key))
                ).scalar_one_or_none()
                if seen is not None:
                    return order
            current = str(order.status)
            if current == target:
                return order
            if target not in ALLOWED_TRANSITIONS.get(current, set()):
                raise OrderStateError(f"invalid order transition {current} -> {target}")
            order.status = target
            order.updated_at = _now()
            if reason:
                order.reason = str(reason)
            if filled_size is not None:
                order.filled_size = max(0.0, float(filled_size))
            if avg_fill_price is not None:
                order.avg_fill_price = float(avg_fill_price)
            if fees_delta:
                order.fees = float(order.fees or 0.0) + float(fees_delta)
            if slippage_delta:
                order.slippage = float(order.slippage or 0.0) + float(slippage_delta)
            db.add(
                OrderEvent(
                    created_at=_now(),
                    order_id=order.id,
                    client_order_id=key,
                    from_state=current,
                    to_state=target,
                    event_type="transition",
                    idempotency_key=str(idempotency_key) if idempotency_key else None,
                    payload=_json(payload or {"reason": reason}),
                )
            )
            db.commit()
            db.refresh(order)
            return order

    def record_fill(
        self,
        client_order_id: str,
        *,
        fill_key: str,
        price: float,
        size: float,
        fee: float = 0.0,
        slippage: float = 0.0,
        venue: str = "",
        payload: dict | None = None,
    ) -> Order:
        key = str(client_order_id or "").strip()
        fill_key = str(fill_key or "").strip()
        if not fill_key:
            raise OrderStateError("fill_key is required")
        if not math.isfinite(float(price)) or float(price) <= 0:
            raise OrderStateError("fill price must be finite and positive")
        if not math.isfinite(float(size)) or float(size) <= 0:
            raise OrderStateError("fill size must be finite and positive")
        with self.session_factory() as db:
            order = db.execute(select(Order).where(Order.client_order_id == key)).scalar_one_or_none()
            if order is None:
                raise OrderStateError(f"order not found: {key}")
            existing = db.execute(select(Fill).where(Fill.fill_key == fill_key)).scalar_one_or_none()
            if existing is not None:
                return order
            if order.status not in {"accepted", "partially_filled"}:
                raise OrderStateError(f"cannot fill order in state {order.status}")
            old_size = float(order.filled_size or 0.0)
            old_avg = float(order.avg_fill_price or 0.0)
            new_size = old_size + float(size)
            if new_size > float(order.size) + 1e-9:
                raise OrderStateError("fill size exceeds order size")
            new_avg = (old_avg * old_size + float(price) * float(size)) / new_size
            order.filled_size = new_size
            order.avg_fill_price = new_avg
            order.fees = float(order.fees or 0.0) + float(fee)
            order.slippage = float(order.slippage or 0.0) + float(slippage)
            order.updated_at = _now()
            if new_size >= float(order.size) - 1e-9:
                target = "filled"
            else:
                target = "partially_filled"
            previous = order.status
            order.status = target
            db.add(
                Fill(
                    fill_key=fill_key,
                    order_id=order.id,
                    client_order_id=key,
                    venue=str(venue or order.venue or ""),
                    price=float(price),
                    size=float(size),
                    fee=float(fee),
                    slippage=float(slippage),
                    raw=_json(payload),
                )
            )
            db.add(
                OrderEvent(
                    created_at=_now(),
                    order_id=order.id,
                    client_order_id=key,
                    from_state=previous,
                    to_state=target,
                    event_type="fill",
                    idempotency_key=f"{key}:fill:{fill_key}",
                    payload=_json(payload or {"price": price, "size": size}),
                )
            )
            db.commit()
            db.refresh(order)
            return order


__all__ = [
    "ALLOWED_TRANSITIONS",
    "ORDER_STATES",
    "OrderRepository",
    "OrderStateError",
    "TERMINAL_STATES",
]
