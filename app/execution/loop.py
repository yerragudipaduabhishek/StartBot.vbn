"""Testnet validation loop: drive the real 1h strategy signals onto Delta
testnet and validate execution mechanics end to end.

Safe by default: LIVE_MODE must be off, TESTNET_MODE on, and every order goes
through the strict testnet client. The loop is driven by injected candles +
signals so it is fully testable offline; the CLI wires it to the live market
feed (see app/execution/__main__.py).

Restart/disconnect semantics (req #14/#15/#16):
* Duplicate signals are ignored via a persisted client_order_id per signal_time.
* SL/TP orphans from a dead process are re-armed from the JSON snapshot.
* Open orders we no longer track are cancelled during recovery.
* In-flight positions are reconciled against the testnet account.
"""

from __future__ import annotations

import asyncio
import logging

from app.config.settings import Settings, get_settings
from app.config.trading import TradingProfile
from app.data.delta_client import ORDER_TYPE_MARKET
from app.execution.adapters import ExecutionRouter
from app.execution.client import TestnetExecutionClient
from app.execution.guard import ExecutionGuardError, TradeGuard
from app.execution.order_manager import OrderManager
from app.execution.recovery import build_client_order_id, reconcile_exchange_state, recover_monitor
from app.execution.report import ExecutionReport
from app.execution.safety import ExecutionSafetyError
from app.execution.sltp import SltpMonitor
from app.execution.validator import ApprovedTestnetOrder
from app.risk.risk_manager import RiskManager
from app.trading.orders import OrderSpec

logger = logging.getLogger(__name__)

CONTRACT_VALUE = 0.001


