"""Mandatory pre-trade guard for every paper/testnet/live entry."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from app.config.trading import TradingProfile
from app.database.models import ExecutionIntent
from app.execution.modes import assert_mode_can_submit
from app.portfolio.manager import PortfolioLimits, PortfolioManager
from app.risk.decision import RiskDecision
from app.risk.kill_switch import KillSwitch
from app.risk.risk_manager import RiskManager
from app.strategy.signal import Signal
from app.strategy.validation import SignalValidationError, validate_signal

logger = logging.getLogger(__name__)


class ExecutionGuardError(RuntimeError):
    """Raised when a signal is not safe to execute."""


@dataclass(frozen=True)
class GuardResult:
    signal: Signal
    decision: RiskDecision
    size: float


class TradeGuard:
    def __init__(
        self,
        profile: TradingProfile,
        risk_manager: RiskManager,
        *,
        kill_switch: KillSwitch | None = None,
        session_factory=None,
        portfolio_manager: PortfolioManager | None = None,
    ) -> None:
        assert_mode_can_submit(profile.mode)
        self.profile = profile
        self.risk_manager = risk_manager
        self.kill_switch = kill_switch
        self.session_factory = session_factory
        self.portfolio_manager = portfolio_manager
        self._seen: set[str] = set()

    def _portfolio(self) -> PortfolioManager:
        if self.portfolio_manager is not None:
            return self.portfolio_manager
        settings = self.risk_manager.settings
        equity = max(0.0, float(self.risk_manager.current_equity or 0.0))
        return PortfolioManager(
            PortfolioLimits(
                max_positions=int(settings.max_positions),
                max_gross_notional=equity * float(settings.max_gross_exposure_pct) / 100.0,
                max_symbol_notional={
                    self.profile.symbol: equity * float(settings.max_symbol_exposure_pct) / 100.0
                },
            )
        )

    def check(
        self,
        payload: Signal | dict,
        *,
        requested_size: float | None = None,
        now: datetime | None = None,
    ) -> GuardResult:
        try:
            signal = validate_signal(payload, profile=self.profile, now=now)
        except SignalValidationError as exc:
            raise ExecutionGuardError(str(exc)) from exc

        if signal.direction == "none":
            raise ExecutionGuardError("no trade signal")
        if self.kill_switch is not None and self.kill_switch.is_halted():
            raise ExecutionGuardError("kill switch is active")

        decision = self.risk_manager.evaluate_decision(
            signal.model_dump(),
            equity=self.risk_manager.current_equity,
            contract_value=self.profile.contract_value,
            now=now,
            bar_index=signal.candle_time,
            timeframe=signal.timeframe,
        )
        if not decision.allowed or decision.size is None:
            raise ExecutionGuardError(decision.reason)
        approved, portfolio_reason = self._portfolio().approve(
            symbol=signal.symbol,
            notional=float(decision.size.notional_usd),
            open_positions=int(self.risk_manager.open_positions),
        )
        if not approved:
            raise ExecutionGuardError(f"portfolio: {portfolio_reason}")
        if signal.accepted_size is None or not math.isclose(
            signal.accepted_size, decision.size.quantities, rel_tol=1e-6, abs_tol=1e-9
        ):
            raise ExecutionGuardError(
                f"accepted_size mismatch: signal={signal.accepted_size} risk={decision.size.quantities:.6f}"
            )
        if requested_size is not None:
            try:
                requested = float(requested_size)
            except (TypeError, ValueError) as exc:
                raise ExecutionGuardError("requested size is invalid") from exc
            if not math.isclose(requested, decision.size.quantities, rel_tol=1e-6, abs_tol=1e-9):
                raise ExecutionGuardError(
                    f"caller size mismatch: requested={requested:.6f} risk={decision.size.quantities:.6f}"
                )
        return GuardResult(signal, decision, decision.size.quantities)

    def reserve(self, signal: Signal, venue: str) -> None:
        """Reserve this candle key exactly once before order placement."""
        key = signal.key
        if self.session_factory is None:
            if key in self._seen:
                raise ExecutionGuardError(f"duplicate signal already executed: {key}")
            self._seen.add(key)
            return
        try:
            with self.session_factory() as db:
                existing = db.execute(select(ExecutionIntent).where(ExecutionIntent.signal_key == key)).scalar_one_or_none()
                if existing is not None:
                    raise ExecutionGuardError(f"duplicate signal already executed: {key}")
                db.add(ExecutionIntent(signal_key=key, venue=venue, status="reserved"))
                db.commit()
        except ExecutionGuardError:
            raise
        except Exception as exc:
            logger.exception("guard: failed to reserve execution intent")
            raise ExecutionGuardError(f"duplicate/intent persistence failure: {exc}") from exc
