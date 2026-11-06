"""FastAPI backend: runs the paper pipeline and exposes read/control APIs.

The signal loop is started on app startup (lifespan) and torn down on close.
Market/data/analysis endpoints are read-only. Watchlist mutations and kill
switch control require ADMIN_TOKEN; order placement remains behind the
execution guard and is disabled for live trading by default.
"""

import asyncio
import json
import logging
import secrets
import time
from collections import deque
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.analysis.config import (
    DEFAULT_FUND_INR,
    DEFAULT_RISK_PCT,
    DEFAULT_SYMBOL,
    DEFAULT_WATCHLIST,
)
from app.analysis.service import AnalysisService
from app.analysis.strategies import DEFAULT_STRATEGY_ID, list_strategies
from app.config.settings import get_settings
from app.config.trading import profile_from_settings
from app.data.market_data import MarketData
from app.data.market_health import MarketHealthMonitor
from app.database.database import SessionLocal, get_db, init_db
from app.database.models import (
    Alert,
    AuditEvent,
    EquitySnapshot,
    Fill,
    Order,
    OrderEvent,
    PaperTrade,
    RiskDecisionRecord,
    Signal,
    StrategyVersion,
    SystemEvent,
    TestnetExecution,
)
from app.instruments.registry import InstrumentRegistry
from app.instruments.watchlist import WatchlistService
from app.observability import snapshot_equity
from app.research.governance import GovernanceError, GovernanceService
from app.research.models import ResearchProposal, ResearchPromotion, ResearchWalkForwardFold
from app.research.store import ResearchStorageError, ResearchStore
from app.risk.kill_switch import KillSwitch
from app.risk.risk_manager import RiskManager
from app.strategy.signal_engine import SignalEngine
from app.trading.paper_trader import PaperTrader
from app.trading.runner import TradingRunner

logger = logging.getLogger(__name__)
settings = get_settings()

state: dict = {}


class WatchlistPayload(BaseModel):
    instrument_id: str | None = None
    symbol: str | None = None


class KillSwitchPayload(BaseModel):
    reason: str = ""


class BotControlPayload(BaseModel):
    reason: str = ""


class ProposalPayload(BaseModel):
    strategy_id: str
    strategy_version: str
    experiment_id: str = ""
    dataset_hash: str = ""
    artifact_path: str = ""
    params: dict = {}
    metrics: dict = {}
    proposal_id: str | None = None


class ReviewPayload(BaseModel):
    reviewer: str
    veto: bool = False
    approved: bool = False
    findings: list[dict] = []
    required_changes: list[str] = []


class ApprovalPayload(BaseModel):
    approver: str
    rationale: str


class CanaryStartPayload(BaseModel):
    duration_seconds: int
    min_trades: int | None = None


class CanaryRecordPayload(BaseModel):
    trades: int
    risk_breaches: int = 0
    reconciliation_ok: bool = True
    alerts_ok: bool = True


class PromotionPayload(BaseModel):
    approver: str


class RollbackPayload(BaseModel):
    actor: str
    reason: str


def _research_store() -> ResearchStore:
    store = state.get("research_store")
    if store is None:
        store = ResearchStore()
        state["research_store"] = store
    return store


def _governance() -> GovernanceService:
    service = state.get("governance")
    if service is None:
        service = GovernanceService(_research_store())
        state["governance"] = service
    return service


def _proposal_dict(row: ResearchProposal) -> dict:
    return {
        "proposal_id": row.proposal_id,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "experiment_id": row.experiment_id,
        "strategy_id": row.strategy_id,
        "strategy_version": row.strategy_version,
        "dataset_hash": row.dataset_hash,
        "artifact_path": row.artifact_path,
        "params": json.loads(row.params_json or "{}"),
        "metrics": json.loads(row.metrics_json or "{}"),
        "status": row.status,
        "rejection_reason": row.rejection_reason,
    }


def _audit_research(event_type: str, actor: str, payload: dict) -> None:
    try:
        with SessionLocal() as db:
            db.add(
                AuditEvent(
                    event_type=event_type,
                    actor=str(actor or "admin"),
                    correlation_id=str(payload.get("proposal_id") or payload.get("strategy_id") or ""),
                    severity="info",
                    payload=json.dumps(payload, default=str, sort_keys=True),
                )
            )
            db.commit()
    except Exception:
        logger.exception("research audit persistence failed")


