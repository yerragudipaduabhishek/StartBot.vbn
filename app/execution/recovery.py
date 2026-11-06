"""Restart/recovery handling for the testnet execution layer.

Two problems this solves (requirements #14 and #15):

* Duplicate-order protection: every order carries a unique client_order_id that
  is persisted (file + DB) before the request is sent. A restarted process can
  never re-place the same signal because the id is already recorded.
* Crash recovery: on start we reconcile open orders/positions from the
  exchange against local state, cancel orphaned orders that belong to us but
  are no longer tracked, and re-arm the SL/TP monitor from its JSON snapshot.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_VERSION = 1


class UsedOrderIdStore:
    """Persistent log of client_order_ids already placed.

    Hydrated from the DB (TestnetExecution rows) and/or a state file so the
    de-dupe set survives restarts even if the local file is lost.
    """

    def __init__(self, path: str = "", session_factory=None, prefix: str = ""):
        self.path = path
        self.session_factory = session_factory
        self.prefix = prefix
        self._ids: set[str] = set()
        self._load_file()
        self._load_db()

    # ------------------------------------------------------------------- io

    def _load_file(self) -> None:
        if not self.path or not Path(self.path).exists():
            return
        try:
            with open(self.path, encoding="utf-8") as fh:
                payload = json.load(fh)
            self._ids.update(payload.get("ids", []))
        except (OSError, ValueError):
            logger.exception("failed to load used order ids from %s", self.path)

    def _load_db(self) -> None:
        try:
            if self.session_factory is not None:
                from app.database.models import TestnetExecution

                with self.session_factory() as db:
                    rows = db.query(TestnetExecution.client_order_id).all()
                self._ids.update(r[0] for r in rows)
            else:
                from app.database.database import SessionLocal
                from app.database.models import TestnetExecution

                with SessionLocal() as db:
                    rows = db.query(TestnetExecution.client_order_id).all()
                self._ids.update(r[0] for r in rows)
        except Exception:
            logger.exception("failed to hydrate used order ids from DB")

    def save(self) -> None:
        if not self.path:
            return
        try:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump({"version": STATE_VERSION, "ids": sorted(self._ids)}, fh)
        except OSError:
            logger.exception("failed to persist used order ids")

    # ------------------------------------------------------------------ api

    def contains(self, client_order_id: str) -> bool:
        return client_order_id in self._ids

    def add(self, client_order_id: str) -> None:
        self._ids.add(client_order_id)
        self.save()

    def reserve(self, client_order_id: str) -> bool:
        """Mark an id used BEFORE the request leaves. True if freshly reserved."""
        if client_order_id in self._ids:
            return False
        self._ids.add(client_order_id)
        self.save()
        return True

    def __len__(self) -> int:
        return len(self._ids)


def build_client_order_id(prefix: str, product_id: int, signal_time: int) -> str:
    """Deterministic id per signal so dedupe works across restarts."""
    return f"{prefix}-{product_id}-{signal_time}"


def reconcile_exchange_state(
    client,
    product_id: int,
    tracked_ids: set[str],
    native_active_ids: set[str] | None = None,
) -> dict:
    """Fetch open orders + positions for the product and cancel orphaned orders.

    `tracked_ids` = client_order_ids this process believes are live. Any open
    order we previously placed (matches our prefix convention) that is NOT
    tracked is an orphan from a dead process and is cancelled.

    Returns a summary dict suitable for the recovery log/report.
    """
    open_orders = client.fetch_open_orders(product_id)
    our_open = [o for o in open_orders if client.is_own_order(o)]
    tracked = tracked_ids or set()
    native_active = native_active_ids or set()

    orphans = [o for o in our_open if str(o.get("client_order_id")) not in tracked and o.get("id") not in native_active]
    cancelled: list[int] = []
    for o in orphans:
        try:
            client.cancel_order(o["id"])
            cancelled.append(o["id"])
        except Exception as e:
            logger.error("reconcile: failed to cancel orphan order %s: %s", o.get("id"), e)

    positions = client.fetch_positions(product_id)
    open_positions = [p for p in positions if float(p.get("size") or 0) != 0.0]

    summary = {
        "open_orders": len(open_orders),
        "our_open_orders": len(our_open),
        "orphan_orders_found": len(orphans),
        "orphan_orders_cancelled": len(cancelled),
        "open_positions": len(open_positions),
        "positions": positions,
    }
    logger.info("reconcile: %s", summary)
    return summary


def compare_positions(local_positions: list[dict], exchange_positions: list[dict]) -> dict:
    """Compare local paper state with exchange-reported open positions.

    Both inputs are normalised to ``(symbol, side, size)``. Any local position
    that is absent or materially different on the exchange is reported as a
    mismatch; trading callers must halt rather than silently repair it.
    """

    def normalize(rows: list[dict]) -> dict[tuple[str, str], float]:
        out: dict[tuple[str, str], float] = {}
        for row in rows or []:
            symbol = str(row.get("symbol") or "").upper()
            side = str(row.get("side") or "").lower()
            if side in ("buy",):
                side = "long"
            elif side in ("sell",):
                side = "short"
            try:
                size = float(row.get("size") or 0.0)
            except (TypeError, ValueError):
                size = 0.0
            if not symbol or size == 0.0:
                continue
            out[(symbol, side)] = out.get((symbol, side), 0.0) + size
        return out

    local = normalize(local_positions)
    exchange = normalize(exchange_positions)
    keys = sorted(set(local) | set(exchange))
    mismatches: list[str] = []
    for key in keys:
        symbol, side = key
        local_size = local.get(key, 0.0)
        exchange_size = exchange.get(key, 0.0)
        if abs(local_size - exchange_size) > 1e-9:
            mismatches.append(
                f"{symbol} {side}: local={local_size:g} exchange={exchange_size:g}"
            )
    return {
        "ok": not mismatches,
        "mismatches": mismatches,
        "local_positions": len(local),
        "exchange_positions": len(exchange),
        "local": [{"symbol": s, "side": d, "size": q} for (s, d), q in local.items()],
        "exchange": [{"symbol": s, "side": d, "size": q} for (s, d), q in exchange.items()],
    }


reconcile_positions = compare_positions


def recover_monitor(monitor, state_path: str) -> int:
    """Re-arm the SL/TP monitor from its persisted JSON snapshot.

    Returns the number of positions restored open.
    """
    if not state_path or not Path(state_path).exists():
        return 0
    try:
        with open(state_path, encoding="utf-8") as fh:
            payload = json.load(fh)
        monitor.restore(payload)
    except (OSError, ValueError):
        logger.exception("failed to restore monitor from %s", state_path)
        return 0
    return len(monitor.open_positions())


def ensure_state_dirs(paths: list[str]) -> None:
    for p in paths:
        if p:
            parent = os.path.dirname(p)
            if parent:
                os.makedirs(parent, exist_ok=True)