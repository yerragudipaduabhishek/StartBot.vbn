"""Paper trader: deterministic LOCAL execution simulation.

Everything here is simulated money. It never calls a Delta order endpoint,
never requires API keys and never submits a testnet/live order. Entry, stop
loss, take profit and close fills are computed locally against real market
candles, with configured fees and adverse slippage applied.

State (account balance, open position, closed trades) is persisted so a
backend/container restart leaves the paper account intact. No exchange
position is ever queried or modified.
"""

import logging
import math
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select

from app.config.trading import TradingProfile
from app.database.models import PaperAccount, PaperPosition, PaperTrade
from app.risk.risk_manager import RiskManager
from app.strategy.signal import Signal
from app.strategy.strategy_a import STRATEGY_A_CONTRACT_VALUE
from app.strategy.validation import SignalValidationError, validate_signal

logger = logging.getLogger(__name__)

CONTRACT_VALUE = STRATEGY_A_CONTRACT_VALUE


class PaperTrader:
    def __init__(
        self,
        starting_equity: float = 5000.0,
        fee_pct: float = 0.05,
        slippage_pct: float = 0.0,
        contract_value: float = CONTRACT_VALUE,
        symbol: str = "BTCUSDT",
        session=None,
        session_factory=None,
        profile: TradingProfile | None = None,
        risk_manager: RiskManager | None = None,
        clock=None,
    ) -> None:
        self.starting_equity = starting_equity
        self.fee_pct = fee_pct
        self.slippage_pct = slippage_pct
        self.contract_value = contract_value
        self.profile = profile
        self.risk_manager = risk_manager
        self.symbol = profile.symbol if profile is not None else symbol
        self.session = session
        self.session_factory = session_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.realization_error = ""
        self.reconciliation_error = ""
        self.realized_pnl = 0.0
        self.position: dict | None = None
        self.trades: list[PaperTrade] = []
        self._restore_state()

    # ------------------------------------------------------------------ state

    @property
    def equity(self) -> float:
        """Simulated cash balance (starting equity + realized P&L)."""
        return self.starting_equity + self.realized_pnl

    @property
    def balance(self) -> float:
        return self.equity

    @property
    def unrealized_pnl(self) -> float:
        return self.position["unrealized_pnl"] if self.position else 0.0

    @property
    def total_equity(self) -> float:
        return self.equity + self.unrealized_pnl

    def _profile_for_validation(self) -> TradingProfile:
        if self.profile is not None:
            return self.profile
        from app.config.settings import get_settings
        from app.config.trading import profile_from_settings

        return profile_from_settings(get_settings())

    @staticmethod
    def _expose(pos: dict | None) -> dict | None:
        """Public view of a position: strip internal underscore-only keys."""
        if pos is None:
            return None
        return {k: v for k, v in pos.items() if not k.startswith("_")}

    def snapshot(self) -> dict:
        return {"equity": self.equity, "realized_pnl": self.realized_pnl, "open_position": self._expose(self.position)}

    def extended_snapshot(self) -> dict:
        return {
            "balance": self.balance,
            "starting_equity": self.starting_equity,
            "realized_pnl": self.realized_pnl,
            "unrealized_pnl": self.unrealized_pnl,
            "total_equity": self.total_equity,
            "currency": "INR",
            "fee_pct": self.fee_pct,
            "slippage_pct": self.slippage_pct,
            "fund_separate": True,
            "open_position": self._expose(self.position),
            "position_count": 1 if self.position else 0,
            "trades_count": len(self.trades),
        }

    # ------------------------------------------------------------ persistence

    def _restore_state(self) -> None:
        """Pick up the persisted paper account + open position after a restart.

        On startup nothing is replayed: the restored position keeps its last
        known mark price (never treated as fresh), and no order is generated.
        """
        if self.session_factory is None and self.session is None:
            return
        try:
            def work(db):
                row = db.query(PaperAccount).order_by(PaperAccount.id.desc()).first()
                if row is None:
                    row = PaperAccount(
                        starting_equity=self.starting_equity,
                        balance=self.starting_equity,
                        realized_pnl=0.0,
                        fee_pct=self.fee_pct,
                        slippage_pct=self.slippage_pct,
                    )
                    db.add(row)
                    db.commit()
                    db.refresh(row)
                self.starting_equity = row.starting_equity
                self.realized_pnl = row.realized_pnl or 0.0
                self.fee_pct = row.fee_pct
                self.slippage_pct = row.slippage_pct
                pos = db.query(PaperPosition).filter_by(status="open").order_by(PaperPosition.id.desc()).first()
                if pos is not None:
                    self.position = self._position_from_row(pos)
                    logger.info("paper: restored open %s position on %s", pos.side, pos.symbol)

            if self.session_factory is not None:
                with self.session_factory() as db:
                    work(db)
            else:
                work(self.session)
        except Exception:
            logger.exception("paper: failed to restore account state")

    def reconcile(self) -> dict:
        """Compare restored account/position state with persisted history.

        A mismatch is recorded and halts new entries instead of silently
        continuing with an inconsistent paper account.
        """
        if self.session_factory is None and self.session is None:
            return {"ok": True, "mismatches": []}
        try:
            def work(db):
                account = db.query(PaperAccount).order_by(PaperAccount.id.desc()).first()
                closed_sum = float(
                    db.execute(select(func.coalesce(func.sum(PaperTrade.pnl), 0.0)).where(PaperTrade.status == "closed")).scalar_one()
                    or 0.0
                )
                open_rows = int(
                    db.execute(select(func.count()).select_from(PaperPosition).where(PaperPosition.status == "open")).scalar_one()
                    or 0
                )
                mismatches: list[str] = []
                if account is not None:
                    if abs(float(account.realized_pnl or 0.0) - self.realized_pnl) > 0.01:
                        mismatches.append(
                            f"account realized_pnl {account.realized_pnl} != restored {self.realized_pnl}"
                        )
                    if abs(float(account.balance or 0.0) - self.equity) > 0.01:
                        mismatches.append(f"account balance {account.balance} != equity {self.equity}")
                if abs(closed_sum - self.realized_pnl) > 0.01:
                    mismatches.append(f"closed trade P&L {closed_sum} != restored {self.realized_pnl}")
                expected_open = 1 if self.position else 0
                if open_rows != expected_open:
                    mismatches.append(f"open position rows {open_rows} != restored {expected_open}")
                return {"ok": not mismatches, "mismatches": mismatches}

            if self.session_factory is not None:
                with self.session_factory() as db:
                    report = work(db)
            else:
                report = work(self.session)
        except Exception as exc:
            logger.exception("paper: reconciliation failed")
            report = {"ok": False, "mismatches": [f"reconciliation error: {exc}"]}

        if not report["ok"]:
            self.reconciliation_error = "; ".join(report["mismatches"])
            logger.error("paper: reconciliation mismatch: %s", self.reconciliation_error)
            if self.risk_manager is not None:
                self.risk_manager.halt(f"paper reconciliation mismatch: {self.reconciliation_error}")
        return report

    def _upsert_account(self) -> None:
        if self.session_factory is None and self.session is None:
            return
        try:
            def work(db):
                row = db.query(PaperAccount).order_by(PaperAccount.id.desc()).first()
                if row is None:
                    row = PaperAccount(starting_equity=self.starting_equity)
                    db.add(row)
                    db.flush()
                row.starting_equity = self.starting_equity
                row.balance = self.balance
                row.realized_pnl = self.realized_pnl
                row.fee_pct = self.fee_pct
                row.slippage_pct = self.slippage_pct
                row.updated_at = datetime.now(timezone.utc)
                db.commit()

            if self.session_factory is not None:
                with self.session_factory() as db:
                    work(db)
            else:
                work(self.session)
        except Exception:
            logger.exception("paper: failed to persist account")

    def _record_equity_snapshot(self) -> None:
        """Record dashboard equity without blocking paper execution."""
        if self.session_factory is None:
            return
        try:
            from app.observability import snapshot_equity
            with self.session_factory() as db:
                snapshot_equity(
                    db,
                    account_key="paper",
                    equity=self.total_equity,
                    balance=self.balance,
                    realized_pnl=self.realized_pnl,
                    unrealized_pnl=self.unrealized_pnl,
                    peak_equity=getattr(self.risk_manager, "peak_equity", self.total_equity),
                    source="paper",
                )
        except Exception:
            logger.exception("paper: failed to persist equity snapshot")

    def _persist_position(self) -> None:
        if self.position is None or (self.session_factory is None and self.session is None):
            return
        try:
            def work(db):
                db.query(PaperPosition).filter_by(status="open").delete()
                db.add(self._position_row())
                db.commit()

            if self.session_factory is not None:
                with self.session_factory() as db:
                    work(db)
            else:
                work(self.session)
        except Exception:
            logger.exception("paper: failed to persist position")

    def _close_position_row(self) -> None:
        if self.session_factory is None and self.session is None:
            return
        try:
            def work(db):
                pos = db.query(PaperPosition).filter_by(status="open").order_by(PaperPosition.id.desc()).first()
                if pos is not None:
                    pos.status = "closed"
                    pos.updated_at = datetime.now(timezone.utc)
                    db.commit()

            if self.session_factory is not None:
                with self.session_factory() as db:
                    work(db)
            else:
                work(self.session)
        except Exception:
            logger.exception("paper: failed to finalise position row")

    def _position_row(self) -> PaperPosition:
        p = self.position
        return PaperPosition(
            symbol=p["symbol"],
            side=p["side"],
            quantity=p["size"],
            entry_price=p["entry_price"],
            current_price=p["current_price"],
            notional=p["notional"],
            leverage=p["leverage"],
            stop_loss=p["stop_loss"],
            take_profit=p["take_profit"],
            unrealized_pnl=p["unrealized_pnl"],
            realized_pnl=p["realized_pnl"],
            fees=p["fees"],
            slippage=p["slippage"],
            status=p["status"],
            opened_at=p["opened_at"],
            updated_at=p["updated_at"],
        )

    @staticmethod
    def _position_from_row(p: PaperPosition) -> dict:
        return {
            "symbol": p.symbol,
            "side": p.side,
            "direction": p.side,
            "size": p.quantity,
            "quantity": p.quantity,
            "entry_price": p.entry_price,
            "current_price": p.current_price,
            "notional": p.notional,
            "leverage": p.leverage,
            "stop_loss": p.stop_loss,
            "target": p.take_profit,
            "take_profit": p.take_profit,
            "unrealized_pnl": p.unrealized_pnl,
            "realized_pnl": p.realized_pnl,
            "fees": p.fees,
            "slippage": p.slippage,
            "opened_at": p.opened_at,
            "updated_at": p.updated_at,
            "status": p.status,
            "reason": "restored",
        }

    # ------------------------------------------------------------------ fills

    def _entry_price(self, side: str, price: float) -> float:
        """Adverse slippage on entry: long buys higher, short sells lower."""
        if side == "short":
            return price * (1.0 - self.slippage_pct / 100.0)
        return price * (1.0 + self.slippage_pct / 100.0)

    def _exit_price(self, side: str, price: float) -> float:
        """Adverse slippage on exit: long sells lower, short buys back higher."""
        if side == "long":
            return price * (1.0 - self.slippage_pct / 100.0)
        return price * (1.0 + self.slippage_pct / 100.0)

    def _fee(self, size: float, price: float) -> float:
        notional = size * price * self.contract_value
        return notional * self.fee_pct / 100.0

    def open_position(
        self,
        side: str,
        size: float,
        price: float,
        stop_loss: float,
        target: float,
        reason: str = "signal",
        symbol: str | None = None,
        bar_index: int | None = None,
    ) -> bool:
        if self.position is not None:
            logger.info("paper: ignoring open %s, already in a position", side)
            return False
        try:
            size_value = float(size)
            price_value = float(price)
            stop_value = float(stop_loss)
            target_value = float(target)
        except (TypeError, ValueError):
            logger.error("paper: rejected non-numeric entry")
            return False
        if side not in ("long", "short") or not all(
            math.isfinite(v) and v > 0 for v in (size_value, price_value, stop_value, target_value)
        ):
            logger.error("paper: rejected invalid entry side/size/price/stop/target")
            return False
        if side == "long" and not (stop_value < price_value < target_value):
            logger.error("paper: rejected long entry with invalid stop/target")
            return False
        if side == "short" and not (target_value < price_value < stop_value):
            logger.error("paper: rejected short entry with invalid stop/target")
            return False
        size = size_value
        price = price_value
        stop_loss = stop_value
        target = target_value

        entry = self._entry_price(side, price)
        notional = size * entry * self.contract_value
        equity = self.equity
        leverage = notional / equity if equity > 0 else 0.0
        entry_fee = self._fee(size, entry)
        now = self._clock()

        self.position = {
            "symbol": symbol or self.symbol,
            "side": side,
            "direction": side,
            "size": size,
            "quantity": size,
            "entry_price": entry,
            "current_price": entry,
            "notional": notional,
            "leverage": round(leverage, 3),
            "stop_loss": stop_loss,
            "target": target,
            "take_profit": target,
            "unrealized_pnl": 0.0,
            "realized_pnl": 0.0,
            "fees": entry_fee,
            "slippage": round(self.slippage_pct * 100.0, 3),  # bps
            "opened_at": now,
            "updated_at": now,
            "status": "open",
            "reason": reason,
            "opened_bar_index": bar_index,
        }
        self._persist_position()
        self._record_equity_snapshot()
        if self.risk_manager is not None:
            try:
                self.risk_manager.on_position_opened(bar_index=bar_index)
            except Exception:
                logger.exception("paper: risk manager failed to record open position")
        logger.info(
            "paper: OPEN %s size=%.4f entry=%s notional=%.2f lev=%sx",
            side.upper(), size, entry, notional, round(leverage, 2),
        )
        return True

    def update_position(self, current_price: float) -> dict | None:
        """Mark the open position to a new price (unrealized P&L only)."""
        if self.position is None:
            return None
        pos = self.position
        entry = pos["entry_price"]
        if entry <= 0 or current_price <= 0:
            return pos
        mult = 1.0 if pos["side"] == "long" else -1.0
        gross = pos["size"] * (current_price - entry) * mult * self.contract_value
        pos["current_price"] = current_price
        pos["unrealized_pnl"] = gross - pos["fees"]
        pos["updated_at"] = datetime.now(timezone.utc)
        persisted = pos.get("_persisted_price")
        if persisted is None or current_price != persisted:
            self._persist_position()
            pos["_persisted_price"] = current_price
            self._record_equity_snapshot()
        return pos

    def close_position(self, price: float, reason: str, bar_index: int | None = None) -> dict | None:
        if self.position is None:
            return None
        pos = self.position
        side = pos["side"]
        exit_price = self._exit_price(side, price)
        mult = 1.0 if side == "long" else -1.0
        gross = pos["size"] * (exit_price - pos["entry_price"]) * mult * self.contract_value
        exit_fee = self._fee(pos["size"], exit_price)
        total_fees = pos["fees"] + exit_fee
        net = gross - total_fees
        self.realized_pnl += net
        self.position = None

        trade = PaperTrade(
            symbol=pos["symbol"],
            side=side,
            size=pos["size"],
            entry_price=pos["entry_price"],
            stop_loss=pos["stop_loss"],
            take_profit=pos["take_profit"],
            exit_price=exit_price,
            status="closed",
            pnl=net,
            notional=pos["notional"],
            leverage=pos["leverage"],
            fees=total_fees,
            slippage=pos["slippage"],
            exit_reason=reason,
            closed_at=self._clock(),
        )
        self.trades.append(trade)
        self._persist_closed_trade(trade)
        close_event_id = f"paper-close:{trade.id}" if trade.id is not None else f"paper-close:{uuid.uuid4().hex}"
        self._close_position_row()
        self._upsert_account()
        self._record_equity_snapshot()
        if self.risk_manager is not None:
            try:
                applied = self.risk_manager.on_trade_closed(net, bar_index=bar_index, event_id=close_event_id)
                if not applied:
                    logger.warning("paper: close event %s was already applied", close_event_id)
            except Exception:
                logger.exception("paper: risk manager failed to record closed trade")
        logger.info(
            "paper: CLOSE %s @ %s (%s) pnl=%.2f fees=%.4f",
            side.upper(), exit_price, reason, net, total_fees,
        )
        return {
            "side": side,
            "pnl": net,
            "reason": reason,
            "fees": total_fees,
            "exit_price": exit_price,
            "slippage": pos["slippage"],
            "event_id": close_event_id,
            "closed_at": trade.closed_at,
            "entry_price": pos["entry_price"],
            "size": pos["size"],
        }

    def _persist_closed_trade(self, trade: PaperTrade) -> None:
        if self.session_factory is None and self.session is None:
            return
        try:
            if self.session_factory is not None:
                with self.session_factory() as db:
                    db.add(trade)
                    db.commit()
            else:
                self.session.add(trade)
                self.session.commit()
        except Exception:
            logger.exception("failed to persist paper trade")

    # -------------------------------------------------------------------- loop

    def on_candle(self, candle: dict) -> dict | None:
        """Process a completed candle: check stop/target for the open position."""
        if self.position is None:
            return None
        high, low = float(candle["high"]), float(candle["low"])
        pos = self.position
        bar_index = int(candle["time"]) if candle.get("time") is not None else None

        if pos["side"] == "long":
            if low <= pos["stop_loss"]:
                return self.close_position(pos["stop_loss"], "stop", bar_index=bar_index)
            if high >= pos["target"]:
                return self.close_position(pos["target"], "target", bar_index=bar_index)
        else:
            if high >= pos["stop_loss"]:
                return self.close_position(pos["stop_loss"], "stop", bar_index=bar_index)
            if low <= pos["target"]:
                return self.close_position(pos["target"], "target", bar_index=bar_index)
        return self.update_position(float(candle["close"]))

    def on_signal(self, sig: Signal | dict, price: float | None = None) -> dict | None:
        """React to a validated signal: open if flat + approved, close otherwise.

        There are no implicit defaults here: a missing risk approval, size,
        stop, target or data-quality record is a rejection, never a trade.
        """
        try:
            signal = validate_signal(
                sig,
                profile=self._profile_for_validation(),
                now=self._clock(),
            )
        except SignalValidationError as exc:
            logger.warning("paper: signal rejected: %s", exc)
            return {"action": "rejected", "reason": str(exc)}

        if signal.direction in ("long", "short"):
            if self.position is not None:
                return {"action": "hold", "reason": "position open"}
            if self.risk_manager is None:
                reason = "risk manager unavailable"
                logger.warning("paper: signal rejected: %s", reason)
                return {"action": "rejected", "reason": reason}
            decision = self.risk_manager.evaluate_decision(
                signal.model_dump(),
                equity=self.equity,
                contract_value=self.contract_value,
                now=self._clock(),
                bar_index=signal.candle_time,
                timeframe=signal.timeframe,
            )
            if not decision.allowed or decision.size is None:
                logger.warning("paper: risk rejected entry: %s", decision.reason)
                return {"action": "rejected", "reason": decision.reason}
            size = decision.size.quantities
            if signal.accepted_size is None or abs(signal.accepted_size - size) > max(1e-9, size * 1e-6):
                reason = (
                    f"accepted_size mismatch: signal={signal.accepted_size} "
                    f"risk={size:.6f}"
                )
                logger.warning("paper: signal rejected: %s", reason)
                return {"action": "rejected", "reason": reason}
            if price is None:
                price = signal.entry
            if price is None or signal.stop_loss is None or signal.target is None:
                return {"action": "rejected", "reason": "entry/stop/target missing"}
            opened = self.open_position(
                signal.direction,
                size,
                price,
                signal.stop_loss,
                signal.target,
                reason="signal",
                symbol=signal.symbol,
                bar_index=signal.candle_time,
            )
            if opened:
                return {"action": "open", "side": signal.direction, "size": size, "price": price}
            return {"action": "hold", "reason": "open rejected by paper engine"}

        if self.position is not None:
            price = price or signal.entry or self.position["entry_price"]
            return self.close_position(price, "signal", bar_index=signal.candle_time)
        return {"action": "noop"}
