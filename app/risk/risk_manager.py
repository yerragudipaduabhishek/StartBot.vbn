"""Hard risk engine with persisted lifecycle state.

All entry decisions are fail-closed. The manager owns daily-loss, drawdown,
position/trade counts, cooldown, exposure and data-quality checks, and persists
its state after every mutation so a restart cannot reset or double-count P&L.
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime, timezone

from sqlalchemy import select

from app.contracts import ExecutionMode, RiskApprovalStatus
from app.database.models import RiskEvent, RiskState as RiskStateRow
from app.execution.modes import (
    LiveTradingDisabled,
    ResearchBoundaryError,
    mode_allows_persistence,
    normalize_mode,
)
from app.risk.config import RiskSettings
from app.risk.decision import RiskDecision
from app.risk.kill_switch import KillSwitch
from app.risk.position_sizer import PositionSize, size_position
from app.risk.state import RiskStateSnapshot
from app.strategy.timeframes import timeframe_seconds

logger = logging.getLogger(__name__)


class RiskManager:
    def __init__(
        self,
        settings: RiskSettings | None = None,
        starting_equity: float = 10_000.0,
        *,
        session_factory=None,
        account_key: str = "paper",
        clock=None,
        kill_switch: KillSwitch | None = None,
        restore: bool = True,
        execution_mode: ExecutionMode | str = ExecutionMode.PAPER,
        profile=None,
    ) -> None:
        selected_mode = getattr(profile, "mode", execution_mode)
        self.execution_mode = normalize_mode(selected_mode)
        if self.execution_mode == ExecutionMode.LIVE:
            raise LiveTradingDisabled("live mode is disabled")
        self.persistence_allowed = mode_allows_persistence(self.execution_mode)
        profile_risk = getattr(profile, "risk", None)
        self.settings: RiskSettings = (
            profile_risk if isinstance(profile_risk, RiskSettings) else (settings or RiskSettings())
        )
        self.starting_equity = float(starting_equity)
        self.peak_equity = float(starting_equity)
        self.realized_pnl = 0.0
        self.day = date.today()
        self.day_pnl = 0.0
        self.open_positions = 0
        self.trades_today = 0
        self.last_trade_bar_index: int | None = None
        self.trading_halted = False
        self.halt_reason = ""
        self.persistence_error = ""
        self.session_factory = session_factory if self.persistence_allowed else None
        self.account_key = account_key
        self.kill_switch = kill_switch
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._applied_events: set[str] = set()
        self._restored = False
        if self.session_factory is not None and restore:
            self.restore()

    # -------------------------------------------------------------- state mgmt

    @property
    def current_equity(self) -> float:
        return self.starting_equity + self.realized_pnl

    @property
    def mode(self) -> ExecutionMode:
        return self.execution_mode

    def require_persistence(self) -> None:
        if not self.persistence_allowed:
            raise ResearchBoundaryError(
                f"{self.execution_mode.value} mode must not persist trading state"
            )

    def research_copy(self) -> "RiskManager":
        """Independent, non-persisting risk state for offline replay."""
        clone = RiskManager(
            settings=self.settings,
            starting_equity=self.starting_equity,
            account_key=self.account_key,
            clock=self._clock,
            kill_switch=self.kill_switch,
            restore=False,
            execution_mode=ExecutionMode.RESEARCH,
        )
        clone.peak_equity = self.peak_equity
        clone.realized_pnl = self.realized_pnl
        clone.day = self.day
        clone.day_pnl = self.day_pnl
        clone.trading_halted = self.trading_halted
        clone.halt_reason = self.halt_reason
        return clone

    def snapshot(self) -> RiskStateSnapshot:
        return RiskStateSnapshot(
            day=self.day,
            day_pnl=self.day_pnl,
            peak_equity=self.peak_equity,
            realized_pnl=self.realized_pnl,
            open_positions=self.open_positions,
            trades_today=self.trades_today,
            last_trade_bar_index=self.last_trade_bar_index,
            trading_halted=self.trading_halted,
            halt_reason=self.halt_reason,
        )

    def restore(self) -> bool:
        if not self.persistence_allowed:
            return False
        if self.session_factory is None:
            return False
        try:
            with self.session_factory() as db:
                row = db.execute(select(RiskStateRow).where(RiskStateRow.account_key == self.account_key)).scalar_one_or_none()
                if row is None:
                    return False
                self.starting_equity = float(row.starting_equity)
                self.peak_equity = float(row.peak_equity)
                self.realized_pnl = float(row.realized_pnl or 0.0)
                self.day = row.day
                self.day_pnl = float(row.day_pnl or 0.0)
                self.open_positions = int(row.open_positions or 0)
                self.trades_today = int(row.trades_today or 0)
                self.last_trade_bar_index = row.last_trade_bar_index
                self.trading_halted = bool(row.trading_halted)
                self.halt_reason = row.halt_reason or ""
                self._restored = True
                self.check_daily_reset()
                logger.info("risk: restored %s state", self.account_key)
                return True
        except Exception:
            logger.exception("risk: failed to restore state; failing closed")
            self.trading_halted = True
            self.halt_reason = "risk state unavailable"
            return False

    def bind_persistence(self, session_factory, account_key: str | None = None) -> bool:
        self.require_persistence()
        self.session_factory = session_factory
        if account_key:
            self.account_key = account_key
        return self.restore()

    def record_trade(
        self,
        pnl: float,
        bar_index: int | None = None,
        event_id: str | None = None,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Apply one closed-trade P&L exactly once.

        ``now`` lets a historical replay roll the daily ledger on simulated
        candle time instead of the wall clock. Returns False for an
        already-applied event id.
        """
        try:
            amount = float(pnl)
        except (TypeError, ValueError):
            logger.error("risk: rejected non-numeric trade P&L %r", pnl)
            return False
        if not math.isfinite(amount):
            logger.error("risk: rejected non-finite trade P&L %r", pnl)
            return False
        if event_id and self._event_seen(event_id):
            logger.info("risk: duplicate close event %s ignored", event_id)
            return False

        self.check_daily_reset(now.date() if now is not None else None)
        self.realized_pnl += amount
        self.day_pnl += amount
        self.peak_equity = max(self.peak_equity, self.current_equity)
        self.open_positions = max(0, self.open_positions - 1)
        if bar_index is not None:
            self.last_trade_bar_index = int(bar_index)
        self._apply_persistence(amount, bar_index, event_id)
        return True

    def on_trade_closed(
        self,
        pnl: float,
        bar_index: int | None = None,
        event_id: str | None = None,
        *,
        now: datetime | None = None,
    ) -> bool:
        return self.record_trade(pnl, bar_index=bar_index, event_id=event_id, now=now)

    def on_position_opened(self, bar_index: int | None = None, *, now: datetime | None = None) -> None:
        self.check_daily_reset(now.date() if now is not None else None)
        self.open_positions += 1
        self.trades_today += 1
        if bar_index is not None:
            self.last_trade_bar_index = int(bar_index)
        self._persist()

    def sync_from_paper(self, paper) -> None:
        """Mirror the restored paper account into risk state.

        Peak equity is never reduced, and day P&L remains owned by the risk
        ledger so syncing cannot erase an intraday loss.
        """
        if paper is None:
            return
        self.starting_equity = float(paper.starting_equity)
        self.realized_pnl = float(paper.realized_pnl)
        self.open_positions = 1 if paper.position else 0
        self.peak_equity = max(self.peak_equity, self.current_equity)
        self.check_daily_reset()
        self._persist()

    def halt(self, reason: str) -> None:
        reason = str(reason or "risk halt")
        if not self.trading_halted or self.halt_reason != reason:
            self.trading_halted = True
            self.halt_reason = reason
            logger.error("risk: trading halted: %s", reason)
            self._persist()

    def resume(self) -> None:
        self.trading_halted = False
        self.halt_reason = ""
        self._persist()

    def check_daily_reset(self, today: date | None = None) -> bool:
        today = today or self._clock().date()
        if today != self.day:
            self.day = today
            self.day_pnl = 0.0
            self.trades_today = 0
            self._persist()
            return True
        return False

    def record_trade_daily_reset(self) -> None:
        self.check_daily_reset()

    # --------------------------------------------------------------- persistence

    def _event_seen(self, event_id: str) -> bool:
        if event_id in self._applied_events:
            return True
        if not self.persistence_allowed or self.session_factory is None:
            return False
        try:
            with self.session_factory() as db:
                row = db.get(RiskEvent, event_id)
                if row is not None:
                    self._applied_events.add(event_id)
                    return True
        except Exception as exc:
            logger.exception("risk: failed to query close event; failing closed")
            self._mark_persistence_failure("close-event query", exc)
            return True
        return False

    def _mark_persistence_failure(self, operation: str, exc: BaseException) -> None:
        """Fail closed when authoritative state cannot be read or written."""
        self.persistence_error = f"{operation}: {exc}"
        self.trading_halted = True
        self.halt_reason = f"risk persistence failure ({operation})"
        logger.error("risk: %s", self.halt_reason)

    def _apply_persistence(self, pnl: float, bar_index: int | None, event_id: str | None) -> None:
        if not self.persistence_allowed:
            if event_id:
                self._applied_events.add(event_id)
            return
        if self.session_factory is None:
            if event_id:
                self._applied_events.add(event_id)
            return
        try:
            with self.session_factory() as db:
                if event_id:
                    existing = db.get(RiskEvent, event_id)
                    if existing is not None:
                        self._applied_events.add(event_id)
                        return
                    db.add(RiskEvent(event_id=event_id, account_key=self.account_key, pnl=pnl, bar_index=bar_index))
                    self._applied_events.add(event_id)
                row = self._write_state(db)
                db.commit()
                _ = row
        except Exception as exc:
            logger.exception("risk: failed to persist closed-trade P&L")
            self._mark_persistence_failure("closed-trade write", exc)

    def _persist(self) -> None:
        if not self.persistence_allowed or self.session_factory is None:
            return
        try:
            with self.session_factory() as db:
                self._write_state(db)
                db.commit()
        except Exception as exc:
            logger.exception("risk: failed to persist state")
            self._mark_persistence_failure("state write", exc)

    def _write_state(self, db) -> RiskStateRow:
        row = db.execute(select(RiskStateRow).where(RiskStateRow.account_key == self.account_key)).scalar_one_or_none()
        if row is None:
            row = RiskStateRow(account_key=self.account_key, starting_equity=self.starting_equity, peak_equity=self.peak_equity, day=self.day)
            db.add(row)
        row.starting_equity = self.starting_equity
        row.peak_equity = self.peak_equity
        row.realized_pnl = self.realized_pnl
        row.day = self.day
        row.day_pnl = self.day_pnl
        row.open_positions = self.open_positions
        row.trades_today = self.trades_today
        row.last_trade_bar_index = self.last_trade_bar_index
        row.trading_halted = self.trading_halted
        row.halt_reason = self.halt_reason
        row.updated_at = self._clock()
        return row

    # ------------------------------------------------------------------ gates

    def _session_allows(self, now: datetime) -> bool:
        current = now.astimezone(timezone.utc).strftime("%H:%M")
        for window in self.settings.allowed_sessions:
            try:
                start, end = str(window).split("-", 1)
                if start <= current <= end:
                    return True
            except ValueError:
                logger.error("risk: invalid allowed_sessions entry %r; failing closed", window)
                return False
        return False

    def _checks(self, signal: dict, equity: float, *, now: datetime | None = None, timeframe: str | None = None) -> list[str]:
        problems: list[str] = []
        now = now or self._clock()
        self.check_daily_reset(now.date())
        if self.trading_halted:
            problems.append(f"trading halted: {self.halt_reason}")
        if self.kill_switch is not None and self.kill_switch.is_halted():
            problems.append("kill switch is active")
        if signal.get("direction") not in ("long", "short"):
            problems.append(f"direction is {signal.get('direction', 'none')}")
        confidence = signal.get("confidence")
        try:
            confidence_value = float(confidence)
        except (TypeError, ValueError):
            confidence_value = 0.0
        if confidence_value < self.settings.min_confidence:
            problems.append(f"confidence {confidence_value:.2f} < {self.settings.min_confidence}")
        if self.open_positions >= self.settings.max_positions:
            problems.append(f"{self.open_positions} positions already open")
        if self.trades_today >= self.settings.max_daily_trades:
            problems.append(f"daily trade limit reached ({self.trades_today}/{self.settings.max_daily_trades})")
        if self.day_pnl <= -self.starting_equity * self.settings.max_daily_loss_pct / 100.0:
            problems.append(
                f"daily loss limit hit (day PnL {self.day_pnl:,.2f} <= -{self.settings.max_daily_loss_pct:.0f}%)"
            )
        dd = (self.peak_equity - equity) / self.peak_equity * 100.0 if self.peak_equity else 0.0
        if dd >= self.settings.max_drawdown_pct:
            self.halt(f"max drawdown {self.settings.max_drawdown_pct:.0f}% breached")
            problems.append(self.halt_reason)
        if equity < self.settings.min_equity:
            problems.append(f"equity {equity:,.2f} below minimum {self.settings.min_equity:,.2f}")
        if not self._session_allows(now):
            problems.append("outside allowed trading session")

        dq = signal.get("data_quality")
        if isinstance(dq, dict):
            status = dq.get("status")
            if self.settings.require_streaming_data and status != "streaming":
                problems.append(f"data is not streaming: {status}")
            if not dq.get("price_valid", False):
                problems.append("price is invalid")
            age = dq.get("last_candle_age_secs")
            try:
                age_value = float(age) if age is not None else None
            except (TypeError, ValueError):
                age_value = None
            if age_value is not None and age_value > self.settings.max_candle_age_secs:
                problems.append(f"candle is stale ({age_value:.0f}s)")
            spread = _as_float(dq.get("spread_bps"))
            if spread is not None and spread > self.settings.max_spread_bps:
                problems.append(f"spread {spread:.1f}bps > {self.settings.max_spread_bps:.1f}bps")
            slippage = _as_float(dq.get("slippage_bps"))
            if slippage is not None and slippage > self.settings.max_slippage_bps:
                problems.append(f"slippage {slippage:.1f}bps > {self.settings.max_slippage_bps:.1f}bps")

        current_bar = signal.get("candle_time")
        if current_bar is not None and self.last_trade_bar_index is not None:
            try:
                elapsed = (int(current_bar) - int(self.last_trade_bar_index)) / timeframe_seconds(timeframe or signal.get("timeframe") or "1h")
            except (TypeError, ValueError):
                elapsed = 0.0
            if elapsed < self.settings.cooldown_bars:
                problems.append(f"cooldown active ({elapsed:.1f} < {self.settings.cooldown_bars} bars)")
        return problems

    def evaluate_decision(
        self,
        signal: dict,
        equity: float,
        contract_value: float = 0.001,
        *,
        now: datetime | None = None,
        bar_index: int | None = None,
        timeframe: str | None = None,
    ) -> RiskDecision:
        if not isinstance(signal, dict) and hasattr(signal, "model_dump"):
            signal = signal.model_dump()
        stop = _as_float(signal.get("stop_loss"))
        entry = _as_float(signal.get("entry") or signal.get("expected_price"))
        target = _as_float(signal.get("target") or signal.get("take_profit"))
        confidence = _as_float(signal.get("confidence"))
        symbol = str(signal.get("symbol") or "BTCUSDT").upper()
        resolved_timeframe = str(signal.get("timeframe") or timeframe or "1h")
        strategy_id = str(signal.get("strategy_id") or "strategy_a")
        strategy_version = str(signal.get("strategy_version") or "")
        data_quality = signal.get("data_quality") if isinstance(signal.get("data_quality"), dict) else {}
        data_source = str(signal.get("data_source") or data_quality.get("source") or "unknown")
        key = str(
            signal.get("signal_key")
            or f"{strategy_id}:{symbol}:{resolved_timeframe}:{signal.get('candle_time', 0)}"
        )

        def decision(
            allowed: bool,
            size: PositionSize | None,
            reason: str,
            notes: tuple[str, ...] = (),
        ) -> RiskDecision:
            return RiskDecision(
                allowed=allowed,
                size=size,
                reason=reason,
                checks=notes,
                signal_key=key,
                approval_status=(
                    RiskApprovalStatus.APPROVED if allowed else RiskApprovalStatus.REJECTED
                ),
                signal_id=key,
                symbol=symbol,
                timeframe=resolved_timeframe,
                strategy_id=strategy_id,
                strategy_version=strategy_version,
                data_source=data_source,
                confidence=confidence,
                entry=entry,
                stop_loss=stop,
                take_profit=target,
                execution_mode=self.execution_mode,
            )

        checks = self._checks(signal, equity, now=now, timeframe=resolved_timeframe)
        if checks:
            return decision(False, None, "; ".join(checks), tuple(checks))

        if stop is None or entry is None or stop <= 0 or entry <= 0:
            reason = "no stop loss available"
            return decision(False, None, reason, (reason,))
        direction = signal.get("direction")
        if direction == "long" and not stop < entry:
            reason = "long stop_loss must be below entry"
            return decision(False, None, reason, (reason,))
        if direction == "short" and not stop > entry:
            reason = "short stop_loss must be above entry"
            return decision(False, None, reason, (reason,))
        if target is not None and target > 0:
            if direction == "long" and not target > entry:
                reason = "long target must be above entry"
                return decision(False, None, reason, (reason,))
            if direction == "short" and not target < entry:
                reason = "short target must be below entry"
                return decision(False, None, reason, (reason,))

        vol_skip = self.settings.volatility_skip_pct
        prev_close = _as_float(signal.get("prev_close"))
        if prev_close and prev_close > 0:
            atr = abs(stop - entry) / self.settings.sl_atr_multiple
            if atr / prev_close * 100.0 > vol_skip:
                reason = f"volatility too high (ATR {atr/prev_close*100:.2f}% > {vol_skip:.1f}%)"
                return decision(False, None, reason, (reason,))

        size = size_position(
            equity,
            entry,
            stop,
            self.settings.risk_per_trade_pct,
            contract_value=contract_value,
            max_leverage=self.settings.max_leverage,
        )
        if size is None or size.quantities <= 0:
            reason = "position sizing failed"
            return decision(False, None, reason, (reason,))

        if size.notional_usd > equity * self.settings.max_order_notional_pct / 100.0:
            reason = f"order notional {size.notional_usd:,.2f} exceeds {self.settings.max_order_notional_pct:.1f}% of equity"
            return decision(False, None, reason, (reason,))
        if size.notional_usd > equity * self.settings.max_gross_exposure_pct / 100.0:
            reason = f"gross exposure {size.notional_usd:,.2f} exceeds {self.settings.max_gross_exposure_pct:.1f}% of equity"
            return decision(False, None, reason, (reason,))
        if size.notional_usd > equity * self.settings.max_symbol_exposure_pct / 100.0:
            reason = f"symbol exposure {size.notional_usd:,.2f} exceeds {self.settings.max_symbol_exposure_pct:.1f}% of equity"
            return decision(False, None, reason, (reason,))

        return decision(True, size, size.explanation)

    def evaluate(
        self,
        signal: dict,
        equity: float,
        contract_value: float = 0.001,
        *,
        now: datetime | None = None,
        bar_index: int | None = None,
        timeframe: str | None = None,
    ) -> tuple[bool, PositionSize | None, str]:
        """Return (allowed, size, reason). Backwards-compatible wrapper."""
        decision = self.evaluate_decision(
            signal, equity, contract_value, now=now, bar_index=bar_index, timeframe=timeframe
        )
        return decision.allowed, decision.size, decision.reason


def _as_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
