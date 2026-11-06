# Architecture - BTC AI Trading System

**Status:** Phase 1 architecture migration implemented (2026-10-31): typed contracts, the
`TradingProfile` configuration boundary, explicit execution modes/adapters, and research/backtest
persistence boundaries are in place. Phase 2 data/research engine implemented (2026-11-01): validated
candle quality, hashed datasets, provenance, a shared causal feature pipeline, an isolated research
database, and reproducible experiment records. Phase 3 observability foundations added
(2026-11-01): durable audit/system events, alerts, equity snapshots, SQLite runtime pragmas, and
read-only metrics endpoints. Migration and dashboard work remain in progress. Live execution
remains permanently disabled. The remaining target sections are the roadmap for later phases.
**Related:** [`RESEARCH_ENGINE.md`](RESEARCH_ENGINE.md), [`PROJECT_AUDIT.md`](PROJECT_AUDIT.md),
[`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md), [`SECURITY_AND_RISK.md`](SECURITY_AND_RISK.md)

---

## 1. Goals and constraints

### Goals

1. Run a deterministic, auditable **paper** trading loop on live market data.
2. Validate strategies with reproducible, timeframe-correct backtests and rolling walk-forward.
3. Keep every order behind one deterministic risk gate.
4. Add AI research agents that can propose changes but cannot execute or alter risk.
5. Support a human-approved self-improvement loop with canary, promotion, and rollback.
6. Make system state observable and recoverable from the database and artifacts alone.

### Hard constraints

- Paper mode is the default and the only mode this plan enables.
- Live mode is unreachable without a separate, explicit, signed readiness process.
- AI never has import-level access to risk limits, the order manager, or a broker.
- Missing/stale/invalid data or an invalid signal fails closed.
- Every decision and order is attributable to a strategy version, dataset, and risk decision.

### Non-goals (for this plan)

- Achieving or claiming profitability.
- High-frequency or co-located execution.
- Multi-exchange arbitrage.
- Automatic live promotion.
- A microservices split. A modular monolith with clear boundaries is sufficient.

---

## 2. Current architecture (as implemented)

```text
                     Delta Exchange
        +----------------+----------------+
        | REST public    | WebSocket      |
        | (testnet or    | ticker/funding |
        |  prod fallback)|                |
        +--------+-------+--------+-------+
                 |                |
          DeltaClient        DeltaWebSocket
                 |                |
              MarketData / MarketHealthMonitor
                 |
                 | candles + ticker
                 v
   +------------------------ SignalEngine ------------------------+
   | trend / structure / sentiment / derivatives -> decision      |
   |                        |                                     |
   |                   RiskManager (in-memory)                    |
   +------------------------+-------------------------------------+
                            | dict signal
             +--------------+----------------+
             |                               |
        PaperTrader                    TradingRunner / SoakRunner
     (SQLite account/position/          (closed-candle loop)
      trades)                                  |
             |                                 |
             +-------------> Signals (SQLite) -+
                            |
                    FastAPI (app/main.py)
                            |
                    Next.js dashboard

Guarded execution path (Phase 1):
  TradeGuard -> RiskDecision -> ExecutionRouter
                                  |-> PaperExecutionAdapter (local only)
                                  |-> TestnetExecutionAdapter -> strict testnet client
                                  `-> LiveExecutionAdapter (always rejects)

Offline path (research/backtest; no trading-state persistence):
  DB/REST candles -> dataset builder (quality + hash + provenance)
    -> FeaturePipeline (phase2.v1) -> backtest engine (research risk copy)
    -> study / run_experiment -> research.db (datasets + experiments, isolated)
```

### Current components

| Layer | Files | Responsibility |
|---|---|---|
| Config | `app/config/settings.py`, `app/config/trading.py` | env settings, venue URLs, safety flags, single `TradingProfile` |
| Contracts | `app/contracts.py` | typed signals, risk decisions, market snapshots, orders, fills, execution modes |
| Data | `app/data/` | REST/WS market data, history/backfill, resampling, provenance-aware news, quality, hashed datasets |
| Features | `app/features/pipeline.py` | one causal, versioned OHLCV-to-indicator transform for live/backtest/soak |
| Strategy | `app/strategy/` | signal agents, weighted decision, signal engine |
| Risk | `app/risk/` | deterministic checks and fixed-fractional sizing |
| Paper | `app/trading/paper_trader.py` | simulated fills, fees, slippage, persistence |
| Runner | `app/trading/runner.py`, `app/soak/` | live closed-candle loops and telemetry |
| Execution | `app/trading/executor.py`, `app/execution/` | mode policy, adapters, guarded routing, testnet validation |
| Backtest | `app/backtest/` | event replay, metrics, regimes, parameter study, dataset/experiment recording |
| Research | `app/research/` | isolated research DB, dataset/experiment lineage, experiment runner |
| Analysis | `app/analysis/` | dashboard-facing per-symbol analysis |
| Instruments | `app/instruments/` | registry, watchlist, provider mapping, compatibility |
| Persistence | `app/database/` | SQLAlchemy models, SQLite session, ad-hoc migration |
| API/UI | `app/main.py`, `dashboard/` | REST endpoints and Next.js UI |
| Ops | `Dockerfile`, `docker-compose.yml` | app container, health, optional infra |

### Phase 1 status (implemented)

- `app/contracts.py` is now the shared typed contract layer: `Candle`, `MarketSnapshot`,
  `DataQuality`, `Signal`, `RiskDecision`, `OrderRequest`, `OrderResult`, `PositionSize`,
  `Position`, `Trade`, `AccountState`, and the `ExecutionMode` enum. Legacy import paths
  (`app.strategy.signal`, `app.risk.decision`, `app.trading.orders`) re-export the same models.
- `TradingProfile` in `app/config/trading.py` binds strategy identity/version, symbol, timeframe,
  EMA/decision/risk settings, costs, contract value, and execution mode. It rejects live mode and
  rejects Strategy A drift. `dev`, `test`, `staging`, and `simulation` are paper-compatible aliases;
  `research` and `backtest` are explicit offline modes.
- `app/execution/modes.py` owns mode policy: research/backtest cannot submit orders or persist
  trading state; paper is local simulation only; testnet requires explicit testnet configuration;
  live is permanently rejected.
- `app/execution/adapters.py` contains `PaperExecutionAdapter`, `TestnetExecutionAdapter`, the
  disabled `LiveExecutionAdapter`, and `ExecutionRouter`. The router validates a risk-approved
  decision, signal-size agreement, mode, symbol, timeframe, strategy version, key, and mode before
  delegating.
- `RiskManager` now carries an execution mode, fails closed on live, and refuses persistence in
  research/backtest. `Executor` routes paper and testnet through the router; legacy live flags never
  produce a live route.
- Backtest replay uses a non-persisting research risk copy when given a DB-bound risk manager.
  `SoakRunner` is explicitly paper-only. Paper execution has no exchange order client.
- Remaining roadmap work is the later-phase target: unified event ledger, research/promotion
  lifecycle, and the full order state machine.

### Phase 2 status (implemented)

- `app/data/quality.py` validates duplicates, ordering, OHLCV integrity, symbol/timeframe identity,
  timestamp alignment, unclosed bars, staleness, and gaps. Critical defects reject a research
  dataset; optional gap/staleness strictness is explicit.
- `app/data/dataset.py` produces canonical ascending candles, a SHA-256 content hash, row/range
  metadata, and source/venue/transport/fallback/fetched-at provenance. `fetch_dataset` recursively
  time-splits full REST pages so deep history cannot be silently truncated.
- Persisted candles carry source, venue, transport, fallback, closed-candle, and fetch-time fields;
  existing SQLite databases are migrated in place. Backfill and resampling preserve provenance.
- `app/features/pipeline.py` (`phase2.v1`) is the single causal feature transform used by live
  signals, backtests, soak runs, and experiments. Prefix/full parity and future-bar independence are
  covered by tests.
- Backtest results carry `dataset_hash`, dataset metadata, and `feature_pipeline_version`.
- `app/research/` stores datasets, experiments, and news events in a dedicated database
  (`RESEARCH_DATABASE_URL`, default `data/research.db`), never in the trading database. Experiments
  require a recorded dataset; validation can require a reproduction callback.
- News ingestion is optional and fail-open for the technical loop; items retain source, URL,
  publication/fetch timestamps, provider, and symbols.
- Tests: `tests/test_phase2_data.py`, `tests/test_phase2_research.py`; full suite 419 passed.

---

## 3. Target architecture

### 3.1 Principles

1. **Functional core, imperative shell.** Pure strategy/risk/metrics logic; I/O at the edges.
2. **Typed contracts.** Pydantic models for signals, decisions, orders, snapshots, artifacts.
3. **One path per action.** All orders go through the order manager; all data goes through quality;
   all research proposals go through review.
4. **Append-only memory.** Decisions, orders, fills, experiments, approvals are never silently
   overwritten.
5. **Explicit versions.** Strategy, feature set, dataset, parameter set, and code version are recorded.
6. **Fail closed at every boundary.**
7. **Ports and adapters.** Market data, broker, LLM, alerts, clock, and storage are interfaces.
8. **Single writer.** One trading process owns paper state; API/dashboard are readers (except explicit
   admin actions).

### 3.2 Target component diagram

```text
 EXTERNAL                    CORE DOMAIN                         DELIVERY
 --------                    -----------                         --------
 Delta REST/WS  --> MarketDataProvider --> DataQuality --> MarketSnapshot
                                                       |
                                                       v
                                               FeaturePipeline (versioned)
                                                       |
                                                       v
                                               Strategy (versioned)
                                                       |
                                                    Signal
                                                       |
                                              SignalValidator (fail closed)
                                                       |
                                                       v
 PortfolioState --> RiskEngine (deterministic) --> RiskDecision
                                                       |
                                                       v
                                              OrderIntent + idempotency key
                                                       |
                                                       v
                                              OrderManager (sole path)
                                                       |
                         +-----------------------------+---------------------+
                         |                             |                     |
                    PaperBroker                  TestnetBroker        LiveBroker
                         |                             |              (disabled)
                         +-----------------------------+---------------------+
                                                       |
                                              Fills/Positions/Equity
                                                       |
                                                       v
                                              EventStore (DB) --> API --> Dashboard
                                                       |              |
                                                       +--> Analytics +--> Alerts

 RESEARCH (read-only, separate process/role)
 Market/data snapshots + artifacts + performance
        -> AI Orchestrator -> five research agents
        -> Proposal + Review -> ExperimentRunner -> Validation gates
        -> Human approval -> StrategyRegistry -> Paper canary -> Promote/Rollback
```

### 3.3 Target package layout

Existing modules remain during migration; new packages are introduced around them.

```text
app/
  core/
    config.py            # profiles, validation, startup refusal
    clock.py             # single UTC clock, injectable for tests
    ids.py               # deterministic/idempotency key helpers
    errors.py            # domain error hierarchy
    logging.py           # structured JSON logging, correlation IDs
  marketdata/
    providers/           # delta REST/WS, historical, fake
    quality.py           # OHLCV/gap/staleness validator
    store.py             # candles + dataset versions
    cache.py             # TTL cache
    ratelimit.py         # token bucket, Retry-After, circuit breaker
    snapshot.py          # MarketSnapshot + provenance
  features/
    pipeline.py          # causal, versioned features
    indicators.py        # SMA/EMA/RSI/MACD/ATR/ADX/BB/VWAP/volume/regime
    store.py             # optional feature snapshots
  strategy/
    contract.py          # Strategy protocol
    signal.py            # typed Signal
    validation.py        # validation + freshness/quality checks
    registry.py          # immutable versions/status
    strategies/          # trend_regime_1h, experiments
  risk/
    engine.py            # deterministic checks
    limits.py            # RiskLimits (human-controlled)
    state.py             # persisted lifecycle state
    kill_switch.py       # halt/resume
  portfolio/
    state.py             # balances, positions, exposure
    manager.py           # allocation, caps, rebalance
  execution/
    broker.py            # Broker protocol
    paper.py             # PaperBroker
    testnet.py           # DeltaTestnetBroker
    live.py              # DeltaLiveBroker (disabled)
    orders.py            # OrderIntent/Order/Fill state machine
    order_manager.py     # sole entry point, idempotency, reconciliation
    guard.py             # mandatory pre-trade gate
  backtest/
    engine.py            # next-bar execution, MTM, costs
    metrics.py           # timeframe-aware metrics
    walkforward.py       # rolling re-fit, folds, leakage guards
    optimize.py          # trial registry
    artifacts.py         # reproducible experiment bundle
  research/
    llm.py               # provider protocol + adapters + stub
    agents/              # market, strategy, code, reviewer, performance
    orchestrator.py      # scheduled read-only pipeline
    schemas.py           # structured outputs
  selfimprove/
    proposals.py         # proposal lifecycle
    gates.py             # validation/canary gates
    promotion.py         # promote/rollback, version pinning
  persistence/
    models.py            # SQLAlchemy models
    repositories.py      # typed queries
    migrations/          # Alembic
    events.py            # append-only event store
  observability/
    metrics.py
    alerts.py
    health.py
  api/
    app.py               # FastAPI wiring
    routes/              # read-only + authenticated admin
  main.py                # composition root (paper mode)
  soak/                  # long-running paper soak
```

### 3.4 Migration strategy (strangler, not rewrite)

1. Add typed contracts and adapters around existing code.
2. Route one behavior at a time through the new path (paper first, then testnet).
3. Keep existing public functions/endpoints until tests migrate.
4. Delete old code only after the new path has equivalent tests.
5. Never change risk/live defaults in the same PR as a structural change.

---

## 4. Core contracts

The Phase 1 contract layer is implemented in `app/contracts.py`. The snippets below show the target
evolution of those contracts; field names may evolve, but the boundary rules are already enforced.

### 4.1 Market data and quality

```python
class MarketSnapshot(BaseModel):
    symbol: str
    timeframe: str
    candles: list[Candle]              # ascending, closed bars
    ticker: Ticker | None
    source: str                        # "delta-testnet", "delta-production", ...
    generated_at: datetime
    last_candle_age_secs: float
    data_status: Literal["streaming", "stale", "unavailable"]
    price_valid: bool
    quality: DataQualityReport

class DataQualityReport(BaseModel):
    ok: bool
    errors: list[str]
    warnings: list[str]
    gaps: list[Gap]
    row_count: int
    dataset_id: str | None
```

Rules: no strategy call with `quality.ok=False` or non-streaming data in paper/live; backtest data
must carry a dataset id/hash.

### 4.2 Strategy and signal

```python
class Signal(BaseModel):
    strategy_id: str
    strategy_version: str
    symbol: str
    timeframe: str
    candle_time: int
    created_at: datetime
    direction: Literal["long", "short", "none"]
    score: float
    confidence: float
    entry: float | None
    stop_loss: float | None
    target: float | None
    accepted_size: float | None
    risk_allowed: bool
    risk_reason: str
    data_quality: str
    reason_codes: list[str]

class Strategy(Protocol):
    id: str
    version: str
    def evaluate(self, snapshot: MarketSnapshot, features: FeatureSet) -> Signal: ...
```

Rules: `risk_allowed` is produced only by the risk engine; a strategy cannot set it to true.
`direction="none"` carries no order fields.

### 4.3 Risk

```python
class RiskDecision(BaseModel):
    allowed: bool
    size: float | None
    reasons: list[str]
    checks: dict[str, bool]
    limits_version: str
    decided_at: datetime

class RiskEngine(Protocol):
    def evaluate(
        self,
        signal: Signal,
        portfolio: PortfolioState,
        snapshot: MarketSnapshot,
        now: datetime,
    ) -> RiskDecision: ...
```

Risk limits are loaded from human-controlled config, versioned, and recorded with every decision.
No research/AI process may write `risk/limits.py`, `RiskLimits`, or a `RiskDecision`.

### 4.4 Orders and broker

```python
class OrderIntent(BaseModel):
    client_order_id: str             # idempotency key
    strategy_id: str
    strategy_version: str
    symbol: str
    side: Literal["buy", "sell"]
    size: float
    order_type: Literal["market", "limit"]
    limit_price: float | None
    stop_loss: float | None
    target: float | None
    reduce_only: bool
    created_at: datetime

class Broker(Protocol):
    venue: str
    supports_native_sltp: bool
    def place_order(self, intent: OrderIntent) -> OrderResult: ...
    def cancel_order(self, order_id: str) -> OrderResult: ...
    def get_order(self, order_id: str) -> OrderResult: ...
    def get_positions(self) -> list[Position]: ...
    def get_balances(self) -> list[Balance]: ...

class OrderManager:
    def submit(self, intent: OrderIntent, decision: RiskDecision) -> Order: ...
```

Rules: `submit` rejects absent/failed decisions, size mismatch, duplicate key, stale snapshot, or
halted state. The broker never receives an intent directly from strategy/research code.

### 4.5 Experiment and research

```python
class ExperimentSpec(BaseModel):
    strategy_id: str
    strategy_version: str
    dataset_id: str
    feature_set_version: str
    params: dict
    costs: CostModel
    seed: int
    folds: list[FoldSpec]

class ExperimentArtifact(BaseModel):
    experiment_id: str
    spec_hash: str
    trades: list[Trade]
    equity_curve: list[EquityPoint]
    metrics: dict[str, float]
    folds: list[FoldResult]
    command: str
    code_version: str
    dataset_hash: str
    created_at: datetime

class LLMClient(Protocol):
    def complete_structured(
        self, *, model: str, system: str, user: str, schema: type[BaseModel], budget: Budget
    ) -> BaseModel: ...

class StrategyRegistry:
    def get(self, strategy_id: str) -> StrategyVersion: ...
    def promote(self, version: str, approval: Approval) -> None: ...
    def rollback(self, to_version: str, approval: Approval) -> None: ...
```

Rules: agents return schema-validated proposals/reviews only; no execution/risk imports; promotion
requires a stored human approval and passing gates.

---

## 5. Runtime data flows

### 5.1 Paper trading (default)

```text
feed -> MarketSnapshot + quality
     -> FeaturePipeline (causal, versioned)
     -> Strategy.evaluate -> Signal
     -> SignalValidator (freshness, schema, quality, direction)
     -> RiskEngine.evaluate -> RiskDecision
     -> OrderManager.submit -> PaperBroker
     -> Fill/Position/Equity/EventStore
     -> API -> Dashboard
     -> Alerts on breach/stale/rejection
```

Startup: hydrate paper account, positions, orders, risk state, runner state; reconcile; seed the last
processed candle; **never** trade on the seed cycle.

### 5.2 Testnet validation

Same path through `RiskEngine` and `OrderManager`, but the broker is `DeltaTestnetBroker`. Native
SL/TP is optional; if simulated, the SL/TP monitor owns the exit and writes the same fill events.
Reconciliation runs at startup and on reconnect. Testnet remains a validation venue, not an
alternative risk path.

### 5.3 Backtest

```text
DatasetVersion (hash, range, quality)
  -> FeaturePipeline (same code/version as live)
  -> fold split (purged/embargoed where needed)
  -> Strategy.evaluate at bar close
  -> SignalValidator + RiskEngine (same deterministic code)
  -> simulated broker: next-bar entry, fees, slippage, funding, sessions
  -> trades + MTM equity + metrics + fold results
  -> ExperimentArtifact + DB rows
```

The backtest must use the same indicator/feature code as live; only the data source and broker differ.
A result without an artifact and dataset hash is not evidence.

### 5.4 Research and self-improvement

```text
scheduled/manual trigger
  -> read-only inputs: market snapshot, data quality, paper/backtest performance, artifacts
  -> AI Orchestrator
       -> MarketAnalyst
       -> StrategyResearcher -> Proposal
       -> CodeAgent -> PatchProposal (sandbox, never applied)
       -> PerformanceAnalyst -> Diagnostics
       -> ResearchReviewer -> Review/Veto
  -> ExperimentRunner (deterministic, no LLM)
  -> validation gates (OOS, walk-forward, cost sensitivity, drawdown, min trades)
  -> human approval (recorded identity/rationale)
  -> paper canary
  -> promotion/rollback in StrategyRegistry
  -> full audit trail
```

The AI path is asynchronous and isolated from the trading process. It may read artifacts and write
proposal/review rows; it cannot write orders, fills, risk decisions, limits, or registry state.

---

## 6. Safety boundary

### Trust zones

| Zone | Trust | Examples |
|---|---|---|
| External data | Untrusted input | exchange payloads, news, calendar, LLM responses |
| Research | Untrusted/sandboxed | proposals, critiques, generated patches |
| Deterministic core | Trusted, reviewed | signal validation, risk engine, order manager, broker adapters |
| Human control | Trusted, audited | risk limits, approvals, kill switch, live readiness |
| Observability | Read-only | dashboard, metrics, alerts |

### Enforced boundaries

- Research package must not import `app.risk`, `app.execution`, or `app.portfolio` execution paths.
  Enforce with an import-boundary test and, in production, a separate process/DB role.
- `OrderManager.submit` requires a `RiskDecision` produced by the deterministic engine for the same
  signal/version/snapshot; no caller-supplied boolean can substitute.
- Risk limits are loaded from a separate, versioned file/table; research code has read-only access at
  most.
- Live adapter requires a signed readiness record plus all existing flags; it is not imported by the
  paper composition root.
- Kill switch is persisted and checked before every entry; resume requires a human/admin action.
- Untrusted text is passed to LLMs as data, never as instructions; prompts delimit and label it.

See [`SECURITY_AND_RISK.md`](SECURITY_AND_RISK.md) for the threat model and runbooks.

---

## 7. Persistence model

### Existing tables (keep, evolve)

`candles`, `signals`, `paper_accounts`, `paper_positions`, `paper_trades`, `testnet_executions`,
`watchlist_items` (uncommitted). Phase 2 adds provenance columns to `candles`.

The research database is separate and contains only `research_datasets`, `research_experiments`, and
`research_news_events`. It has no account, order, position, risk, or execution tables.

### Target additions

| Table | Purpose | Key fields/indexes |
|---|---|---|
| `dataset_versions` | immutable dataset identity | id, symbol, timeframe, start, end, rows, gaps, hash, source |
| `feature_sets` / snapshots | feature lineage | version, params, candle_time, dataset_id |
| `strategy_versions` | immutable strategies | id, version, code_version, params_hash, status, dataset_id |
| `risk_state` | restart-safe risk lifecycle | account, day, day_pnl, peak, realized, open positions, trades, halted |
| `risk_decisions` | every decision | signal_id, allowed, reasons, checks JSON, limits_version |
| `orders` | order state machine | client_order_id unique, intent, state, timestamps |
| `fills` | executions | order_id, price, size, fee, slippage, venue, timestamp |
| `equity_snapshots` | curves/drawdown | account, timestamp, balance, equity, unrealized |
| `runner_state` | idempotent restarts | profile, last_processed_candle, mode |
| `experiments` | reproducible runs | spec_hash, dataset_id, strategy_version, artifact_path |
| `experiment_trials` | optimizer trials | experiment_id, params, metrics, seed |
| `experiment_folds` | walk-forward folds | experiment_id, train/test ranges, params, metrics |
| `validation_runs` | gate evidence | experiment_id, gate, passed, details |
| `proposals` / `reviews` / `approvals` / `promotions` / `rollbacks` | self-improvement lineage | IDs, actor, rationale, hashes, timestamps |
| `audit_events` | append-only actions | actor, action, entity, before/after hash, timestamp |
| `system_events` | operational events | severity, source, code, details, timestamp |
| `alerts` | delivery/dedupe | rule, severity, state, delivered_at, dedupe_key |

### Indexing and integrity

- Unique: `(symbol, timeframe, candle_time)` for candles; `client_order_id` for orders;
  `(strategy_id, symbol, timeframe, candle_time)` for signals; experiment `spec_hash` where
  appropriate.
- Index: signal time/strategy; trades account/time; orders state/time; risk decisions time/allowed;
  equity account/time; alerts state/time.
- Foreign keys where SQLite supports them; enable `PRAGMA foreign_keys=ON`.
- UTC everywhere; store epoch seconds for market time and ISO-8601 UTC for system time.
- SQLite: WAL, busy timeout, periodic backup. Document PostgreSQL for multi-process deployments.

---

## 8. Configuration and modes

### Profiles

| Profile | Data | Orders | Persistence | Purpose |
|---|---|---|---|---|
| `dev` / `test` / `staging` | fixtures or live public data | paper | paper state only | aliases of `paper` for local/CI checks |
| `paper` | live public data | paper only | paper state only | default runtime |
| `testnet` | testnet/read fallback | testnet only | testnet state | execution mechanics |
| `research` | historical/snapshots | none | none | analysis and feature research |
| `backtest` | historical/snapshots | none | none | deterministic replay and study |
| `live` | live | permanently disabled | none | rejected at profile construction |

`ExecutionMode` is the canonical vocabulary. `TradingProfile` is immutable and rejects live mode;
`RiskManager` refuses to bind persistence in research/backtest; `ExecutionRouter` has no order
adapter for those modes.

### Startup validation

Refuse to start when:

- mode is unknown or live flags are inconsistent;
- paper/testnet uses a strategy/timeframe mismatch;
- risk limits are missing/non-finite/out of bounds;
- timeframes/symbols disagree across profile, feed, and persistence;
- secrets are required but absent, or default credentials are present in production;
- migrations are pending;
- a prior shutdown/reconciliation left an unresolved mismatch.

### Clock and determinism

All modules use an injectable UTC clock (`app/core/clock.py`). Backtests and tests use a fake clock.
Daily P&L boundaries, cooldowns, stale checks, and experiment timestamps must use the same clock.

---

## 9. Deployment topology

### Paper (target)

```text
docker compose:
  app        (FastAPI + paper runner, single writer, localhost bind behind proxy)
  dashboard  (Next.js, read-only proxy)
  soak       (optional profile; paper-only, own state partition)
```

SQLite initially; PostgreSQL optional when multiple writers/services are needed. Backups mount
outside the container. Logs rotate. Healthchecks cover API, data freshness, last cycle, and paper
reconciliation.

### Testnet

Same topology with testnet mode/keys, separate DB/state directory, and explicit venue labels in
every row/dashboard panel. No production order host is reachable.

### Live

Out of scope. If ever considered, it requires a separate deployment, separate credentials, a signed
readiness checklist, small-capital limits, native exchange-side stops, reconciliation, and a
kill-switch drill. It must not share the paper process or database.

---

## 10. Key architecture decisions (ADRs to record)

Phase 1 implements the contract, profile, mode, and adapter parts of ADRs 2, 4, 5, and 8. The
remaining decisions stay as roadmap until their persistence/research layers are implemented.

1. **Modular monolith over microservices** — simpler consistency, one writer, easy rollback.
2. **Pydantic contracts at boundaries** — dicts are not allowed across strategy/risk/execution.
3. **Persisted risk lifecycle** — risk is part of the trade state machine, not an in-memory helper.
4. **One order manager** — no direct broker calls from strategies, runners, or research.
5. **Backtest uses live feature/strategy/risk code** — only data source and broker differ.
6. **Research is read-only and separate** — import boundary + approval gate.
7. **Append-only audit memory** — experiments/approvals/orders are never silently mutated.
8. **Fail closed** — missing data/approval means no entry, not a default-permit.
9. **SQLite first, Postgres path** — optimize for a single-node paper system, keep migration open.
10. **No automatic live promotion** — strategy promotion and live authorization are separate.

---

## 11. What this architecture deliberately does not do

- It does not assume the current strategy has edge.
- It does not let AI choose risk limits or place orders.
- It does not hide failures behind synthetic defaults or silent fallbacks.
- It does not require a rewrite to reach the target; each boundary is introduced incrementally with
  the existing regression suite preserved.