def require_admin(x_admin_token: str | None = Header(default=None, alias="X-Admin-Token")) -> None:
    expected = settings.admin_token or ""
    if not expected:
        raise HTTPException(status_code=403, detail="admin token is not configured")
    if not x_admin_token or not secrets.compare_digest(x_admin_token, expected):
        raise HTTPException(status_code=403, detail="invalid admin token")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.validate_startup()
    init_db()
    market = MarketData(candle_poll_secs=45, candle_buffer=60)
    monitor = MarketHealthMonitor(symbols=list(DEFAULT_WATCHLIST), poll_secs=45)
    # Paper account defaults follow the Phase-6 presets: ₹5,000 fund, 0.5% risk.
    # This is SIMULATED money - the risk/paper engines only ever act on it.
    profile = profile_from_settings(settings, mode="paper")
    kill_switch = KillSwitch(SessionLocal)
    risk = RiskManager(
        starting_equity=settings.paper_fund_inr,
        settings=profile.risk,
        session_factory=SessionLocal,
        account_key="paper",
        kill_switch=kill_switch,
    )
    paper = PaperTrader(
        starting_equity=settings.paper_fund_inr,
        session_factory=SessionLocal,
        fee_pct=settings.paper_fee_pct,
        slippage_pct=settings.paper_slippage_pct,
        symbol=profile.symbol,
        contract_value=profile.contract_value,
        profile=profile,
        risk_manager=risk,
    )
    reconcile = paper.reconcile()
    if not reconcile["ok"]:
        logger.error("paper/risk reconciliation failed: %s", reconcile)
        risk.halt(f"persistence mismatch on startup: {reconcile}")
    risk.sync_from_paper(paper)
    try:
        with SessionLocal() as db:
            snapshot_equity(
                db,
                account_key="paper",
                equity=paper.total_equity,
                balance=paper.balance,
                realized_pnl=paper.realized_pnl,
                unrealized_pnl=paper.unrealized_pnl,
                peak_equity=risk.peak_equity,
                source="paper",
            )
    except Exception:
        logger.exception("failed to record initial paper equity snapshot")
    engine = SignalEngine(
        weights=profile.weights,
        risk_manager=risk,
        decision_threshold=profile.decision_threshold,
        contract_value=profile.contract_value,
    )
    runner = TradingRunner(
        market=market,
        engine=engine,
        risk_manager=risk,
        paper_trader=paper,
        profile=profile,
        kill_switch=kill_switch,
        session_factory=SessionLocal,
    )

    market_task = asyncio.create_task(market.start())
    monitor_task = asyncio.create_task(monitor.run())
    runner_task = asyncio.create_task(runner.run())
    state.update(
        market=market,
        monitor=monitor,
        risk=risk,
        engine=engine,
        paper=paper,
        runner=runner,
        profile=profile,
        kill_switch=kill_switch,
    )
    logger.info("pipeline started (live_mode=%s, paper_fund=%s)", settings.live_mode, settings.paper_fund_inr)
    yield
    runner.stop()
    monitor.stop()
    market_task.cancel()
    monitor_task.cancel()
    runner_task.cancel()
    await asyncio.gather(market_task, monitor_task, runner_task, return_exceptions=True)


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type", "X-Admin-Token"],
)


_rate_buckets: dict[str, deque[float]] = {}
_RATE_WINDOW_SECONDS = 60.0
_HEALTH_PATHS = {"/health", "/metrics"}


def reset_rate_limits() -> None:
    """Clear the in-process limiter (tests and operator recovery)."""
    _rate_buckets.clear()


def _client_key(request: Request) -> str:
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip() or "unknown"
    return request.client.host if request.client else "unknown"


def _rate_limited(key: str, now: float) -> bool:
    bucket = _rate_buckets.get(key)
    if bucket is None:
        bucket = deque()
        _rate_buckets[key] = bucket
    while bucket and now - bucket[0] >= _RATE_WINDOW_SECONDS:
        bucket.popleft()
    if len(bucket) >= settings.rate_limit_per_minute:
        return True
    bucket.append(now)
    return False


def _with_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


@app.middleware("http")
async def safety_middleware(request: Request, call_next):
    if request.url.path not in _HEALTH_PATHS:
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                length = int(content_length)
            except ValueError:
                return _with_security_headers(JSONResponse({"detail": "invalid content-length"}, status_code=400))
            if length > settings.max_request_bytes:
                return _with_security_headers(JSONResponse({"detail": "request body too large"}, status_code=413))
        if _rate_limited(_client_key(request), time.monotonic()):
            return _with_security_headers(
                JSONResponse(
                    {"detail": "rate limit exceeded"},
                    status_code=429,
                    headers={"Retry-After": "60"},
                )
            )
    response = await call_next(request)
    return _with_security_headers(response)


