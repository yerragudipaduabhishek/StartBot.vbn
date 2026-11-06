from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, Float, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Candle(Base):
    __tablename__ = "candles"
    __table_args__ = (
        UniqueConstraint("symbol", "resolution", "time", name="uq_candle"),
        Index("ix_candle_lookup", "symbol", "resolution", "time"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32))
    resolution: Mapped[str] = mapped_column(String(16))
    time: Mapped[int] = mapped_column(Integer)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float, default=0.0)
    source: Mapped[str] = mapped_column(String(32), default="unknown")
    venue: Mapped[str] = mapped_column(String(64), default="unknown")
    transport: Mapped[str] = mapped_column(String(16), default="unknown")
    fallback_used: Mapped[bool] = mapped_column(Boolean, default=False)
    is_closed: Mapped[bool] = mapped_column(Boolean, default=True)
    quality_status: Mapped[str] = mapped_column(String(16), default="ok")
    quality_flags: Mapped[str] = mapped_column(Text, default="")
    ingested_at: Mapped[datetime | None] = mapped_column(nullable=True)
    fetched_at: Mapped[datetime | None] = mapped_column(nullable=True)


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    candle_time: Mapped[int] = mapped_column(Integer)
    symbol: Mapped[str] = mapped_column(String(32))
    timeframe: Mapped[str] = mapped_column(String(16))
    direction: Mapped[str] = mapped_column(String(8))  # long | short | none
    score: Mapped[float] = mapped_column(Float, default=0.0)
    entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    target: Mapped[float | None] = mapped_column(Float, nullable=True)
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    strategy_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    strategy_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    risk_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    data_quality: Mapped[str | None] = mapped_column(Text, nullable=True)


class PaperTrade(Base):
    __tablename__ = "paper_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(8))  # long | short
    size: Mapped[float] = mapped_column(Float)
    entry_price: Mapped[float] = mapped_column(Float)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16))  # open | closed | stopped
    pnl: Mapped[float] = mapped_column(Float, default=0.0)
    notional: Mapped[float] = mapped_column(Float, default=0.0)
    leverage: Mapped[float] = mapped_column(Float, default=0.0)
    fees: Mapped[float] = mapped_column(Float, default=0.0)
    slippage: Mapped[float] = mapped_column(Float, default=0.0)  # adverse slippage in bps
    opened_at: Mapped[datetime] = mapped_column(default=_utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class PaperAccount(Base):
    """Persistent paper balance. Always simulated money - never a wallet,
    never an exchange position, never the dashboard Trading Fund."""

    __tablename__ = "paper_account"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    starting_equity: Mapped[float] = mapped_column(Float)
    balance: Mapped[float] = mapped_column(Float)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    currency: Mapped[str] = mapped_column(String(8), default="INR")
    fee_pct: Mapped[float] = mapped_column(Float, default=0.05)
    slippage_pct: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow)


class PaperPosition(Base):
    """Persistent open paper position. Reflects a local simulation only; no
    exchange position is ever queried or modified for it."""

    __tablename__ = "paper_positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(8))  # long | short
    quantity: Mapped[float] = mapped_column(Float)
    entry_price: Mapped[float] = mapped_column(Float)
    current_price: Mapped[float] = mapped_column(Float)
    notional: Mapped[float] = mapped_column(Float, default=0.0)
    leverage: Mapped[float] = mapped_column(Float, default=0.0)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    fees: Mapped[float] = mapped_column(Float, default=0.0)
    slippage: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(16), default="open")
    opened_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow)


