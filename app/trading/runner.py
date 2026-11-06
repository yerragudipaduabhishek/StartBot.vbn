"""Trading runner: async loop that turns closed candles into signals and fills.

The runner seeds its cursor on startup so a restart never auto-enters. It only
passes a fully validated signal to the paper engine, persists the profile and
data provenance with every decision, and never touches a live order endpoint.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import select

from app.config.trading import TradingProfile, profile_from_settings
from app.data.market_data import MarketData
from app.data.news import fetch_headlines, headlines_from
from app.database.database import SessionLocal
from app.database.models import RunnerState, Signal as SignalRow
from app.risk.kill_switch import KillSwitch
from app.risk.risk_manager import RiskManager
from app.strategy.signal import Signal
from app.strategy.signal_engine import SignalEngine
from app.strategy.validation import SignalValidationError, data_quality_from_snapshot, validate_signal
from app.trading.paper_trader import PaperTrader

logger = logging.getLogger(__name__)


class TradingRunner:
    def __init__(
        self,
        market: MarketData,
        engine: SignalEngine,
        risk_manager: RiskManager,
        paper_trader: PaperTrader,
        poll_secs: float = 15.0,
        news_getter=None,
        *,
        profile: TradingProfile | None = None,
        kill_switch: KillSwitch | None = None,
        session_factory=None,
        seed_on_startup: bool = True,
        clock=None,
    ) -> None:
        self.market = market
        self.engine = engine
        self.risk_manager = risk_manager
        self.paper = paper_trader
        self.profile = profile or profile_from_settings(mode="paper")
        self.kill_switch = kill_switch
        self.session_factory = session_factory or SessionLocal
        self.poll_secs = poll_secs
        self.news_getter = news_getter or (lambda: fetch_headlines())
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._startup_complete = not seed_on_startup
        self._processed_candle: int | None = None
        self._running = False
        self._paused = False
        self._started_at: datetime | None = None
        self._last_cycle_at: datetime | None = None
        self._cycle_count = 0
        self._last_error: str | None = None
        self.last_signal: dict | None = None

    # ------------------------------------------------------------------ loop

    async def run(self) -> None:
        self._running = True
        self._started_at = self._clock()
        while self._running:
            try:
                if not self._paused:
                    self._process_cycle()
                    self._cycle_count += 1
                    self._last_cycle_at = self._clock()
            except Exception as e:
                self._last_error = str(e)
                logger.exception("runner cycle failed: %s", e)
            await asyncio.sleep(self.poll_secs)

    def stop(self) -> None:
        self._running = False

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        if not self._running:
            raise RuntimeError("runner task is not running")
        self._paused = False

    def status(self) -> dict:
        return {
            "running": self._running,
            "paused": self._paused,
            "status": "paused" if self._paused else ("running" if self._running else "stopped"),
            "mode": self.profile.mode.value,
            "profile_id": self.profile.profile_id,
            "strategy_id": self.profile.strategy_id,
            "strategy_version": self.profile.strategy_version,
            "symbol": self.profile.symbol,
            "timeframe": self.profile.timeframe,
            "started_at": self._started_at,
            "last_cycle_at": self._last_cycle_at,
            "cycle_count": self._cycle_count,
            "last_error": self._last_error,
        }

    def _closed_candles(self) -> list[dict]:
        # Last candle may still be forming; only act on fully closed bars.
        return self.market.candles[:-1]

    def _market_snapshot(self) -> dict | None:
        snapshot = getattr(self.market, "snapshot", None)
        if callable(snapshot):
            try:
                return snapshot()
            except Exception:
                logger.exception("runner: market snapshot failed")
        return None

    def _headlines(self) -> list[str]:
        try:
            return headlines_from(self.news_getter())
        except Exception:
            logger.exception("runner: news provider failed; continuing without news")
            return []

    def _process_cycle(self) -> None:
        closed = self._closed_candles()
        if not closed:
            return

        # 1) Always manage an open paper position against the latest closed bar.
        self.paper.on_candle(closed[-1])

        last_time = int(closed[-1]["time"])
        if not self._startup_complete:
            self._startup_complete = True
            self._processed_candle = last_time
            self._persist_runner_state(last_time)
            logger.info("runner: seeded at candle %s; no auto-entry on startup", last_time)
            return

        if last_time == self._processed_candle:
            return
        self._processed_candle = last_time
        self._persist_runner_state(last_time)

        if self.kill_switch is not None and self.kill_switch.is_halted():
            logger.warning("runner: kill switch active; skipping new entry at %s", last_time)
            return

        view = SimpleNamespace(candles=closed, ticker=self.market.ticker or {})
        sig = self.engine.generate(view, headlines=self._headlines())
        if sig.get("direction") == "neutral":
            sig["direction"] = "none"
        sig["time"] = last_time
        sig["symbol"] = self.profile.symbol
        sig["timeframe"] = self.profile.timeframe
        sig["strategy_id"] = self.profile.strategy_id
        sig["strategy_version"] = self.profile.strategy_version
        sig["data_quality"] = data_quality_from_snapshot(self._market_snapshot(), profile=self.profile).model_dump()

        try:
            validated = validate_signal(
                sig,
                profile=self.profile,
                candle_time=last_time,
                now=self._clock(),
                data_quality=sig["data_quality"],
            )
        except SignalValidationError as exc:
            sig["risk_allowed"] = False
            sig["risk_reason"] = str(exc)
            self.last_signal = sig
            self._persist_signal(sig)
            logger.warning("runner: signal rejected at %s: %s", last_time, exc)
            return

        self.last_signal = validated.model_dump()
        self._persist_signal(validated)
        logger.info(
            "signal @ %s -> %s (score %+.2f)",
            last_time,
            validated.direction.upper(),
            validated.score or 0.0,
        )

        # 3) Feed the paper trader only a validated, approved decision.
        action = self.paper.on_signal(validated)
        if action and action.get("action") == "rejected":
            logger.warning("runner: paper rejected validated signal: %s", action.get("reason"))
        self.risk_manager.record_trade_daily_reset()

    # -------------------------------------------------------------- persistence

    @property
    def _runner_key(self) -> str:
        return f"{self.profile.profile_key}:{self.profile.mode.value}"

    def _persist_runner_state(self, candle_time: int) -> None:
        try:
            with self.session_factory() as db:
                row = db.execute(select(RunnerState).where(RunnerState.runner_key == self._runner_key)).scalar_one_or_none()
                if row is None:
                    row = RunnerState(runner_key=self._runner_key)
                    db.add(row)
                row.last_candle_time = int(candle_time)
                row.updated_at = self._clock()
                db.commit()
        except Exception:
            logger.exception("runner: failed to persist cursor")

    def _persist_signal(self, sig: Signal | dict) -> None:
        try:
            if isinstance(sig, Signal):
                data = sig.model_dump()
            else:
                data = dict(sig)
            dq = data.get("data_quality")
            if hasattr(dq, "model_dump"):
                dq = dq.model_dump()
            with self.session_factory() as db:
                db.add(
                    SignalRow(
                        candle_time=int(data.get("candle_time") or data.get("time") or 0),
                        symbol=str(data.get("symbol") or self.profile.symbol),
                        timeframe=str(data.get("timeframe") or self.profile.timeframe),
                        direction=str(data.get("direction") or "none"),
                        score=float(data.get("score") or 0.0),
                        entry_price=data.get("entry"),
                        stop_loss=data.get("stop_loss"),
                        target=data.get("target"),
                        explanation=data.get("explanation"),
                        strategy_id=data.get("strategy_id") or self.profile.strategy_id,
                        strategy_version=data.get("strategy_version") or self.profile.strategy_version,
                        risk_reason=data.get("risk_reason"),
                        data_quality=json.dumps(dq) if dq is not None else None,
                    )
                )
                db.commit()
        except Exception:
            logger.exception("failed to persist signal")