@app.get("/health")
def health() -> dict:
    market = state.get("market")
    snap = market.snapshot() if market else None
    monitor = state.get("monitor")
    paper = state.get("paper")
    mon_snap = monitor.snapshot() if monitor else None
    return {
        "status": "ok",
        "ready": bool(market and snap and snap["data_status"] == "streaming"),
        "symbol": settings.trading_symbol,
        "live_mode": settings.live_mode,
        "data_status": snap.get("data_status") if snap else None,
        "candles_count": snap.get("candles_count") if snap else 0,
        "data_source": snap.get("data_source") if snap else None,
        "price_valid": snap.get("price_valid") if snap else None,
        "last_candle_age_secs": snap.get("last_candle_age_secs") if snap else None,
        "ws_status": snap.get("ws_status") if snap else None,
        "monitor": {"overall": mon_snap["overall"] if mon_snap else None, "covered_symbols": len(monitor.symbols) if monitor else 0},
        "paper": {
            "mode": "paper",
            "persisted": bool(paper and paper.session_factory),
            "balance": paper.balance if paper else None,
            "position_open": bool(paper and paper.position),
        },
    }


@app.get("/monitor")
def monitor() -> dict:
    mon = state.get("monitor")
    if mon is None:
        return {"overall": None, "symbols": [], "error": "monitor not running"}
    return mon.snapshot()


@app.get("/safety")
def safety() -> dict:
    profile = state.get("profile")
    kill_switch = state.get("kill_switch") or KillSwitch(SessionLocal)
    return {
        "live_mode": settings.live_mode,
        "testnet_mode": settings.testnet_mode,
        "real_order_confirmation": settings.real_order_confirmation,
        "execution_allowed": settings.execution_allowed,
        "api_keys_configured": bool(settings.delta_api_key and settings.delta_api_secret),
        "delta_base_url": settings.delta_base_url,
        "backend_host": settings.backend_host,
        "admin_auth_configured": bool(settings.admin_token),
        "profile": profile.as_dict() if profile else None,
        "kill_switch": kill_switch.status(),
    }


@app.get("/kill-switch")
def kill_switch_status() -> dict:
    kill_switch = state.get("kill_switch") or KillSwitch(SessionLocal)
    return kill_switch.status()


@app.get("/bot/status")
def bot_status() -> dict:
    runner = state.get("runner")
    if runner is None:
        return {"status": "starting", "running": False, "paused": False, "mode": "paper"}
    return {
        **runner.status(),
        "risk_halted": bool(state.get("risk") and state["risk"].trading_halted),
        "kill_switch_halted": bool((state.get("kill_switch") or KillSwitch(SessionLocal)).is_halted()),
        "live_trading_enabled": False,
    }


@app.post("/bot/pause", dependencies=[Depends(require_admin)])
def bot_pause(payload: BotControlPayload) -> dict:
    runner = state.get("runner")
    if runner is None:
        raise HTTPException(status_code=503, detail="bot runner is not ready")
    runner.pause()
    with SessionLocal() as db:
        from app.observability import record_audit, record_system_event
        reason = payload.reason.strip() or "paused via API"
        record_audit(db, "bot_paused", actor="api", severity="warning", payload={"reason": reason})
        record_system_event(db, "bot_paused", reason, severity="warning")
    return bot_status()


@app.post("/bot/start", dependencies=[Depends(require_admin)])
def bot_start(payload: BotControlPayload) -> dict:
    runner = state.get("runner")
    if runner is None:
        raise HTTPException(status_code=503, detail="bot runner is not ready")
    rm = state.get("risk")
    kill_switch = state.get("kill_switch") or KillSwitch(SessionLocal)
    if (rm is not None and rm.trading_halted) or kill_switch.is_halted():
        raise HTTPException(status_code=409, detail="risk or kill switch is halted; resolve safety state first")
    runner.resume()
    with SessionLocal() as db:
        from app.observability import record_audit, record_system_event
        reason = payload.reason.strip() or "started via API"
        record_audit(db, "bot_started", actor="api", payload={"reason": reason})
        record_system_event(db, "bot_started", reason)
    return bot_status()


@app.post("/bot/emergency-halt", dependencies=[Depends(require_admin)])
def bot_emergency_halt(payload: BotControlPayload) -> dict:
    runner = state.get("runner")
    if runner is not None:
        runner.pause()
    kill_switch = state.get("kill_switch") or KillSwitch(SessionLocal)
    reason = payload.reason.strip() or "emergency halt via API"
    out = kill_switch.halt(reason, actor="api")
    rm = state.get("risk")
    if rm is not None:
        rm.halt(reason)
    return {"status": "halted", "reason": reason, "kill_switch": out, **bot_status()}


@app.post("/kill-switch/halt", dependencies=[Depends(require_admin)])
def kill_switch_halt(payload: KillSwitchPayload) -> dict:
    kill_switch = state.get("kill_switch") or KillSwitch(SessionLocal)
    reason = payload.reason.strip() or "halted via API"
    out = kill_switch.halt(reason, actor="api")
    rm = state.get("risk")
    if rm is not None:
        rm.halt(reason)
    return out