class WatchlistItem(Base):
    """User instrument watchlist - a persisted registry-id reference set.

    Metadata only and strictly read-only with respect to trading: it never
    places an order and never references an exchange account.
    """

    __tablename__ = "watchlist_items"
    __table_args__ = (UniqueConstraint("instrument_id", name="uq_watchlist_instrument"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument_id: Mapped[str] = mapped_column(String(64))
    position: Mapped[int] = mapped_column(Integer, default=0)
    selected: Mapped[bool] = mapped_column(default=False)
    added_at: Mapped[datetime] = mapped_column(default=_utcnow)


class TestnetExecution(Base):
    """One persisted testnet execution round-trip (entry order + SL/TP outcome).

    Mirrors the execution report so the validation phase produces an auditable,
    queryable record of order mechanics: expected vs actual price, slippage,
    fees, latency, and the SL/TP + P&L outcome.
    """

    __tablename__ = "testnet_executions"
    __table_args__ = (UniqueConstraint("client_order_id", name="uq_testnet_exec_client_order_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    client_order_id: Mapped[str] = mapped_column(String(64))
    signal_time: Mapped[int] = mapped_column(Integer)
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(8))  # long | short | buy | sell
    size: Mapped[float] = mapped_column(Float)
    venue: Mapped[str] = mapped_column(String(32), default="delta-testnet")

    expected_entry: Mapped[float] = mapped_column(Float)
    actual_entry: Mapped[float | None] = mapped_column(Float, nullable=True)
    slippage: Mapped[float] = mapped_column(Float, default=0.0)
    slippage_bps: Mapped[float] = mapped_column(Float, default=0.0)

    expected_sl: Mapped[float | None] = mapped_column(Float, nullable=True)
    actual_sl: Mapped[float | None] = mapped_column(Float, nullable=True)
    expected_tp: Mapped[float | None] = mapped_column(Float, nullable=True)
    actual_tp: Mapped[float | None] = mapped_column(Float, nullable=True)

    fees: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)

    order_status: Mapped[str] = mapped_column(String(16), default="open")
    position_status: Mapped[str] = mapped_column(String(32), default="flat")
    final_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    exit_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    raw: Mapped[str | None] = mapped_column(Text, nullable=True)


class RiskState(Base):
    """Persisted risk lifecycle for one account/profile key."""

    __tablename__ = "risk_state"
    __table_args__ = (UniqueConstraint("account_key", name="uq_risk_state_account"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_key: Mapped[str] = mapped_column(String(128))
    starting_equity: Mapped[float] = mapped_column(Float)
    peak_equity: Mapped[float] = mapped_column(Float)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    day: Mapped[date] = mapped_column(Date)
    day_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    open_positions: Mapped[int] = mapped_column(Integer, default=0)
    trades_today: Mapped[int] = mapped_column(Integer, default=0)
    last_trade_bar_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trading_halted: Mapped[bool] = mapped_column(Boolean, default=False)
    halt_reason: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow)


class RiskEvent(Base):
    """Idempotency ledger for closed-trade P&L application."""

    __tablename__ = "risk_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_key: Mapped[str] = mapped_column(String(128), index=True)
    pnl: Mapped[float] = mapped_column(Float)
    bar_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    applied_at: Mapped[datetime] = mapped_column(default=_utcnow)


class KillSwitchState(Base):
    """Persistent human-controlled entry halt."""

    __tablename__ = "kill_switch"
    __table_args__ = (UniqueConstraint("account_key", name="uq_kill_switch_account"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_key: Mapped[str] = mapped_column(String(64), default="global")
    halted: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(Text, default="")
    updated_by: Mapped[str] = mapped_column(String(64), default="system")
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow)


class RunnerState(Base):
    """Persisted runner cursor so a restart seeds rather than auto-enters."""

    __tablename__ = "runner_state"
    __table_args__ = (UniqueConstraint("runner_key", name="uq_runner_state_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    runner_key: Mapped[str] = mapped_column(String(192))
    last_candle_time: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow)


class ExecutionIntent(Base):
    """Duplicate-entry guard keyed by strategy/symbol/timeframe/candle."""

    __tablename__ = "execution_intents"
    __table_args__ = (UniqueConstraint("signal_key", name="uq_execution_intent_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    signal_key: Mapped[str] = mapped_column(String(256))
    venue: Mapped[str] = mapped_column(String(32), default="paper")
    status: Mapped[str] = mapped_column(String(32), default="reserved")
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)


class AuditEvent(Base):
    """Append-only operational audit record for decisions and controls."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow, index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    actor: Mapped[str] = mapped_column(String(128), default="system")
    correlation_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    severity: Mapped[str] = mapped_column(String(16), default="info")
    payload: Mapped[str] = mapped_column(Text, default="{}")


class RiskDecisionRecord(Base):
    """Durable risk decision made before an order is created."""

    __tablename__ = "risk_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow, index=True)
    signal_key: Mapped[str] = mapped_column(String(256), index=True)
    execution_mode: Mapped[str] = mapped_column(String(16))
    allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(Text, default="")
    size: Mapped[float | None] = mapped_column(Float, nullable=True)
    equity: Mapped[float | None] = mapped_column(Float, nullable=True)
    payload: Mapped[str] = mapped_column(Text, default="{}")


class Order(Base):
    """Broker-neutral order lifecycle, reconstructed from rows alone."""

    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("client_order_id", name="uq_order_client_id"),
        Index("ix_orders_status_time", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow)
    client_order_id: Mapped[str] = mapped_column(String(128))
    idempotency_key: Mapped[str | None] = mapped_column(String(160), nullable=True, unique=True)
    signal_key: Mapped[str] = mapped_column(String(256), default="", index=True)
    venue: Mapped[str] = mapped_column(String(32), default="paper")
    execution_mode: Mapped[str] = mapped_column(String(16), default="paper")
    symbol: Mapped[str] = mapped_column(String(32), default="")
    side: Mapped[str] = mapped_column(String(8), default="")
    order_type: Mapped[str] = mapped_column(String(16), default="market")
    size: Mapped[float] = mapped_column(Float, default=0.0)
    filled_size: Mapped[float] = mapped_column(Float, default=0.0)
    expected_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    fees: Mapped[float] = mapped_column(Float, default=0.0)
    slippage: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(24), default="created")
    reason: Mapped[str] = mapped_column(Text, default="")
    strategy_id: Mapped[str] = mapped_column(String(64), default="")
    strategy_version: Mapped[str] = mapped_column(String(32), default="")
    risk_decision_key: Mapped[str] = mapped_column(String(256), default="")
    risk_decision_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw: Mapped[str] = mapped_column(Text, default="{}")


class OrderEvent(Base):
    """Append-only order transition log with a unique idempotency key."""

    __tablename__ = "order_events"
    __table_args__ = (Index("ix_order_events_order_time", "order_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow, index=True)
    order_id: Mapped[int] = mapped_column(Integer, index=True)
    client_order_id: Mapped[str] = mapped_column(String(128), index=True)
    from_state: Mapped[str] = mapped_column(String(24), default="")
    to_state: Mapped[str] = mapped_column(String(24))
    event_type: Mapped[str] = mapped_column(String(32), default="transition")
    idempotency_key: Mapped[str | None] = mapped_column(String(192), nullable=True, unique=True)
    payload: Mapped[str] = mapped_column(Text, default="{}")


class Fill(Base):
    """One execution fill; multiple fills may belong to a partial order."""

    __tablename__ = "fills"
    __table_args__ = (
        UniqueConstraint("fill_key", name="uq_fill_key"),
        Index("ix_fills_order_time", "order_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow, index=True)
    fill_key: Mapped[str] = mapped_column(String(192))
    order_id: Mapped[int] = mapped_column(Integer, index=True)
    client_order_id: Mapped[str] = mapped_column(String(128), index=True)
    venue: Mapped[str] = mapped_column(String(32), default="")
    price: Mapped[float] = mapped_column(Float)
    size: Mapped[float] = mapped_column(Float)
    fee: Mapped[float] = mapped_column(Float, default=0.0)
    slippage: Mapped[float] = mapped_column(Float, default=0.0)
    raw: Mapped[str] = mapped_column(Text, default="{}")


class StrategyVersion(Base):
    """Immutable strategy version linked to a validation artifact."""

    __tablename__ = "strategy_versions"
    __table_args__ = (UniqueConstraint("strategy_id", "version", name="uq_strategy_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    strategy_id: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(24), default="UNVALIDATED")
    artifact_path: Mapped[str] = mapped_column(Text, default="")
    validation_run_id: Mapped[str] = mapped_column(String(64), default="")
    dataset_hash: Mapped[str] = mapped_column(String(64), default="")
    params_json: Mapped[str] = mapped_column(Text, default="{}")
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    promoted_at: Mapped[datetime | None] = mapped_column(nullable=True)


class EquitySnapshot(Base):
    """Point-in-time equity snapshot used for drawdown and dashboard metrics."""

    __tablename__ = "equity_snapshots"
    __table_args__ = (Index("ix_equity_snapshot_account_time", "account_key", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow, index=True)
    account_key: Mapped[str] = mapped_column(String(128), index=True)
    equity: Mapped[float] = mapped_column(Float)
    balance: Mapped[float] = mapped_column(Float)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    drawdown_pct: Mapped[float] = mapped_column(Float, default=0.0)
    source: Mapped[str] = mapped_column(String(32), default="paper")


class SystemEvent(Base):
    """Lifecycle and health events retained for incident investigation."""

    __tablename__ = "system_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow, index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    severity: Mapped[str] = mapped_column(String(16), default="info")
    message: Mapped[str] = mapped_column(Text)
    payload: Mapped[str] = mapped_column(Text, default="{}")


class Alert(Base):
    """Deduplicated alert state; delivery is deliberately non-blocking."""

    __tablename__ = "alerts"
    __table_args__ = (Index("ix_alerts_active", "active", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(256), unique=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow)
    severity: Mapped[str] = mapped_column(String(16), default="warning")
    title: Mapped[str] = mapped_column(String(160))
    message: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    delivered: Mapped[bool] = mapped_column(Boolean, default=False)
