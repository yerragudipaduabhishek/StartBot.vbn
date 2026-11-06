"""Simulated SL/TP monitor.

Runs the stop/take-profit state machine locally (no exchange dependency) so the
testnet phase can validate exit MECHANICS even when native testnet SL/TP order
behaviour is unsuitable (requirement #19). When a trigger fires, the caller
places a reduce-only market order on the testnet to exit and measures actual SL
vs expected SL.

The monitor follows the backtest worst-case order (stop checked first on a
price tick) so simulated exits match the strategy's assumptions.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class SltpPosition:
    client_order_id: str
    side: str  # long | short
    size: float
    entry_price: float
    stop_price: float
    target_price: float
    latest_mark: float | None = None
    status: str = "open"  # open | stopped | targeted
    stop_fill: float | None = None
    target_fill: float | None = None
    triggered: bool = False
    exit_price: float | None = None
    exit_reason: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SltpPosition":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class SltpMonitor:
    """Local stop/target tracker keyed by client_order_id."""

    def __init__(self, state_path: str = ""):
        self.positions: dict[str, SltpPosition] = {}
        self.state_path = state_path
        self._load()

    # ------------------------------------------------------------- lifecycle

    def arm(
        self,
        client_order_id: str,
        side: str,
        size: float,
        entry_price: float,
        stop_price: float,
        target_price: float,
    ) -> SltpPosition:
        if client_order_id in self.positions:
            logger.info("sltp: %s already tracked (duplicate arm ignored)", client_order_id)
            return self.positions[client_order_id]
        pos = SltpPosition(
            client_order_id=client_order_id,
            side=side,
            size=size,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
        )
        self.positions[client_order_id] = pos
        self._save()
        logger.info("sltp: armed %s %s size=%s entry=%s sl=%s tp=%s", side, client_order_id, size, entry_price, stop_price, target_price)
        return pos

    def disarm(self, client_order_id: str) -> None:
        self.positions.pop(client_order_id, None)
        self._save()

    def track(self, client_order_id: str, mark: float) -> SltpPosition | None:
        """Update the mark price for a tracked position."""
        pos = self.positions.get(client_order_id)
        if pos is None:
            return None
        pos.latest_mark = mark
        self._save()
        return pos

    def open_positions(self) -> list[SltpPosition]:
        return [p for p in self.positions.values() if p.status == "open"]

    def reconcile(self, client_order_id: str) -> SltpPosition | None:
        return self.positions.get(client_order_id)

    # ------------------------------------------------------------ triggering

    def check(self, client_order_id: str, price: float) -> tuple[str, float] | None:
        """Evaluate one price tick against the position.

        Stop is evaluated first (worst case first, matching the backtest), then
        target. Returns (reason, fill_price) when the position flips closed,
        else None. Only an OPEN position can be triggered.
        """
        pos = self.positions.get(client_order_id)
        if pos is None or pos.status != "open":
            return None
        pos.latest_mark = price

        if pos.side == "long":
            if price <= pos.stop_price:
                return self._close(pos, "stop", pos.stop_price)
            if price >= pos.target_price:
                return self._close(pos, "target", pos.target_price)
        else:
            if price >= pos.stop_price:
                return self._close(pos, "stop", pos.stop_price)
            if price <= pos.target_price:
                return self._close(pos, "target", pos.target_price)
        self._save()
        return None

    def check_bar(self, client_order_id: str, high: float, low: float) -> tuple[str, float] | None:
        """Evaluate an OHLC bar (worst case first, same rule as the backtest)."""
        pos = self.positions.get(client_order_id)
        if pos is None or pos.status != "open":
            return None
        pos.latest_mark = low if pos.side == "long" else high

        if pos.side == "long":
            if low <= pos.stop_price:
                return self._close(pos, "stop", pos.stop_price)
            if high >= pos.target_price:
                return self._close(pos, "target", pos.target_price)
        else:
            if high >= pos.stop_price:
                return self._close(pos, "stop", pos.stop_price)
            if low <= pos.target_price:
                return self._close(pos, "target", pos.target_price)
        self._save()
        return None

    def _close(self, pos: SltpPosition, reason: str, fill: float) -> tuple[str, float]:
        pos.status = "stopped" if reason == "stop" else "targeted"
        pos.exit_reason = reason
        pos.exit_price = fill
        if reason == "stop":
            pos.stop_fill = fill
        else:
            pos.target_fill = fill
        pos.triggered = True
        self._save()
        logger.info("sltp: %s TRIGGERED %s fill=%s", pos.side, reason, fill)
        return reason, fill

    # ------------------------------------------------------------- snapshot

    def snapshot(self) -> dict:
        return {cid: pos.to_dict() for cid, pos in self.positions.items()}

    def restore(self, payload: dict) -> None:
        for cid, d in payload.items():
            try:
                self.positions[cid] = SltpPosition.from_dict(d)
            except Exception:
                logger.exception("sltp: dropping unparsable state %s", cid)
        self._save()

    def save(self, path: str = "") -> None:
        path = path or self.state_path
        if not path:
            return
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(self.snapshot(), fh, default=str)
        except OSError:
            logger.exception("failed to save sltp state")

    def _load(self) -> None:
        if not self.state_path or not Path(self.state_path).exists():
            return
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                payload = json.load(fh)
            self.restore(payload)
        except (OSError, ValueError):
            logger.exception("failed to load sltp state from %s", self.state_path)

    def _save(self) -> None:
        self.save()