@app.post("/kill-switch/resume", dependencies=[Depends(require_admin)])
def kill_switch_resume() -> dict:
    kill_switch = state.get("kill_switch") or KillSwitch(SessionLocal)
    out = kill_switch.resume(actor="api")
    rm = state.get("risk")
    if rm is not None:
        rm.resume()
    return out


@app.get("/ticker")
def ticker() -> dict:
    return state.get("market", MarketData(settings=settings)).snapshot()


@app.get("/decision")
def decision() -> dict:
    if "runner" not in state:
        return {"direction": "none", "reason": "not running"}
    sig = state["runner"].last_signal
    if not sig:
        return {"direction": "n/a", "reason": "awaiting first closed candle"}
    return sig


@app.get("/paper")
def paper() -> dict:
    pt = state.get("paper")
    if pt is None:
        return {"mode": "paper", "error": "paper engine not running", "fund_separate": True}
    out = pt.extended_snapshot()
    out["mode"] = "paper"
    out["note"] = "SIMULATED MONEY - never the real Trading Fund, no exchange orders"
    return out


@app.get("/paper/account")
def paper_account() -> dict:
    pt = state.get("paper")
    if pt is None:
        return {"error": "paper engine not running"}
    return {
        "starting_balance": pt.starting_equity,
        "balance": pt.balance,
        "realized_pnl": pt.realized_pnl,
        "unrealized_pnl": pt.unrealized_pnl,
        "total_equity": pt.total_equity,
        "currency": "INR",
        "fee_pct": pt.fee_pct,
        "slippage_pct": pt.slippage_pct,
        "fund_separate": True,
        "note": "SIMULATED MONEY - separate from the selected Trading Fund",
    }


@app.get("/paper/position")
def paper_position() -> dict:
    pt = state.get("paper")
    if pt is None:
        return {"position": None, "error": "paper engine not running"}
    return {"position": pt.position, "unrealized_pnl": pt.unrealized_pnl}


@app.get("/risk")
def risk() -> dict:
    rm = state["risk"]
    kill_switch = state.get("kill_switch") or rm.kill_switch
    return {
        "equity": rm.current_equity,
        "realized_pnl": rm.realized_pnl,
        "day_pnl": rm.day_pnl,
        "peak_equity": rm.peak_equity,
        "open_positions": rm.open_positions,
        "trades_today": rm.trades_today,
        "daily_loss_limit": rm.settings.max_daily_loss_pct,
        "max_daily_trades": rm.settings.max_daily_trades,
        "trading_halted": rm.trading_halted,
        "kill_switch_halted": kill_switch.is_halted() if kill_switch else None,
    }


@app.get("/metrics")
def metrics(db: Session = Depends(get_db)) -> dict:
    """Operational metrics for the dashboard and health checks."""
    trade_count = db.execute(select(func.count()).select_from(PaperTrade)).scalar_one()
    signal_count = db.execute(select(func.count()).select_from(Signal)).scalar_one()
    execution_count = db.execute(select(func.count()).select_from(TestnetExecution)).scalar_one()
    alert_count = db.execute(select(func.count()).select_from(Alert).where(Alert.active.is_(True))).scalar_one()
    latest_equity = db.execute(select(EquitySnapshot).order_by(EquitySnapshot.created_at.desc()).limit(1)).scalar_one_or_none()
    order_rows = db.execute(select(Order.status, func.count()).group_by(Order.status)).all()
    order_counts = {str(status): int(count) for status, count in order_rows}
    rejected_orders = order_counts.get("rejected", 0)
    latest_signal = db.execute(select(Signal).order_by(Signal.created_at.desc()).limit(1)).scalar_one_or_none()
    runner = state.get("runner")
    bot = state.get("bot")
    uptime = None
    if bot is not None and getattr(bot, "_started_at", None):
        uptime = max(0.0, time.time() - bot._started_at.timestamp())
    return {
        "paper_trades": trade_count,
        "signals": signal_count,
        "testnet_executions": execution_count,
        "active_alerts": alert_count,
        "latest_equity": latest_equity.equity if latest_equity else None,
        "latest_drawdown_pct": latest_equity.drawdown_pct if latest_equity else None,
        "orders": order_counts,
        "rejected_orders": rejected_orders,
        "last_signal_at": latest_signal.created_at if latest_signal else None,
        "runner_status": runner.status() if runner is not None else None,
        "uptime_seconds": uptime,
        "live_trading_enabled": False,
    }


@app.get("/alerts")
def alerts(limit: int = 50, db: Session = Depends(get_db)) -> list[dict]:
    rows = db.execute(select(Alert).order_by(Alert.created_at.desc()).limit(max(1, min(limit, 200)))).scalars().all()
    return [{
        "id": row.id,
        "dedupe_key": row.dedupe_key,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "severity": row.severity,
        "title": row.title,
        "message": row.message,
        "active": row.active,
        "delivered": row.delivered,
    } for row in rows]