class TestnetValidationLoop:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: TestnetExecutionClient | None = None,
        monitor: SltpMonitor | None = None,
        contract_value: float = CONTRACT_VALUE,
        profile: TradingProfile | None = None,
        risk_manager: RiskManager | None = None,
        guard: TradeGuard | None = None,
        requested_by: str = "testnet-loop",
        reason: str = "approved testnet validation signal",
    ) -> None:
        self.settings = settings or get_settings()
        self.client = client or TestnetExecutionClient(settings=self.settings)
        self.monitor = monitor or SltpMonitor(state_path=self.settings.delta_execution_state_file)
        self.profile = profile or (guard.profile if guard is not None else None)
        self.contract_value = self.profile.contract_value if self.profile else contract_value
        self.symbol = self.profile.symbol if self.profile else self.settings.trading_symbol
        self.risk_manager = risk_manager
        self.guard = guard
        self.requested_by = requested_by
        self.reason = reason
        self._position_open_for: dict[str, str] = {}  # client_order_id -> side of live entry
        self.last_report: ExecutionReport | None = None
        self.order_manager = None
        if self.profile is not None:
            self.order_manager = OrderManager(
                ExecutionRouter(self.profile, settings=self.settings, execution_client=self.client),
            )

    # ------------------------------------------------------------- recovery

    def recover(self) -> dict:
        """Reconcile on start: cancel orphaned orders, map tracked positions,
        restore the SL/TP monitor, and report what changed."""
        if not self.settings.testnet_mode or self.settings.live_mode:
            raise ExecutionSafetyError("loop refuses to run in an unsafe mode configuration")
        product = self.client.discover_product(self.symbol)
        product_id = int(product["id"])

        restored = recover_monitor(self.monitor, self.settings.delta_execution_state_file)
        for pos in self.monitor.open_positions():
            self._position_open_for[pos.client_order_id] = pos.side

        reconciled = reconcile_exchange_state(
            self.client,
            product_id,
            tracked_ids=set(self._position_open_for),
        )
        result = {
            "product_id": product_id,
            "monitor_positions_restored": restored,
            "tracked_entries": len(self._position_open_for),
            **reconciled,
        }
        self.client.journal.event(kind="recovery_done", **result)
        return result

    # --------------------------------------------------------------- signals

    def on_signal(self, sig: dict, signal_time: int, price: float | None = None) -> dict:
        """Process one strategy signal. Returns an action dict.

        * no trade                          -> {"action": "noop"}
        * position already open             -> {"action": "position_open"}
        * duplicate signal already executed -> {"action": "duplicate"}
        * otherwise                         -> testnet entry + armed monitor
        """
        direction = sig.get("direction")
        if direction not in ("long", "short") or not sig.get("risk_allowed"):
            return {"action": "noop"}

        if self.guard is None or self.profile is None:
            reason = "TradeGuard and testnet profile are required for testnet entries"
            self.client.journal.event(kind="testnet_entry_rejected", reason=reason, signal=sig)
            return {"action": "rejected", "reason": reason}

        product = self.client.discover_product(self.symbol)
        product_id = int(product["id"])

        # These are safe, state-only short circuits.  They must happen before
        # the risk evaluation so a duplicate or an already-open position is
        # reported accurately even when the risk cooldown would reject the
        # new signal.  No order or reservation is created on either branch.
        candidate_time = int(sig.get("candle_time") or signal_time)
        candidate_cid = build_client_order_id(
            str(self.settings.delta_client_id_prefix or "btc"), product_id, candidate_time
        )
        if self.client.store.contains(candidate_cid):
            logger.info("loop: duplicate signal %s already executed", candidate_time)
            return {"action": "duplicate"}

        if self._position_open_for:
            logger.info("loop: ignoring signal, position already open (%s)", direction)
            return {"action": "position_open"}

        try:
            approved = ApprovedTestnetOrder.from_signal(
                sig,
                self.guard,
                product_id=product_id,
                requested_by=self.requested_by,
                reason=self.reason,
                client_order_prefix=self.settings.delta_client_id_prefix,
            )
        except (ExecutionSafetyError, ExecutionGuardError) as exc:
            logger.warning("loop: guarded signal rejected: %s", exc)
            return {"action": "noop", "reason": str(exc)}

        cid = approved.request.client_order_id
        signal_time = int(approved.signal.candle_time)
        if self.client.store.contains(cid):
            logger.info("loop: duplicate signal %s already executed", signal_time)
            return {"action": "duplicate"}

        try:
            self.guard.reserve(approved.signal, "delta-testnet")
        except ExecutionGuardError as exc:
            logger.warning("loop: execution intent rejected: %s", exc)
            return {"action": "duplicate", "reason": str(exc)}

        side = approved.signal.direction
        size = approved.request.size
        expected = approved.request.expected_price
        entry_sl = approved.request.stop_loss
        entry_tp = approved.request.take_profit
        report = ExecutionReport(
            signal_time=signal_time,
            side=side,
            size=size,
            expected_entry=expected,
            expected_sl=entry_sl,
            expected_tp=entry_tp,
            client_order_id=cid,
            venue="delta-testnet",
            order_status="open",
        )
        self.client.journal.event(
            kind="testnet_entry_requested",
            user=self.requested_by,
            reason=self.reason,
            signal_id=approved.signal.key,
            risk_decision=approved.decision.model_dump(),
            order_id=cid,
        )
        try:
            if self.order_manager is None:
                raise ExecutionSafetyError("testnet order manager is not initialized")
            result = self.order_manager.place(approved.request, approved.decision)
        except Exception as exc:
            logger.exception("loop: approved testnet entry failed")
            self.client.journal.event(
                kind="testnet_entry_rejected",
                user=self.requested_by,
                reason=str(exc),
                signal_id=approved.signal.key,
                risk_decision=approved.decision.model_dump(),
                order_id=cid,
            )
            return {"action": "rejected", "reason": str(exc)}

        report.order_status = result.order_state
        report.actual_entry = result.fill_price or result.price
        report.fees += result.fees
        report.latency_ms += result.latency_ms
        report.finish_fill()

        entry_price = report.actual_entry or expected
        self._position_open_for[cid] = side
        self.monitor.arm(cid, side, size, entry_price, entry_sl, entry_tp)
        if self.risk_manager is not None:
            self.risk_manager.on_position_opened(bar_index=signal_time)
        self.client.journal.event(
            kind="signal_executed", signal_time=signal_time, direction=side, size=size,
            expected=expected, client_order_id=cid, order_state=result.order_state,
            user=self.requested_by, reason=self.reason, signal_id=approved.signal.key,
            risk_decision=approved.decision.model_dump(), order_id=result.order_id,
        )
        self.client.journal.write_report(report)
        self.last_report = report
        return {"action": "executed", "report": report.to_dict()}

    # ---------------------------------------------------------- SL/TP drives

    def on_candle(self, candle: dict) -> list[dict]:
        """Evaluate open monitor positions against a closed candle (worst case
        first). Each trigger is closed with a reduce-only testnet market order,
        actual SL/TP fill recorded, P&L computed, and the report finalised."""
        events: list[dict] = []
        high, low = float(candle["high"]), float(candle["low"])
        for cid in list(self.monitor.positions.keys()):
            triggered = self.monitor.check_bar(cid, high, low)
            if triggered is None:
                continue
            reason, trigger_price = triggered
            events.append(self._close_and_report(cid, reason, trigger_price))
        return events

    def on_tick(self, price: float) -> list[dict]:
        """Evaluate open monitor positions against a streamed mark price."""
        events: list[dict] = []
        for cid in list(self.monitor.positions.keys()):
            triggered = self.monitor.check(cid, price)
            if triggered is None:
                continue
            reason, trigger_price = triggered
            events.append(self._close_and_report(cid, reason, trigger_price))
        return events

    def _close_and_report(self, cid: str, reason: str, trigger_price: float) -> dict:
        pos = self.monitor.reconcile(cid)
        side = pos.side if pos else "long"
        size = pos.size if pos else 0.0
        entry = pos.entry_price if pos else trigger_price

        mark = self.client.mark_price(self.symbol) or trigger_price
        close_spec = OrderSpec(
            product_id=int(self.client.discover_product(self.symbol)["id"]),
            side="sell" if side == "long" else "buy",
            size=size,
            order_type=ORDER_TYPE_MARKET,
            reduce_only=True,
            expected_price=mark,
        )
        exit_result = None
        try:
            exit_result = self.client.reduce_position(close_spec)
            exit_fill = exit_result.fill_price or exit_result.price or mark
        except Exception as e:
            logger.exception("loop: exit order failed for %s", cid)
            self.client.journal.event(kind="exit_error", client_order_id=cid, reason=str(e))
            exit_fill = trigger_price  # record what local simulation assumed

        report = self._finalize_report(cid, side, size, entry, reason, trigger_price, exit_fill, exit_result)
        self.monitor.disarm(cid)
        self._position_open_for.pop(cid, None)
        if self.risk_manager is not None:
            self.risk_manager.on_trade_closed(report.final_pnl, event_id=f"testnet:{cid}")
        self.client.journal.write_report(report)
        self.last_report = report
        logger.info("loop: closed %s (%s) fill=%s pnl=%+.4f", cid, reason, exit_fill, report.final_pnl)
        return {"client_order_id": cid, "reason": reason, "report": report.to_dict()}

    def _finalize_report(self, cid, side, size, entry, reason, trigger_price, exit_fill, exit_result) -> ExecutionReport:
        base = self._entry_report(cid)
        report = base or ExecutionReport(
            signal_time=0, side=side, size=size, expected_entry=entry,
            client_order_id=cid, venue="delta-testnet",
        )
        # Expected levels came from the original signal; re-assign from the trigger.
        if reason == "stop":
            report.expected_sl = trigger_price
            report.actual_sl = exit_fill
        else:
            report.expected_tp = trigger_price
            report.actual_tp = exit_fill

        mult = 1.0 if side == "long" else -1.0
        gross = size * (exit_fill - entry) * mult * self.contract_value
        exit_fees = float(exit_result.fees or 0.0) if exit_result is not None else 0.0
        report.fees = float(report.fees or 0.0) + exit_fees
        report.final_pnl = round(gross - report.fees, 6)
        report.order_status = getattr(exit_result, "order_state", "simulated") if exit_result is not None else "simulated"
        report.position_status = "closed"
        report.exit_reason = reason
        return report

    def _entry_report(self, cid: str) -> ExecutionReport | None:
        if self.last_report and self.last_report.client_order_id == cid:
            return self.last_report
        return None


class TestnetValidationRunner:
    """Async composition: reconcile on start, then poll a price source and run
    the tick-based SL/TP loop. The price source is injectable so tests never
    need a socket and a live run can pull from the WS/MarketData feed."""

    def __init__(self, loop: TestnetValidationLoop, price_source=None):
        self.loop = loop
        self.price_source = price_source  # () -> float | None
        self._running = False

    async def run(self, poll_secs: float = 2.0) -> None:
        self._running = True
        recovery = self.loop.recover()
        logger.info("runner started, recovery=%s", recovery)
        while self._running:
            price = self.price_source() if self.price_source else None
            if price:
                for ev in self.loop.on_tick(price):
                    logger.info("tick close: %s", ev["reason"])
            await asyncio.sleep(max(0.01, poll_secs))

    def stop(self) -> None:
        self._running = False
