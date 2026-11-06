"""Local paper-trading soak-test runner (Phase 7A).

Run continuously on a local Windows machine using REAL public market data and
PAPER execution only. Reuses the existing Strategy A signal engine, the risk
engine and the local, persisted PaperTrader. It never calls a Delta
order/account/private endpoint, needs no API credentials, and cannot spend a
single real cent.

Guarantees this module enforces:
  * PAPER ONLY by default (SIMULATED money, separate from the Trading Fund);
  * no order/account endpoint is ever reached (none of those paths are touched);
  * no automatic trade right after a restart (startup seeds the newest closed
    candle so the first cycle only ever *manages*, never *trades*);
  * unavailable / stale / invalid-price data can never become a trade signal;
  * a single broken watchlist symbol does not stop the runner - it is reported
    in telemetry and the rest keep working;
  * recoverable failures are logged with timestamps; unexpected exceptions are
    logged, never silently swallowed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import pandas as pd
from sqlalchemy import func, select

from app.analysis.config import DEFAULT_WATCHLIST
from app.config.settings import Settings, get_settings
from app.config.trading import TradingProfile, profile_from_settings
from app.contracts import ExecutionMode
from app.database.database import SessionLocal
from app.database.models import PaperTrade, Signal
from app.execution.modes import ExecutionModeError
from app.features.pipeline import FeaturePipeline
from app.risk.risk_manager import RiskManager
from app.soak.feed import SoakFeed
from app.soak.telemetry import SoakTelemetry, iso_now
from app.strategy.signal_engine import SignalEngine
from app.strategy.strategy_a import STRATEGY_A_EMAS, STRATEGY_A_WEIGHTS
from app.strategy.validation import SignalValidationError, data_quality_from_snapshot, validate_signal
from app.trading.paper_trader import PaperTrader

logger = logging.getLogger(__name__)

BANNER = "\n".join(
    [
        "=" * 62,
        "  PAPER TRADING",
        "  NOT REAL FUNDS",
        "  NO EXCHANGE ORDERS",
        "  LIVE OFF",
        "=" * 62,
    ]
)

DEFAULT_EMAS = STRATEGY_A_EMAS
DEFAULT_WEIGHTS = dict(STRATEGY_A_WEIGHTS)

TELEMETRY_FIELDS = [
    "timestamp",
    "symbol",
    "market_data_status",
    "data_source",
    "ticker_source",
    "ws_connected",
    "ws_reconnects",
    "ws_failures",
    "ws_messages_received",
    "ws_messages_processed",
    "ws_data_messages",
    "ws_message_types",
    "ws_ticker_messages",
    "ws_funding_messages",
    "ws_handler_errors",
    "ws_parse_errors",
    "last_candle_age_secs",
    "last_ws_message_age_secs",
    "last_ws_ticker_age_secs",
    "last_rest_update_age_secs",
    "last_transport_age_secs",
    "rest_polls",
    "rest_failures",
    "fallback_used",
    "price_valid",
    "open_interest_available",
    "funding_rate_available",
    "paper_balance",
    "open_position",
    "entry_price",
    "current_price",
    "unrealized_pnl",
    "realized_pnl",
    "fees",
    "slippage_bps",
    "trade_count",
    "last_error",
    "cycle_number",
    "run_elapsed_secs",
    "cycle_duration_secs",
    "requested_duration_secs",
    "signal_evaluated",
    "risk_reason",
    "action",
    "action_reason",
    "ema_fast",
    "ema_slow",
    "ema_mid",
    "skip_reason",
    "errors",
]


def build_paper_engine(
    settings: Settings,
    profile: TradingProfile | None = None,
    *,
    session_factory=None,
    kill_switch=None,
) -> tuple[SignalEngine, RiskManager]:
    """Wire the exact Strategy A engine + risk manager from the profile."""
    profile = profile or profile_from_settings(settings, mode="paper")
    if profile.mode != ExecutionMode.PAPER:
        raise ExecutionModeError("the soak runner is paper-only")
    risk = RiskManager(
        starting_equity=settings.paper_fund_inr,
        settings=profile.risk,
        session_factory=session_factory,
        account_key="paper",
        kill_switch=kill_switch,
        execution_mode=profile.mode,
    )
    engine = SignalEngine(
        weights=profile.weights,
        risk_manager=risk,
        decision_threshold=profile.decision_threshold,
        contract_value=profile.contract_value,
        profile=profile,
    )
    return engine, risk


class SoakRunner:
    def __init__(
        self,
        symbol: str | None = None,
        watchlist: list[str] | None = None,
        settings: Settings | None = None,
        risk: RiskManager | None = None,
        engine: SignalEngine | None = None,
        paper: PaperTrader | None = None,
        feeds: dict[str, SoakFeed] | None = None,
        tick_secs: float = 30.0,
        telemetry: SoakTelemetry | None = None,
        telemetry_path: str | None = None,
        enable_ws: bool = True,
        session_factory: object = None,
        feed_poll_secs: float | None = None,
        profile: TradingProfile | None = None,
        kill_switch=None,
    ) -> None:
        self.settings = settings or get_settings()
        self.profile = profile or profile_from_settings(self.settings, mode="paper")
        if self.profile.mode != ExecutionMode.PAPER:
            raise ExecutionModeError("the soak runner is paper-only")
        self.symbol = (symbol or self.profile.symbol).upper()
        if self.symbol != self.profile.symbol:
            raise ValueError(
                f"soak symbol {self.symbol} does not match profile symbol {self.profile.symbol}"
            )
        raw = watchlist if watchlist is not None else list(DEFAULT_WATCHLIST)
        self.watchlist = [s.upper() for s in raw if isinstance(s, str) and s.strip()]
        if self.symbol not in self.watchlist:
            self.watchlist.append(self.symbol)

        if feeds is None:
            if feed_poll_secs is not None:
                feeds = {s: SoakFeed(s, settings=self.settings, enable_ws=enable_ws, poll_secs=feed_poll_secs) for s in self.watchlist}
            else:
                feeds = {s: SoakFeed(s, settings=self.settings, enable_ws=enable_ws) for s in self.watchlist}
        if self.symbol not in feeds:
            feeds[self.symbol] = SoakFeed(self.symbol, settings=self.settings, enable_ws=enable_ws, poll_secs=feed_poll_secs or 45.0)
        self.feeds = feeds

        if engine is None and risk is None:
            engine, risk = build_paper_engine(
                self.settings,
                self.profile,
                session_factory=session_factory,
                kill_switch=kill_switch,
            )
        elif engine is None:
            engine = SignalEngine(
                weights=self.profile.weights,
                risk_manager=risk,
                decision_threshold=self.profile.decision_threshold,
                contract_value=self.profile.contract_value,
                profile=self.profile,
            )
        elif risk is None:
            risk = engine.risk_manager
        self.engine = engine
        self.risk = risk

        if paper is None:
            used_factory = session_factory or SessionLocal
            paper = PaperTrader(
                starting_equity=self.settings.paper_fund_inr,
                session_factory=used_factory,
                fee_pct=self.profile.paper_fee_pct,
                slippage_pct=self.profile.paper_slippage_pct,
                symbol=self.symbol,
                contract_value=self.profile.contract_value,
                profile=self.profile,
                risk_manager=risk,
            )
            if session_factory is None:
                session_factory = SessionLocal
        elif paper.risk_manager is None:
            paper.risk_manager = risk
        if paper.profile is None:
            paper.profile = self.profile
        self.paper = paper
        self.session_factory = session_factory if session_factory is not None else getattr(self.paper, "session_factory", None)
        if self.session_factory is not None:
            self.risk.bind_persistence(self.session_factory)

        self.tick_secs = tick_secs
        self.ema_spans = self.profile.ema_spans
        self.feature_pipeline = FeaturePipeline(ema_spans=self.ema_spans)

        if telemetry is None:
            telemetry = SoakTelemetry(out_path=telemetry_path)
        self.telemetry = telemetry

        self.processed_time: int | None = None
        self.last_action: dict | None = None
        self.start_time: float | None = None
        self.cycles_run = 0
        self.cycle_number = 0
        self.requested_duration_secs: float | None = None
        self._cycle_started_mono: float | None = None
        self._running = False
        self._errors: list[dict] = []

    # ------------------------------------------------------------------ state

    def _sync_risk_equity(self) -> None:
        """Risk sizing tracks the paper account (simulated money only)."""
        if self.risk is None:
            return
        self.risk.sync_from_paper(self.paper)
        self.risk.check_daily_reset()

    def _push_error(self, source: str, message: str) -> None:
        self._errors.append({"timestamp": iso_now(), "source": source, "message": message})
        del self._errors[:-50]

    def _closed_trade_count(self) -> int:
        if self.session_factory is not None:
            try:
                with self.session_factory() as db:
                    return int(
                        db.execute(select(func.count()).select_from(PaperTrade).where(PaperTrade.status == "closed")).scalar_one()
                        or 0
                    )
            except Exception:
                return 0
        return len([t for t in self.paper.trades if getattr(t, "status", "closed") == "closed"])

    def _persist_signal(self, sig: dict) -> None:
        if self.session_factory is None:
            return
        try:
            dq = sig.get("data_quality")
            if hasattr(dq, "model_dump"):
                dq = dq.model_dump()
            with self.session_factory() as db:
                db.add(
                    Signal(
                        symbol=self.symbol,
                        timeframe=self.profile.timeframe,
                        candle_time=int(sig["time"]),
                        direction=sig.get("direction", "none"),
                        score=float(sig.get("score") or 0.0),
                        entry_price=sig.get("entry"),
                        stop_loss=sig.get("stop_loss"),
                        target=sig.get("target"),
                        explanation=sig.get("explanation"),
                        strategy_id=self.profile.strategy_id,
                        strategy_version=self.profile.strategy_version,
                        risk_reason=sig.get("risk_reason"),
                        data_quality=json.dumps(dq) if dq is not None else None,
                    )
                )
                db.commit()
        except Exception:
            logger.exception("soak: failed to persist signal")
            self._push_error("persist", "signal write failed")

    # ---------------------------------------------------------------- telemetry

    @staticmethod
    def _market_telemetry(snap: dict) -> dict:
        ws = snap.get("ws_status") or {}
        return {
            "market_data_status": snap.get("data_status"),
            "data_source": snap.get("data_source"),
            "ticker_source": snap.get("ticker_source"),
            "ws_connected": bool(ws.get("connected")),
            "ws_reconnects": ws.get("reconnects", 0),
            "ws_failures": ws.get("failures", 0),
            "ws_messages_received": ws.get("messages_received", 0),
            "ws_messages_processed": ws.get("messages_processed", 0),
            "ws_data_messages": ws.get("data_messages", 0),
            "ws_message_types": ws.get("message_types", {}),
            "ws_ticker_messages": ws.get("ticker_messages", 0),
            "ws_funding_messages": ws.get("funding_messages", 0),
            "ws_handler_errors": ws.get("handler_errors", 0),
            "ws_parse_errors": ws.get("parse_errors", 0),
            "last_candle_age_secs": snap.get("last_candle_age_secs"),
            "last_ws_message_age_secs": snap.get("last_ws_message_age_secs"),
            "last_ws_ticker_age_secs": snap.get("last_ws_ticker_age_secs"),
            "last_rest_update_age_secs": snap.get("last_rest_update_age_secs"),
            "last_transport_age_secs": snap.get("last_transport_age_secs"),
            "rest_polls": snap.get("rest_polls", 0),
            "rest_failures": snap.get("rest_failures", 0),
            "fallback_used": bool(snap.get("fallback_used")),
            "price_valid": bool(snap.get("price_valid")),
            "open_interest_available": snap.get("open_interest") is not None,
            "funding_rate_available": snap.get("funding_rate") is not None,
            "last_error": snap.get("last_error"),
        }

    def _runtime_telemetry(self) -> dict:
        now = time.monotonic()
        return {
            "cycle_number": self.cycle_number,
            "run_elapsed_secs": round(now - self.start_time, 3) if self.start_time is not None else None,
            "cycle_duration_secs": round(now - self._cycle_started_mono, 3) if self._cycle_started_mono is not None else None,
            "requested_duration_secs": self.requested_duration_secs,
        }

    def _feed_row(self, feed: SoakFeed, symbol: str) -> dict:
        snap = feed.snapshot()
        row = {
            "symbol": symbol,
            **self._market_telemetry(snap),
            **self._runtime_telemetry(),
            "paper_balance": None,
            "open_position": None,
            "entry_price": None,
            "current_price": None,
            "unrealized_pnl": None,
            "realized_pnl": None,
            "fees": 0.0,
            "slippage_bps": 0.0,
            "trade_count": 0,
            "signal_evaluated": False,
            "risk_reason": None,
            "action": None,
            "action_reason": None,
            "ema_fast": None,
            "ema_slow": None,
            "ema_mid": None,
            "skip_reason": None,
            "errors": [f"{e['timestamp']} {e['source']}: {e['message']}" for e in self._errors[-10:]],
        }
        return row

    def _trading_row(
        self,
        snap: dict,
        sig: dict | None = None,
        action: dict | None = None,
        skip_reason: str | None = None,
        indicators: dict | None = None,
    ) -> dict:
        pos = self.paper.position
        row = {
            "symbol": self.symbol,
            **self._market_telemetry(snap),
            **self._runtime_telemetry(),
            "paper_balance": round(self.paper.balance, 2),
            "open_position": pos["side"] if pos else None,
            "entry_price": pos["entry_price"] if pos else None,
            "current_price": pos["current_price"] if pos else None,
            "unrealized_pnl": round(self.paper.unrealized_pnl, 2),
            "realized_pnl": round(self.paper.realized_pnl, 2),
            "fees": round(pos["fees"], 4) if pos else 0.0,
            "slippage_bps": pos["slippage"] if pos else 0.0,
            "trade_count": self._closed_trade_count(),
            "action": None,
            "action_reason": None,
            "signal_evaluated": sig is not None,
            "risk_reason": sig.get("risk_reason") if sig is not None else None,
            "ema_fast": indicators.get("ema_fast") if indicators else None,
            "ema_slow": indicators.get("ema_slow") if indicators else None,
            "ema_mid": indicators.get("ema_mid") if indicators else None,
            "errors": [f"{e['timestamp']} {e['source']}: {e['message']}" for e in self._errors[-10:]],
            "skip_reason": skip_reason,
        }
        if sig is not None:
            row.update(
                {
                    "signal_direction": sig.get("direction"),
                    "signal_score": sig.get("score"),
                    "signal_confidence": sig.get("confidence"),
                    "risk_allowed": bool(sig.get("risk_allowed")),
                }
            )
        if action is not None:
            row["action"] = str(action.get("action") if isinstance(action, dict) else action)
            row["action_reason"] = action.get("reason") if isinstance(action, dict) else None
        return row

    def _emit_telemetry(
        self,
        snap: dict,
        sig: dict | None = None,
        action: dict | None = None,
        skip_reason: str | None = None,
        indicators: dict | None = None,
    ) -> None:
        trading = self._trading_row(snap, sig=sig, action=action, skip_reason=skip_reason, indicators=indicators)
        for symbol in self.watchlist:
            if symbol == self.symbol:
                self.telemetry.add(trading)
            else:
                feed = self.feeds.get(symbol)
                if feed is None:
                    continue
                row = self._feed_row(feed, symbol)
                row["paper_balance"] = trading["paper_balance"]
                row["open_position"] = None
                row["entry_price"] = None
                row["current_price"] = None
                row["unrealized_pnl"] = trading["unrealized_pnl"]
                row["realized_pnl"] = trading["realized_pnl"]
                row["fees"] = 0.0
                row["slippage_bps"] = 0.0
                row["trade_count"] = trading["trade_count"]
                self.telemetry.add(row)

    # ------------------------------------------------------------------- cycle

    def cycle(self) -> dict:
        """One synchronous soak cycle.

        Order matters:
        1. risk equity mirrors the paper account (simulated money),
        2. an open position is managed against the newest candle first
           (mark-to-market + stop/target; never a new trade),
        3. the startup seeding guard prevents any auto-trade right after
           a restart,
        4. only on a *new*, *streaming*, *price-valid* candle is a Strategy A
           decision generated; nothing else can open or close a trade.
        """
        self.cycle_number += 1
        self._cycle_started_mono = time.monotonic()
        feed = self.feeds[self.symbol]
        snap = feed.snapshot()
        self._sync_risk_equity()

        if self.paper.position is not None and snap["data_status"] != "unavailable" and snap["price_valid"]:
            result = self.paper.on_candle(snap["candles"][-1])
            if result is not None and "pnl" in result:
                self._sync_risk_equity()

        if not snap["candles"]:
            self._emit_telemetry(snap, skip_reason="no candles")
            return {"action": "noop", "reason": "no candles"}

        last_time = int(snap["candles"][-1]["time"])
        if self.processed_time is None:
            self.processed_time = last_time
            self._emit_telemetry(snap, skip_reason="seeded on startup (no auto trade)")
            return {"action": "seeded", "reason": "startup seeding, no auto trade"}
        if last_time == self.processed_time:
            self._emit_telemetry(snap, skip_reason="no new candle")
            return {"action": "noop", "reason": "no new candle"}
        self.processed_time = last_time

        if snap["data_status"] != "streaming":
            self._emit_telemetry(snap, skip_reason=f"data_status={snap['data_status']}")
            return {"action": "noop", "reason": f"data_status={snap['data_status']}, no trade"}
        if not snap["price_valid"]:
            self._emit_telemetry(snap, skip_reason="price invalid")
            return {"action": "noop", "reason": "price invalid, no trade"}

        indicators: dict | None = None
        try:
            df = self.feature_pipeline.transform(pd.DataFrame(snap["candles"]))
            last_features = df.iloc[-1]
            indicators = {
                "ema_fast": float(last_features["ema_fast"]),
                "ema_slow": float(last_features["ema_slow"]),
                "ema_mid": float(last_features["ema_mid"]),
            }
            sig = self.engine.analyze_candle(df, ticker=feed.ticker or {})
        except Exception as e:  # unexpected failure: never silently swallowed
            logger.exception("soak: signal generation failed for %s: %s", self.symbol, e)
            self._push_error("signal", str(e))
            self._emit_telemetry(snap, skip_reason=f"signal error: {e}")
            return {"action": "noop", "reason": f"signal error: {e}"}

        sig["time"] = last_time
        sig["symbol"] = self.symbol
        sig["timeframe"] = self.profile.timeframe
        sig["strategy_id"] = self.profile.strategy_id
        sig["strategy_version"] = self.profile.strategy_version
        sig["data_quality"] = data_quality_from_snapshot(snap, profile=self.profile).model_dump()
        try:
            validated = validate_signal(
                sig,
                profile=self.profile,
                candle_time=last_time,
                data_quality=sig["data_quality"],
            )
        except SignalValidationError as exc:
            risk_reason = sig.get("risk_reason") or str(exc)
            sig["risk_allowed"] = False
            sig["risk_reason"] = risk_reason
            action = {"action": "rejected", "reason": str(exc)}
            self.last_action = action
            self._persist_signal(sig)
            self._emit_telemetry(snap, sig=sig, action=action, indicators=indicators)
            return {"action": action, "signal": sig}

        sig = validated.model_dump()
        action = self.paper.on_signal(validated)
        if isinstance(action, dict) and action.get("action") == "rejected":
            sig["risk_reason"] = action.get("reason") or sig.get("risk_reason")
        self.last_action = action
        self._sync_risk_equity()
        self._persist_signal(sig)
        self._emit_telemetry(snap, sig=sig, action=action, indicators=indicators)
        return {"action": action, "signal": sig}

    # -------------------------------------------------------------------- loop

    def stop(self) -> None:
        self._running = False

    async def _stop_feeds(self) -> None:
        try:
            await asyncio.gather(*[f.stop() for f in self.feeds.values()], return_exceptions=True)
        except Exception:
            logger.exception("soak: error while stopping feeds")

    async def run(self, duration: float | None = None) -> None:
        """Run the soak loop forever (duration=None) or for `duration` seconds.

        Safe to cancel (e.g. Ctrl+C): the finally block stops every feed task
        and flushes telemetry, so a kill never leaks sockets or data.
        """
        self.requested_duration_secs = duration
        for f in self.feeds.values():
            await f.start()
            try:
                await asyncio.to_thread(f.poll)  # warm up without blocking WS/event-loop work
            except Exception as e:
                logger.warning("soak: initial poll for %s failed: %s", f.symbol, e)

        self._running = True
        self.start_time = time.monotonic()
        next_tick = self.start_time
        try:
            while self._running:
                try:
                    self.cycle()
                    self.cycles_run += 1
                except Exception as e:
                    logger.exception("soak: cycle failed: %s", e)
                    self._push_error("cycle", str(e))
                self.telemetry.flush()
                if duration is not None and time.monotonic() - self.start_time >= duration:
                    break
                next_tick += self.tick_secs
                await asyncio.sleep(max(0.0, next_tick - time.monotonic()))
        finally:
            self._running = False
            await self._stop_feeds()
            self.telemetry.flush()

    # --------------------------------------------------------------- reporting

    def summary_text(self) -> str:
        feed = self.feeds[self.symbol]
        snap = feed.snapshot()
        return "\n".join(
            [
                "soak runner summary",
                f"  symbols    : {', '.join(self.watchlist)}",
                f"  trading    : {self.symbol}  EMA {'/'.join(str(e) for e in self.ema_spans)}  1h",
                f"  data       : status={snap['data_status']} source={snap['data_source']} "
                + (f"age={snap['last_candle_age_secs']}s" if snap["last_candle_age_secs"] is not None else "n/a"),
                f"  paper      : balance INR {self.paper.balance:,.2f}  realized INR {self.paper.realized_pnl:,.2f}",
                "  position   : " + (f"{self.paper.position['side']} @ {self.paper.position['entry_price']}" if self.paper.position else "flat"),
                f"  trades     : {self._closed_trade_count()} closed",
                f"  telemetry  : {self.telemetry.rows_written} rows written -> {self.telemetry.out_path or '(memory)'}",
                f"  cycles     : {self.cycles_run}",
            ]
        )