@app.get("/audit")
def audit(limit: int = 100, db: Session = Depends(get_db)) -> list[dict]:
    rows = db.execute(select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(max(1, min(limit, 500)))).scalars().all()
    return [{
        "id": row.id,
        "created_at": row.created_at,
        "event_type": row.event_type,
        "actor": row.actor,
        "severity": row.severity,
        "correlation_id": row.correlation_id,
        "payload": row.payload,
    } for row in rows]


@app.get("/system-events")
def system_events(limit: int = 100, db: Session = Depends(get_db)) -> list[dict]:
    rows = db.execute(select(SystemEvent).order_by(SystemEvent.created_at.desc()).limit(max(1, min(limit, 500)))).scalars().all()
    return [{
        "id": row.id,
        "created_at": row.created_at,
        "event_type": row.event_type,
        "severity": row.severity,
        "message": row.message,
        "payload": row.payload,
    } for row in rows]


@app.get("/equity")
def equity(limit: int = 200, db: Session = Depends(get_db)) -> list[dict]:
    rows = db.execute(select(EquitySnapshot).order_by(EquitySnapshot.created_at.desc()).limit(max(1, min(limit, 1000)))).scalars().all()
    return [{
        "created_at": row.created_at,
        "account_key": row.account_key,
        "equity": row.equity,
        "balance": row.balance,
        "realized_pnl": row.realized_pnl,
        "unrealized_pnl": row.unrealized_pnl,
        "drawdown_pct": row.drawdown_pct,
        "source": row.source,
    } for row in reversed(rows)]


@app.get("/signals")
def signals(limit: int = 20, db: Session = Depends(get_db)) -> list[dict]:
    rows = db.execute(select(Signal).order_by(Signal.candle_time.desc()).limit(limit)).scalars().all()
    return [
        {
            "time": r.candle_time,
            "direction": r.direction,
            "score": r.score,
            "entry": r.entry_price,
            "stop": r.stop_loss,
            "target": r.target,
            "strategy_id": r.strategy_id,
            "strategy_version": r.strategy_version,
            "risk_reason": r.risk_reason,
            "created_at": r.created_at,
        }
        for r in rows
    ]


@app.get("/signals/stats")
def signal_stats(db: Session = Depends(get_db)) -> dict:
    total = db.execute(select(func.count()).select_from(Signal)).scalar_one()
    return {"total_signals": total}


@app.get("/trades")
def trades(limit: int = 20, db: Session = Depends(get_db)) -> list[dict]:
    rows = db.execute(select(PaperTrade).order_by(PaperTrade.id.desc()).limit(limit)).scalars().all()
    return [
        {
            "id": t.id,
            "side": t.side,
            "size": t.size,
            "entry": t.entry_price,
            "exit": t.exit_price,
            "pnl": t.pnl,
            "status": t.status,
            "exit_reason": t.exit_reason,
            "fees": t.fees,
            "slippage": t.slippage,
            "notional": t.notional,
            "leverage": t.leverage,
        }
        for t in rows
    ]


@app.get("/executions")
def executions(limit: int = 20, db: Session = Depends(get_db)) -> list[dict]:
    rows = db.execute(select(TestnetExecution).order_by(TestnetExecution.id.desc()).limit(limit)).scalars().all()
    return [
        {
            "id": x.id,
            "created_at": x.created_at,
            "client_order_id": x.client_order_id,
            "symbol": x.symbol,
            "side": x.side,
            "size": x.size,
            "expected_entry": x.expected_entry,
            "actual_entry": x.actual_entry,
            "slippage": x.slippage,
            "slippage_bps": x.slippage_bps,
            "expected_sl": x.expected_sl,
            "actual_sl": x.actual_sl,
            "expected_tp": x.expected_tp,
            "actual_tp": x.actual_tp,
            "fees": x.fees,
            "latency_ms": x.latency_ms,
            "order_status": x.order_status,
            "position_status": x.position_status,
            "final_pnl": x.final_pnl,
            "exit_reason": x.exit_reason,
        }
        for x in rows
    ]


@app.get("/orders")
def orders(limit: int = 50, status: str | None = None, db: Session = Depends(get_db)) -> list[dict]:
    query = select(Order)
    if status:
        query = query.where(Order.status == status)
    query = query.order_by(Order.id.desc()).limit(max(1, min(limit, 500)))
    rows = db.execute(query).scalars().all()
    return [
        {
            "id": row.id,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "client_order_id": row.client_order_id,
            "signal_key": row.signal_key,
            "venue": row.venue,
            "execution_mode": row.execution_mode,
            "symbol": row.symbol,
            "side": row.side,
            "size": row.size,
            "filled_size": row.filled_size,
            "expected_price": row.expected_price,
            "avg_fill_price": row.avg_fill_price,
            "fees": row.fees,
            "slippage": row.slippage,
            "status": row.status,
            "reason": row.reason,
            "strategy_id": row.strategy_id,
            "strategy_version": row.strategy_version,
        }
        for row in rows
    ]


@app.get("/orders/{client_order_id}")
def order_detail(client_order_id: str, db: Session = Depends(get_db)) -> dict:
    row = db.execute(select(Order).where(Order.client_order_id == client_order_id)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="order not found")
    fills = db.execute(select(Fill).where(Fill.client_order_id == client_order_id).order_by(Fill.id)).scalars().all()
    events = db.execute(select(OrderEvent).where(OrderEvent.client_order_id == client_order_id).order_by(OrderEvent.id)).scalars().all()
    return {
        "order": {
            "client_order_id": row.client_order_id,
            "status": row.status,
            "symbol": row.symbol,
            "side": row.side,
            "size": row.size,
            "filled_size": row.filled_size,
            "avg_fill_price": row.avg_fill_price,
            "fees": row.fees,
            "slippage": row.slippage,
            "venue": row.venue,
            "execution_mode": row.execution_mode,
            "risk_decision_key": row.risk_decision_key,
            "reason": row.reason,
        },
        "fills": [
            {
                "fill_key": fill.fill_key,
                "price": fill.price,
                "size": fill.size,
                "fee": fill.fee,
                "slippage": fill.slippage,
                "venue": fill.venue,
                "created_at": fill.created_at,
            }
            for fill in fills
        ],
        "events": [
            {
                "from_state": event.from_state,
                "to_state": event.to_state,
                "event_type": event.event_type,
                "created_at": event.created_at,
                "payload": event.payload,
            }
            for event in events
        ],
    }


@app.get("/risk-decisions")
def risk_decisions(limit: int = 50, db: Session = Depends(get_db)) -> list[dict]:
    rows = db.execute(
        select(RiskDecisionRecord).order_by(RiskDecisionRecord.id.desc()).limit(max(1, min(limit, 500)))
    ).scalars().all()
    return [
        {
            "id": row.id,
            "created_at": row.created_at,
            "signal_key": row.signal_key,
            "execution_mode": row.execution_mode,
            "allowed": row.allowed,
            "reason": row.reason,
            "size": row.size,
            "equity": row.equity,
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Research and governance. Reads are public; every mutation requires the
# admin token. Nothing here places orders, changes risk limits or promotes a
# strategy without stored gates and explicit human approval.
# ---------------------------------------------------------------------------


@app.get("/research/experiments")
def research_experiments(limit: int = 50) -> list[dict]:
    rows = _research_store().list_experiments(limit=max(1, min(limit, 500)))
    return [
        {
            "experiment_id": row.experiment_id,
            "created_at": row.created_at,
            "validated_at": row.validated_at,
            "strategy_id": row.strategy_id,
            "strategy_version": row.strategy_version,
            "symbol": row.symbol,
            "timeframe": row.timeframe,
            "dataset_hash": row.dataset_hash,
            "status": row.status,
            "metrics": json.loads(row.metrics_json or "{}"),
        }
        for row in rows
    ]


@app.get("/research/walk-forward")
def research_walk_forward(limit: int = 50) -> list[dict]:
    store = _research_store()
    runs = store.list_walk_forward_runs(limit=max(1, min(limit, 200)))
    rows: list[dict] = []
    with store.SessionLocal() as db:
        for run in runs:
            fold_count = db.execute(
                select(func.count())
                .select_from(ResearchWalkForwardFold)
                .where(ResearchWalkForwardFold.run_id == run.run_id)
            ).scalar_one()
            rows.append(
                {
                    "run_id": run.run_id,
                    "experiment_id": run.experiment_id,
                    "strategy_id": run.strategy_id,
                    "dataset_hash": run.dataset_hash,
                    "artifact_path": run.artifact_path,
                    "command": run.command,
                    "code_version": run.code_version,
                    "created_at": run.created_at,
                    "config": json.loads(run.config_json or "{}"),
                    "aggregate": json.loads(run.aggregate_json or "{}"),
                    "folds": int(fold_count),
                }
            )
    return rows


@app.get("/research/proposals")
def research_proposals(limit: int = 50) -> list[dict]:
    store = _research_store()
    with store.SessionLocal() as db:
        rows = db.execute(
            select(ResearchProposal).order_by(ResearchProposal.id.desc()).limit(max(1, min(limit, 500)))
        ).scalars().all()
        return [_proposal_dict(row) for row in rows]


@app.get("/research/proposals/{proposal_id}")
def research_proposal(proposal_id: str) -> dict:
    row = _governance().get_proposal(proposal_id)
    if row is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    return _proposal_dict(row)


@app.get("/research/proposals/{proposal_id}/gates")
def research_proposal_gates(proposal_id: str) -> dict:
    try:
        return _governance().gates(proposal_id)
    except GovernanceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/research/strategies")
def research_strategies(db: Session = Depends(get_db)) -> list[dict]:
    rows = db.execute(select(StrategyVersion).order_by(StrategyVersion.id.desc())).scalars().all()
    return [
        {
            "strategy_id": row.strategy_id,
            "version": row.version,
            "status": row.status,
            "active": row.active,
            "dataset_hash": row.dataset_hash,
            "artifact_path": row.artifact_path,
            "validation_run_id": row.validation_run_id,
            "promoted_at": row.promoted_at,
        }
        for row in rows
    ]


@app.get("/research/promotions")
def research_promotions(limit: int = 50) -> list[dict]:
    store = _research_store()
    with store.SessionLocal() as db:
        rows = db.execute(
            select(ResearchPromotion).order_by(ResearchPromotion.id.desc()).limit(max(1, min(limit, 500)))
        ).scalars().all()
        return [
            {
                "proposal_id": row.proposal_id,
                "strategy_id": row.strategy_id,
                "strategy_version": row.strategy_version,
                "previous_version": row.previous_version,
                "promoted_at": row.promoted_at,
                "promoted_by": row.promoted_by,
                "rolled_back_at": row.rolled_back_at,
                "rollback_reason": row.rollback_reason,
                "status": row.status,
            }
            for row in rows
        ]


@app.post("/research/proposals", dependencies=[Depends(require_admin)])
def research_create_proposal(payload: ProposalPayload) -> dict:
    try:
        row = _governance().create_proposal(
            strategy_id=payload.strategy_id,
            strategy_version=payload.strategy_version,
            experiment_id=payload.experiment_id,
            dataset_hash=payload.dataset_hash,
            artifact_path=payload.artifact_path,
            params=payload.params,
            metrics=payload.metrics,
            proposal_id=payload.proposal_id,
        )
    except (GovernanceError, ResearchStorageError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit_research("research_proposal_created", "admin", {"proposal_id": row.proposal_id, "strategy_id": row.strategy_id})
    return _proposal_dict(row)


@app.post("/research/proposals/{proposal_id}/reviews", dependencies=[Depends(require_admin)])
def research_add_review(proposal_id: str, payload: ReviewPayload) -> dict:
    try:
        row = _governance().add_review(
            proposal_id,
            reviewer=payload.reviewer,
            veto=payload.veto,
            approved=payload.approved,
            findings=payload.findings,
            required_changes=payload.required_changes,
        )
    except GovernanceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _audit_research("research_review_recorded", payload.reviewer, {"proposal_id": proposal_id, "veto": payload.veto})
    return {
        "proposal_id": row.proposal_id,
        "reviewer": row.reviewer,
        "veto": row.veto,
        "approved": row.approved,
        "created_at": row.created_at,
    }


@app.post("/research/proposals/{proposal_id}/approve", dependencies=[Depends(require_admin)])
def research_approve(proposal_id: str, payload: ApprovalPayload) -> dict:
    try:
        row = _governance().approve(proposal_id, approver=payload.approver, rationale=payload.rationale)
    except GovernanceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit_research("research_proposal_approved", payload.approver, {"proposal_id": proposal_id})
    return {
        "proposal_id": row.proposal_id,
        "approver": row.approver,
        "rationale": row.rationale,
        "created_at": row.created_at,
    }


@app.post("/research/proposals/{proposal_id}/canary/start", dependencies=[Depends(require_admin)])
def research_canary_start(proposal_id: str, payload: CanaryStartPayload) -> dict:
    try:
        row = _governance().start_canary(
            proposal_id,
            duration_seconds=payload.duration_seconds,
            min_trades=payload.min_trades,
        )
    except GovernanceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit_research("research_canary_started", "admin", {"proposal_id": proposal_id})
    return {
        "proposal_id": row.proposal_id,
        "status": row.status,
        "started_at": row.started_at,
        "duration_seconds": row.duration_seconds,
        "min_trades": row.min_trades,
    }


@app.post("/research/proposals/{proposal_id}/canary/record", dependencies=[Depends(require_admin)])
def research_canary_record(proposal_id: str, payload: CanaryRecordPayload) -> dict:
    try:
        row = _governance().record_canary(
            proposal_id,
            trades=payload.trades,
            risk_breaches=payload.risk_breaches,
            reconciliation_ok=payload.reconciliation_ok,
            alerts_ok=payload.alerts_ok,
        )
    except GovernanceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit_research("research_canary_recorded", "admin", {"proposal_id": proposal_id, "trades": payload.trades})
    return {
        "proposal_id": row.proposal_id,
        "status": row.status,
        "trades": row.trades,
        "risk_breaches": row.risk_breaches,
        "reconciliation_ok": row.reconciliation_ok,
        "alerts_ok": row.alerts_ok,
    }


@app.post("/research/proposals/{proposal_id}/canary/finish", dependencies=[Depends(require_admin)])
def research_canary_finish(proposal_id: str) -> dict:
    try:
        row = _governance().finish_canary(proposal_id)
    except GovernanceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit_research("research_canary_finished", "admin", {"proposal_id": proposal_id, "status": row.status})
    return {"proposal_id": row.proposal_id, "status": row.status, "trades": row.trades}


@app.post("/research/proposals/{proposal_id}/promote", dependencies=[Depends(require_admin)])
def research_promote(proposal_id: str, payload: PromotionPayload) -> dict:
    try:
        row = _governance().promote(proposal_id, approver=payload.approver)
    except GovernanceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit_research("research_promoted", payload.approver, {"proposal_id": proposal_id, "strategy_id": row.strategy_id})
    return {
        "proposal_id": row.proposal_id,
        "strategy_id": row.strategy_id,
        "strategy_version": row.strategy_version,
        "previous_version": row.previous_version,
        "promoted_by": row.promoted_by,
        "status": row.status,
        "promoted_at": row.promoted_at,
    }


@app.post("/research/strategies/{strategy_id}/rollback", dependencies=[Depends(require_admin)])
def research_rollback(strategy_id: str, payload: RollbackPayload) -> dict:
    try:
        row = _governance().rollback(strategy_id, actor=payload.actor, reason=payload.reason)
    except GovernanceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit_research("research_rolled_back", payload.actor, {"strategy_id": strategy_id, "proposal_id": row.proposal_id})
    return {
        "proposal_id": row.proposal_id,
        "strategy_id": row.strategy_id,
        "status": row.status,
        "rolled_back_at": row.rolled_back_at,
        "rollback_reason": row.rollback_reason,
    }


# ---------------------------------------------------------------------------
# Milestone 2: read-only strategy / watchlist / analysis endpoints.
# No order placement anywhere: every handler below only reads public market
# data and computes a decision. The pipeline execution path stays locked.
# ---------------------------------------------------------------------------


@app.get("/strategies")
def strategies() -> dict:
    return {
        "strategies": list_strategies(),
        "default": DEFAULT_STRATEGY_ID,
    }


@app.get("/watchlist")
def watchlist(db: Session = Depends(get_db)) -> dict:
    return WatchlistService(db).list_instruments()


@app.post("/watchlist", dependencies=[Depends(require_admin)])
def watchlist_add(payload: WatchlistPayload, db: Session = Depends(get_db)) -> dict:
    return WatchlistService(db).add(payload.instrument_id or payload.symbol)


@app.delete("/watchlist/{instrument_id}", dependencies=[Depends(require_admin)])
def watchlist_remove(instrument_id: str, db: Session = Depends(get_db)) -> dict:
    return WatchlistService(db).remove(instrument_id)


@app.post("/watchlist/select", dependencies=[Depends(require_admin)])
def watchlist_select(payload: WatchlistPayload, db: Session = Depends(get_db)) -> dict:
    service = WatchlistService(db)
    selected = service.set_selected(payload.instrument_id or payload.symbol)
    return {"selected": selected, "default_symbol": DEFAULT_SYMBOL}


@app.get("/instruments")
def instruments() -> dict:
    base = AnalysisService(settings=settings).list_instruments()
    return {
        "instruments": InstrumentRegistry.enrich_listing(base.get("instruments", [])),
        "error": base.get("error"),
    }


@app.get("/instruments/catalog")
def instruments_catalog() -> dict:
    base = AnalysisService(settings=settings).list_instruments()
    availability = {i.get("symbol"): bool(i.get("available")) for i in base.get("instruments", [])}
    rows = InstrumentRegistry.with_availability(InstrumentRegistry().list_all(), availability)
    return {"instruments": rows, "error": base.get("error")}


@app.get("/instruments/search")
def instruments_search(
    q: str = "",
    market: str | None = None,
    asset_type: str | None = None,
    exchange: str | None = None,
    limit: int = 30,
) -> dict:
    base = AnalysisService(settings=settings).list_instruments()
    availability = {i.get("symbol"): bool(i.get("available")) for i in base.get("instruments", [])}
    rows = InstrumentRegistry().search(q=q, market=market, asset_type=asset_type, exchange=exchange, limit=limit)
    return {"instruments": InstrumentRegistry.with_availability(rows, availability), "error": base.get("error")}


@app.get("/analysis/{symbol}")
def analysis(
    symbol: str,
    strategy: str = DEFAULT_STRATEGY_ID,
    fund: float = DEFAULT_FUND_INR,
    risk: float = DEFAULT_RISK_PCT,
) -> dict:
    return AnalysisService(settings=settings).analyze(
        symbol.upper(),
        strategy_id=strategy,
        fund_inr=fund,
        risk_pct=risk,
    )
