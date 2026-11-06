# Implementation Plan — BTC AI Trading System

**Plan date:** 2026-10-28
**Baseline:** `master` @ `91623ef` plus uncommitted instrument/watchlist work
**Baseline tests:** 366/366 before Phase 1; 376/376 after Phase 1; dashboard production build and type-check passing
**Current mode:** paper trading only; live execution disabled
**Related documents:** [`PROJECT_AUDIT.md`](PROJECT_AUDIT.md), [`ARCHITECTURE.md`](ARCHITECTURE.md), [`SECURITY_AND_RISK.md`](SECURITY_AND_RISK.md)

---

## 1. How this plan is sequenced

The repository is a working prototype. The goal is an incremental, testable evolution, not a
big-bang rewrite. Each phase:

- keeps the existing test suite green;
- adds tests before or with behavior changes;
- keeps live execution unreachable;
- introduces adapters around existing modules rather than deleting them;
- ends at a review gate with objective acceptance evidence.

Priority order is deliberate:

1. **Correctness and safety of the existing paper system.**
2. **Trustworthy data and backtest evidence.**
3. **Persistence, observability, and dashboard truth.**
4. **Clean execution/portfolio abstractions.**
5. **Read-only AI research.**
6. **Human-approved self-improvement.**
7. **Operational hardening.**

AI and self-improvement come last because they are unsafe on top of incorrect risk state,
untrustworthy metrics, or an unauthenticated execution surface.

### Non-negotiable rules

- **Paper by default.** No real order may be placed by this plan.
- **Risk is deterministic and outside AI control.** AI may propose; it may never change limits,
  bypass checks, or call an order client.
- **Fail closed.** Missing, stale, malformed, or unapproved data/signals reject the trade.
- **One execution path.** Every order (paper, testnet, future live) goes through one risk-gated order
  manager.
- **Reproducibility.** Every result must have dataset identity, code/strategy version, parameters,
  costs, and command.
- **Human approval.** No strategy promotion or limit change is automatic.
- **No secrets in code, logs, tests, or documents.**
- **Small PRs.** Each item below is independently reviewable and reversible.

---

## 2. Phase 0 — Audit and baseline (complete)

| Deliverable | Status | Evidence |
|---|---|---|
| Repository/module audit | Done | `docs/PROJECT_AUDIT.md` |
| Test baseline | Done | 366 passed, 6 warnings, 24.08s |
| Python compile check | Done | `python -m compileall -q app` OK |
| Dashboard build/type-check | Done | Next.js 15.5.25 build passed |
| Architecture/security design | Done | `ARCHITECTURE.md`, `SECURITY_AND_RISK.md` |
| Application code changes | None (by design) | Worktree unchanged apart from new docs |

**Exit gate:** user approval to begin Phase 1.

---

## 3. Phase 1 — Safety hardening of the existing paper system

**Goal:** make the current paper pipeline fail closed, stateful across restarts, and consistent with
the validated 1H Strategy A. No new AI, no new broker, no live trading.

**Dependencies:** none beyond current stack.
**Estimated scope:** 4–6 small PRs.

### 1.1 Validated signal contract (modules 10, 19)

**Existing:** free-form `dict` produced by `app/strategy/signal_engine.py`, consumed by
`PaperTrader.on_signal` and `Executor`.

**Changes/new files:**

- `app/strategy/signal.py`: Pydantic `Signal` / `SignalSide` with required
  `strategy_id`, `strategy_version`, `symbol`, `timeframe`, `candle_time`, `created_at`,
  `direction` (`long|short|none`), `score`, `confidence`, `entry`, `stop_loss`, `target`,
  `accepted_size`, `risk_allowed`, `risk_reason`, `data_quality`.
- `app/strategy/validation.py`: reject missing/NaN/inf, non-positive prices/size, stop on the wrong
  side, target on the wrong side, stale candle, symbol/timeframe mismatch, and
  `risk_allowed is not True`.
- `PaperTrader.on_signal` accepts only a validated signal; remove `risk_allowed=True` default and
  remove fabricated 1% stop/target fallbacks.
- `SignalEngine` emits an explicit `none` signal with `risk_allowed=False` when risk is unavailable or
  rejected.
- `Signal` DB model gains `strategy_id`, `strategy_version`, `risk_reason`, `data_quality`
  (schema change; see 1.7).

**Tests:**

- `tests/test_signal_validation.py`: each rejection reason; risk false; missing risk; NaN; wrong-side
  stop/target; stale candle; wrong symbol; zero/negative size.
- `tests/test_m6_paper_engine.py` additions: paper never opens on an incomplete or unapproved signal;
  valid signal still opens.
- `tests/test_signal_engine.py` additions: no risk manager => `risk_allowed=False`.

**Acceptance:**

- No code path can open a paper position without an explicit, validated `risk_allowed=True` and a
  valid risk decision object.
- No arbitrary stop/target is invented.
- Existing behavior for valid signals is unchanged.

### 1.2 Risk lifecycle and persistence (module 15)

**Existing:** `RiskManager` is in-memory; paper closes never call `record_trade`; `open_positions` is
never incremented on paper entry; restart resets daily P&L, peak equity, and halt state.

**Changes/new files:**

- `app/risk/state.py`: `RiskState` snapshot (`day`, `day_pnl`, `peak_equity`, `realized_pnl`,
  `open_positions`, `trades_today`, `last_trade_bar_index`, `halted`, `halt_reason`).
- `RiskManager.restore(snapshot)`, `RiskManager.snapshot()`,
  `RiskManager.on_position_opened()`, `RiskManager.on_trade_closed(pnl, bar_index)`.
- `risk_state` table (single row per mode/account) persisted after every mutation and hydrated during
  startup.
- `PaperTrader` returns a structured close event (pnl, reason, entry/exit, timestamps); runners must
  pass it to the risk engine exactly once.
- Wire `RiskManager` into `PaperTrader` (or a thin coordinator) so the lifecycle cannot be skipped by
  a caller.
- Sync main app and soak runner with the restored account; remove duplicated ad-hoc sync.
- `day_pnl` reset at UTC day boundary; use a single clock source.

**Tests:**

- `tests/test_risk_lifecycle.py`: open/close updates position count, realized/day P&L, peak;
  daily-loss and drawdown gates trigger after losses; halt persists; no double counting on restart.
- Restart test: create losses, recreate app/risk/paper, verify equity, peak, day P&L, halt, and sizing
  match the pre-restart state.
- Test both main runner and soak runner.

**Acceptance:**

- Daily-loss and drawdown checks demonstrably fire in the paper pipeline.
- Restart does not reset or duplicate risk state.
- Position count is correct for the single-position case and ready for portfolio expansion.

### 1.3 Single source of truth for symbol/timeframe/strategy (modules 9, 10, 26)

**Existing:** `settings.trading_timeframe=5m`; `TradingRunner` persists hard-coded
`BTCUSDT`/`5m`; Strategy A is 1H; analysis/study use `WINNER` separately.

**Changes/new files:**

- `app/config/trading.py` (or `app/strategy/profile.py`): validated `TradingProfile` containing
  symbol, timeframe, strategy_id/version, parameter set, cost assumptions, risk profile, and mode.
- `Strategy A` becomes the explicit paper default (1H, WINNER parameters), or the default is changed
  to a profile that is explicitly labeled experimental. The README, dashboard, persisted `Signal`,
  and runner must all display the same profile.
- `TradingRunner` and `SoakRunner` read the profile; no hard-coded symbol/timeframe.
- Persist the profile in `strategy_versions`/`signals` so old rows are interpretable.
- Add startup validation: unknown timeframe, mismatched profile, or Strategy A on non-1H refuses to
  start.

**Tests:**

- Config/profile contract tests; runner persistence test asserts the configured symbol/timeframe.
- A test that the paper default equals the documented Strategy A profile.
- Startup failure test for invalid combinations.

**Acceptance:**

- `python -m app`, soak, dashboard, and database all report one consistent profile.
- No `"5m"` hard-code remains outside explicit test/config fixtures.

### 1.4 Required pre-trade controls (module 15)

**Existing:** confidence, daily loss (ineffective), drawdown (ineffective), max positions (not
updated), volatility; `cooldown_bars` unused.

**Changes/new files:**

- Extend `RiskSettings` with conservative defaults:
  `max_order_notional_pct`, `max_daily_trades`, `cooldown_bars` (enforced), `max_spread_bps`,
  `max_slippage_bps`, `allowed_sessions` (UTC), `require_streaming_data`,
  `max_candle_age_secs` (resolution-aware), `max_positions`, `max_gross_exposure_pct`,
  `max_symbol_exposure_pct`.
- `RiskDecision` object with per-check results for auditability.
- Enforce in `RiskManager` and re-check in the paper/order boundary.
- Add duplicate-signal guard keyed by `(strategy_id, symbol, timeframe, candle_time)`.
- Data-freshness gate uses the feed snapshot, not only candle timestamps.
- Kill-switch check in every entry decision.

**Tests:**

- One test per limit, including boundary values and fail-closed behavior.
- Duplicate candle cannot open twice.
- Stale/non-streaming data rejects entry.
- Cooldown blocks immediate re-entry and allows after N bars.
- Session filter uses UTC.

**Acceptance:**

- Every limit listed above is enforced and covered by a test.
- Rejection reasons are persisted/visible.

### 1.5 Execution boundary and kill switch (modules 15, 19, 21, 23)

**Existing:** `Executor` trusts caller size; `DeltaClient` has no internal host guard; no kill switch.

**Changes/new files:**

- `app/execution/guard.py`: mandatory pre-trade check requiring a validated `RiskDecision`; checks
  mode flags, symbol, freshness, kill switch, duplicate key, and size equality.
- `Executor.execute` refuses to place any order without a guard token/decision; paper path consumes
  the same decision.
- `app/risk/kill_switch.py`: persisted halt state with `halt`, `resume` (explicit human action),
  `status`; checked by runner/executor. `resume` requires an admin token if exposed via API.
- `python -m app.risk.kill_switch --halt --reason ...` CLI; default API disabled unless
  `ADMIN_TOKEN` is configured and the request is authenticated.
- Minimal API exposure fix in the same phase: bind `127.0.0.1` by default and require `ADMIN_TOKEN`
  for watchlist mutations (full auth/rate limiting remains Phase 7).
- Startup seeding for `TradingRunner` (persist last processed candle in `runner_state`; on first
  cycle do not trade until a new candle arrives).
- Paper reconciliation on startup: compare restored position/account with trade history; record any
  mismatch instead of silently continuing.

**Tests:**

- `tests/test_execution_guard.py`: no risk decision => no paper/testnet/live order; size mismatch
  rejected; kill switch rejects; duplicate rejected.
- `tests/test_runner_restart.py`: startup seeds and does not auto-enter; next new candle may trade.
- `tests/test_kill_switch.py`: halt persists across restart; resume requires explicit token; loop
  stops entering.
- Reconciliation test with a deliberately inconsistent DB.

**Acceptance:**

- No order path bypasses the guard.
- Restart never auto-enters.
- Kill switch stops new entries immediately and survives restart.

### 1.6 Data-freshness/provenance gate (modules 1, 3)

Phase 1 minimum (full validator in Phase 2):

- Record `data_status`, `price_valid`, `source`, `last_candle_age_secs`, and symbol/timeframe on every
  decision.
- Reject paper entry if data is not streaming, price is invalid, candle is stale for the configured
  timeframe, or the feed's symbol/timeframe differs from the profile.
- Mark fallback-sourced rows; never claim testnet origin for production-sourced reads.

**Tests:** stale feed, invalid price, mismatched timeframe, fallback source label.

**Acceptance:** no entry on stale/invalid/mismatched data; provenance is visible in the decision
record.

### Phase 1 review gate

All must hold:

- [x] 376 existing tests pass (updated where the contract intentionally changed).
- [x] New safety tests pass, including fail-closed and restart cases.
- [x] Paper default is the documented strategy profile.
- [x] No open P1 finding that affects paper entry, risk, restart, or profile consistency.
- [ ] Full API auth/TLS and the complete data-quality validator are scheduled with live disabled.
- [x] README and docstrings match actual behavior.
- [x] Live mode remains unreachable; no secrets added.

**Phase 1 verification:** `python -m compileall -q app tests` OK; `pytest -q` -> 376 passed,
6 warnings; dashboard `npm run build` passed. Admin-token auth is implemented for control
endpoints; TLS is documented as a reverse-proxy deployment requirement. No testnet credentials
were available, so no live/testnet order round-trip was performed.

---

## 4. Phase 2 — Trustworthy data and validation

**Goal:** make every performance number reproducible, timeframe-correct, and data-provenance aware.

**Dependencies:** Phase 1. Add `alembic` (Phase 3 can land the migration runner; Phase 2 may use
temporary `create_all` only if explicitly documented).

### 2.1 Unified data-quality validator (modules 1, 2, 3)

**Existing:** scattered inline checks; no OHLCV invariants, gap detection, or per-row status.

**Changes/new files:**

- `app/data/quality.py`: `DataQualityReport` and checks:
  monotonic time, duplicate timestamps, OHLCV invariants, non-positive volume, missing bars/gaps,
  stale last bar, outlier/zero-range candles, symbol/timeframe consistency.
- Ingestion path writes `source`, `ingested_at`, `quality_status`, `quality_flags` on `Candle`;
  invalid rows are quarantined, not silently traded.
- `Dataset`/`dataset_versions` metadata: symbol, timeframe, range, row count, gap count, hash,
  source, created_at.
- A single `MarketDataSnapshot`/`DataQuality` object is passed to strategies and risk.

**Tests:** synthetic bad rows for every invariant; gap detection at timeframe boundaries; duplicate
handling; dataset hash stability; testnet/production fallback labeling.

**Acceptance:** no decision can consume data that failed validation; dataset identity is reproducible.

### 2.2 Cache, rate limiting, and resilience (module 1)

- `app/data/cache.py`: TTL cache for products/tickers/candles keyed by symbol/timeframe/range.
- Token-bucket rate limiter shared by REST reads; honor `Retry-After`; add jitter and capped backoff;
  circuit breaker with health score.
- Expose cache/rate-limit/venue-source metrics in health.
- Never cache order/account reads for execution decisions beyond a short, explicit TTL.

**Tests:** 429 handling, Retry-After, circuit open/half-open, cache hit/miss, fallback source labels.

**Acceptance:** dashboard polling cannot exhaust the rate limit; retries are bounded and observable.

### 2.3 Feature and indicator completeness (modules 4, 5)

- Add SMA, Bollinger Bands, volume/volatility features, and regime features to the live feature set
  only after each has a causal unit test.
- Add `FeatureSet` version and parameters to every signal/backtest record.
- Optional feature snapshot persistence keyed by candle time and feature version.
- Regime labels used in research must be computed point-in-time; full-window labels may be used only
  for descriptive post-hoc analysis and must be labeled as such.

**Tests:** causality (changing future rows cannot change past features); formula fixtures; version
provenance.

**Acceptance:** signal/backtest rows identify the exact feature set and parameters.

### 2.4 News and event processing (module 6)

- Persist raw headlines (source, published_at, fetched_at, URL, hash) with dedupe.
- Add source allowlist, timeout, and failure isolation.
- Optional economic-calendar ingestion behind a provider interface; no scraping assumptions.
- Sentiment features must be timestamped and point-in-time; no future headlines.

**Tests:** dedupe, timestamp ordering, provider failure does not block trading, no future data.

**Acceptance:** news is reproducible and optional; its absence degrades gracefully.

### 2.5 Backtest correctness (module 11)

- `bars_per_year(timeframe)` helper; use it for Sharpe/Sortino; include flat bars in the return
  series; use log or simple returns consistently.
- Enter at next-bar open (or a documented, modeled delay), never at the signal-bar close.
- Mark-to-market equity curve using close; compute unrealized drawdown.
- Charge entry and exit fees exactly once; mark and charge the final open position.
- Add funding, session gaps, optional partial-fill/participation model, and order rejection.
- Add metrics: CAGR, expectancy, exposure/time-in-market, turnover, average holding bars, cost
  totals, benchmark buy-and-hold, and cost-sensitivity table.
- Preserve the current `BacktestResult` API where practical; add fields rather than break callers.

**Tests:** hand-calculated fixtures for fees, P&L, CAGR, Sharpe on 1H and 5m, flat-bar handling,
final open position, next-bar entry, no look-ahead, funding/cost sensitivity.

**Acceptance:** 1H and 5m metrics are annualized correctly; tests prove accounting identities; results
are not optimistic relative to the documented execution assumptions.

### 2.6 Real walk-forward and optimization (modules 12, 13, 24)

- `app/backtest/walkforward.py`: rolling/expanding train-test folds, re-fit parameters inside each
  train window, evaluate untouched test windows, record fold boundaries, parameters, trades, metrics,
  and stability.
- `app/backtest/optimize.py`: reusable random/grid optimizer with parameter bounds, max trials,
  deterministic seed, objective, minimum-trade and drawdown constraints; store every trial.
- Purged/embargoed folds for overlapping labels if labels are used.
- No hard-coded `"VALIDATED"` status. Status derives from a stored validation run; otherwise
  `"UNVALIDATED"`.
- Produce `artifacts/<experiment_id>/` with config, dataset hash, code version, folds, trades,
  metrics, and command.

**Tests:** leakage test (OOS data cannot influence fit), fold coverage, deterministic seed, parameter
stability output, status derivation, artifact contents.

**Acceptance:** one command reproduces the same result hash on the same dataset/config; no strategy is
labeled validated without a linked artifact.

### Phase 2 review gate

- [ ] Unified validator gates ingestion and decisions.
- [ ] Correct 1H/5m annualization and complete accounting covered by hand-calculated tests.
- [ ] Next-bar execution, final-position fees, and mark-to-market drawdown implemented.
- [ ] Real rolling walk-forward and trial registry implemented.
- [ ] Experiment artifacts are reproducible; hard-coded `"VALIDATED"` removed.
- [ ] Data provenance/source labels visible in DB and API.

---

## 5. Phase 3 — Persistence, observability, and dashboard truth

**Goal:** make the system operable and auditable without reading source code.

**Dependencies:** Phase 2 dataset/experiment models.

### 3.1 Migrations and schema (module 24)

- Add Alembic with a baseline migration matching the current schema (including uncommitted
  watchlist tables).
- Add tables/indexes:
  `orders`, `fills`, `risk_decisions`, `risk_state`, `equity_snapshots`, `strategy_versions`,
  `runner_state`, `experiments`, `experiment_trials`, `experiment_folds`, `validation_runs`,
  `dataset_versions`, `audit_events`, `system_events`, `alerts`.
- Backfill existing paper/testnet rows where practical; otherwise document retention.
- Enable SQLite WAL/busy timeout; document PostgreSQL path.
- Test upgrade from current schema and from empty DB; test downgrade where safe.

**Acceptance:** `alembic upgrade head` works on a copy of the current DB; no ad-hoc `ALTER TABLE` at
startup.

### 3.2 Order/fill/risk audit trail (modules 18, 19, 21, 22)

- Paper order state machine: `created -> accepted -> filled/partially_filled/rejected/cancelled`;
  every transition persisted with idempotency key.
- Persist risk decisions (allowed/rejected + reasons) before any order.
- Persist equity snapshots each cycle and on close.
- Startup reconciliation for paper: compare account/position/trades/orders; report and halt on
  material mismatch.
- Testnet reconciliation remains; unify its event model with paper orders where practical.

**Acceptance:** a complete trade can be reconstructed from DB rows alone; restart reconciliation
reports zero unexplained drift in tests.

### 3.3 Logging, metrics, and alerts (modules 22, 23)

- Structured JSON logs with correlation IDs (cycle, signal, order), rotation, and retention.
- Metrics endpoint: cycle latency, data age, signal counts, reject reasons, order latency/failures,
  process uptime, paper equity/P&L.
- Alert adapter interface (log/webhook/Telegram/email) with dry-run and dedupe.
- Alerts: drawdown breach, daily-loss breach, stale feed, repeated rejection, reconciliation
  mismatch, kill switch, process heartbeat missing, API errors.
- Alert delivery failures must not block trading but must be logged.

**Acceptance:** each alert has a deterministic test with a fake sink; kill switch and drawdown
breach are visible in the API/dashboard.

### 3.4 Dashboard (module 25)

- Equity curve + drawdown chart (SVG/React, no mandatory chart dependency).
- Metrics: return, CAGR, Sharpe (label timeframe), Sortino, max DD, win rate, profit factor,
  expectancy, exposure, turnover, fees/slippage totals.
- Backtest/walk-forward/experiment pages with dataset hash, parameters, folds, provenance, and
  "paper" labeling.
- System health: data source/age, feed status, strategy heartbeat, last signal, risk halt, open
  position, order rejections, alerts.
- Kill-switch control behind authenticated admin access; read-only by default.
- Keep the existing SIMULATED/paper labeling prominent.

**Acceptance:** dashboard build/type-check passes; API contract tests cover new fields; no page claims
live/real performance for paper data.

### Phase 3 review gate

- [ ] Migrations are the only schema path.
- [ ] Full order/fill/risk/equity audit trail.
- [ ] Restart reconciliation tested.
- [ ] Alerts and kill switch are observable.
- [ ] Dashboard answers: is data healthy, is risk within limits, what did the strategy do, and is the
      result reproducible?

---

## 6. Phase 4 — Execution and portfolio architecture

**Goal:** one broker-neutral, risk-gated path and multi-asset portfolio management. Live stays
disabled.

**Dependencies:** Phase 3 order/risk persistence.

### 4.1 Broker interface and order manager (modules 17, 18, 20)

- `app/execution/broker.py`: `Broker` protocol (`place_order`, `cancel_order`, `get_order`,
  `get_positions`, `get_balances`, capabilities, venue/source identity).
- Adapters: `PaperBroker`, `DeltaTestnetBroker`, future `DeltaLiveBroker` (disabled).
- `app/execution/order_manager.py`: sole order entry point; mandatory risk decision, idempotency,
  state machine, retries, timeouts, reconciliation.
- Keep `DeltaClient` as a transport; add a host interlock so production order hosts are unreachable
  unless an explicit, separate live adapter is constructed.
- Live adapter startup refuses unless all flags, keys, confirmation, and a signed live-readiness
  record are present. It is never enabled by this plan.

**Acceptance:** every order in code and tests flows through the order manager; fake-broker tests
cover rejection, timeout, partial fill, cancel, restart recovery; live remains off.

### 4.2 Portfolio management (module 16)

- `app/portfolio/manager.py`: target weights, max gross/net exposure, per-symbol caps, correlation
  cap, max positions, cash reserve, rebalance policy, and conflict resolution.
- Single-position behavior remains the default until multi-asset tests pass.
- Integrate watchlist as an input, not an implicit trading list.

**Acceptance:** exposure caps and allocation are enforced and tested; paper account still reconciles;
no cross-symbol duplicate/oversized orders.

---

## 7. Phase 5 — Read-only AI research agents

**Goal:** add the five requested research agents and orchestrator without granting execution or
risk authority.

**Dependencies:** Phase 2 artifacts; Phase 3 experiment storage; optional LLM/network credentials
(blocker if unavailable).

### 5.1 LLM provider layer (module 8)

- `app/research/llm.py`: provider protocol with OpenAI/Anthropic/local (Ollama/HTTP) adapters.
- Config: model, temperature, max tokens, timeout, retries, budget. Keys from env/secret manager only.
- Log token/cost/latency and request IDs; never log prompts containing secrets or full keys.
- Offline stub provider for tests; no network in CI.
- Structured outputs validated by Pydantic; invalid output is rejected, not coerced.

### 5.2 Agents and orchestrator (modules 7, 8)

- `MarketAnalystAgent`: summarizes market state, data quality, regime, news; cites input timestamps.
- `StrategyResearcherAgent`: proposes hypotheses and parameter ranges within allowed bounds.
- `CodeAgent`: produces patch proposals/diffs in a sandbox; cannot apply or commit.
- `ResearchReviewerAgent`: checks leakage, overfitting, sample size, cost assumptions, and
  contradictions; can veto.
- `PerformanceAnalystAgent`: analyzes paper/backtest/experiment results and proposes diagnostics.
- `ResearchOrchestrator`: schedules a pipeline, stores inputs/outputs, enforces schema and policy,
  and requires human approval for any next step.

**Hard boundaries:**

- No import of execution/broker/order modules by research code.
- No write access to risk settings, strategy registry, or live config.
- All proposals are data rows/artifacts; nothing is auto-applied.

**Tests:** fixture-driven outputs for each agent; schema rejection; prompt-injection resistance
(headlines/news treated as untrusted data); no network; no order/risk imports; budget enforcement;
human-approval gate.

**Acceptance:** offline deterministic tests pass; an AI proposal cannot change runtime behavior
without a separate reviewed change.

---

## 8. Phase 6 — Self-improvement with human approval

**Goal:** a safe proposal → validation → review → approval → canary → promotion/rollback loop.

**Dependencies:** Phases 2–5.

### 6.1 Experiment and proposal registry (modules 9, 13, 14, 24)

- Append-only `experiments`, `proposals`, `reviews`, `approvals`, `promotions`, `rollbacks` with
  lineage: code version, dataset hash, feature set, params, costs, seed, command, artifacts.
- Strategy registry with immutable versions and explicit status derived from validation runs.
- Promotion pins a strategy version; rollback restores the previous pinned version.

### 6.2 Gates

A proposal must pass all of:

1. Data-quality and dataset-identity checks.
2. In-sample, OOS, and rolling walk-forward results.
3. Minimum trade count and minimum fold coverage.
4. Max drawdown / cost-sensitivity constraints.
5. Reviewer-agent critique with no unresolved veto.
6. Human approval recorded with identity, timestamp, and rationale.
7. Paper canary for a configured duration/trade count with no risk breach.
8. Reconciliation and alerting healthy.

Only then may a version be promoted. Promotion changes paper configuration first; live remains out of
scope.

**Tests:** rejected low-quality proposal, failed OOS, failed reviewer veto, missing approval, canary
breach, successful promotion, rollback, audit trail completeness, deterministic replay.

**Acceptance:** no path promotes automatically; rollback restores the prior version; every promotion
is explainable from stored artifacts.

---

## 9. Phase 7 — Operational hardening (paper-first)

**Goal:** make a VPS deployment safe to operate. This is not a live-trading authorization.

### 7.1 Security and configuration (module 26)

- Authentication for mutation/admin endpoints; read-only API keys where possible.
- Rate limiting and request size limits; CORS allowlist per environment.
- Bind `127.0.0.1` by default behind a reverse proxy; explicit opt-in for public bind.
- Secret manager/env-only secrets; startup refusal on default/empty production secrets.
- Environment profiles: `dev`, `test`, `paper`, `staging`, `live` (live refused without a separate
  signed checklist).
- Pin Python dependencies and add a lock file; run dependency audit; add `ruff`/`mypy` and a coverage
  threshold.

### 7.2 Deployment and recovery (modules 22, 23, 25)

- Dashboard container/service and health checks; app healthcheck; restart policy.
- Structured log rotation and retention; disk-space alerts.
- Automated SQLite backup/restore test; document PostgreSQL migration path.
- Soak runner as an explicit service/profile with its own health and alerts.
- CI: tests, type-check/build, lint, security scan, migration check.
- Runbooks: start/stop, kill switch, drawdown incident, stale data, broker outage, DB restore,
  credential rotation, rollback.

**Acceptance:** restore drill succeeds; alerts fire in staging; unauthorized mutation is rejected;
CI is green on a clean checkout; live remains off.

---

## 10. Requested-module work-package map

Every module from the request appears below. Statuses refer to the **target** plan, not current code.

| # | Module | Phase | Existing files | Required changes / new files | Key dependencies | Tests | Acceptance |
|---|---|---|---|---|---|---|---|
| 1 | Market Data Engine | 1–2 | `app/data/market_data.py`, `websocket.py`, `delta_client.py`, `market_health.py` | provenance/source, resolution-aware freshness, cache, rate limiter, provider interface | none | freshness, source, 429, cache, circuit breaker | no stale/mismatched data used for decisions |
| 2 | Historical Data Manager | 2 | `app/data/historical.py`, `resample.py`, `Candle` | dataset versions/hash, gaps, ingest time, quality state | migrations | provenance, hash, gap, resample | reproducible dataset identity |
| 3 | Data Quality Validator | 1–2 | inline checks | `app/data/quality.py`, single report passed to strategy/risk | none | invariant/gap/stale/duplicate tests | fail-closed ingestion and decisions |
| 4 | Feature Engineering | 2 | `indicators/technical.py`, `backtest/regimes.py` | feature-set version, snapshot/provenance, causal regime labels | dataset versions | causality/formula/version | signals identify exact features |
| 5 | Indicator Engine | 2 | `indicators/technical.py` | SMA, Bollinger, volume/volatility/regime features | feature tests | formula/causality | coverage of required features |
| 6 | News and Event Processing | 2, 5 | untracked `app/data/news.py`, sentiment agent | persistence, dedupe, provider interface, economic calendar, point-in-time | optional news API | dedupe/time/failure/prompt-injection | reproducible, optional, timestamped |
| 7 | AI Research Agents | 5 | none (signal agents are deterministic) | five agents under `app/research/agents/` | LLM layer | offline fixtures, schema, policy | read-only, human-reviewed |
| 8 | AI Orchestrator | 5 | settings keys only | `app/research/orchestrator.py`, provider layer, approval gate | LLM/network optional | schema/budget/approval/offline | no execution/risk authority |
| 9 | Strategy Registry | 1, 6 | `analysis/strategies.py`, `instruments/strategy_compat.py` | immutable versions, derived status, promotion/rollback, lineage | DB/artifacts | version/status/promotion/rollback | no hard-coded "VALIDATED" |
| 10 | Strategy Execution Engine | 1 | `signal_engine.py`, `trading/runner.py`, `soak/runner.py` | validated `Signal`, profile, startup seed, one execution path | Phase 1 | profile/restart/fail-closed | consistent paper strategy |
| 11 | Backtesting Engine | 2 | `backtest/engine.py` | next-bar entry, MTM, fees, timeframe metrics, funding/sessions/partial fills, benchmark | quality/data | hand-calculated fixtures | trustworthy, reproducible results |
| 12 | Walk-Forward Analysis | 2 | `backtest/study.py` | rolling re-fit, folds, artifacts, leakage guards | optimizer/experiments | leakage/fold/artifact | real OOS evidence |
| 13 | Optimization Engine | 2, 6 | `study.py` random scan | `backtest/optimize.py`, trial registry, bounds, deterministic seed | experiments | determinism/constraints | no untracked tuning |
| 14 | Self-Improvement Engine | 6 | none | proposal → validate → review → approve → canary → promote/rollback | phases 2–5 | gate/rollback/audit | human-approved only |
| 15 | Risk Management | 1 | `risk/risk_manager.py`, `config.py`, `position_sizer.py` | lifecycle, persistence, required limits, kill switch, risk decisions | DB | every limit/restart/halt | deterministic, fail-closed, auditable |
| 16 | Portfolio Management | 4 | single-position `PaperTrader` | `app/portfolio/manager.py`, exposure/correlation/allocation | order manager | allocation/exposure/conflict | multi-asset safe |
| 17 | Broker Integration | 4 | `delta_client.py`, `execution/client.py` | `broker.py` protocol, adapters, host interlock, capabilities | order manager | fake broker/venue guard | one broker-neutral path |
| 18 | Order Manager | 3–4 | `trading/orders.py`, testnet loop/recovery | persistent state machine, idempotency, cancel/timeout/reconcile | DB/migrations | lifecycle/restart/reconcile | every order auditable |
| 19 | Paper Trading Engine | 1, 3 | `trading/paper_trader.py`, paper models | validated signal, risk lifecycle, order/fill/equity persistence, startup reconciliation | Phase 1/3 | fail-closed/restart/reconcile | state fully reconstructable |
| 20 | Live Execution Adapter | 4 (disabled) | `trading/executor.py` | live adapter behind signed readiness gate + order manager; remains off | phases 3–4 | refusal/guard tests only | impossible under defaults |
| 21 | Trade Reconciliation | 1, 3 | `execution/recovery.py` (testnet) | unified paper/testnet reconciliation at startup and periodically | DB/orders | drift/mismatch tests | no silent divergence |
| 22 | Logging and Analytics | 3 | logging, JSONL telemetry | structured logs, correlation IDs, metrics, retention | none | log schema/metrics | operable and auditable |
| 23 | Monitoring and Alerting | 1, 3 | `/health`, `/monitor` | kill switch, alert adapter, heartbeat/stale/rejection/drawdown alerts | Phase 3 | fake-sink tests | incidents visible and actionable |
| 24 | Database and Experiment Memory | 2–3 | `database/models.py`, `database.py` | Alembic, experiment/audit/equity/order tables, indexes, SQLite/WAL | alembic | migration/restore/query tests | durable, versioned memory |
| 25 | Web Dashboard | 3, 7 | `dashboard/` | equity/DD/metrics, backtests/experiments, health/alerts, authenticated admin | API/phase 3 | build/type/API contract | truthful, complete observability |
| 26 | Configuration and Secrets | 1, 7 | `config/settings.py`, `.env.example` | profiles, startup validation, secret policy, pinned deps, auth/rate limit | none | profile/refusal/secret tests | safe by default |

---

## 11. Immediate next changes (after approval)

These are the first small changes; they do not require new AI or broker work.

### PR 1 — Fail-closed signal contract

- Add `app/strategy/signal.py`, `app/strategy/validation.py`.
- Make `PaperTrader` reject invalid/unapproved signals; remove fallback stop/target and
  `risk_allowed` default.
- Update engine output and DB signal fields; add tests.
- **Gate:** no paper open without a validated risk decision; all tests pass.

### PR 2 — Risk lifecycle and restart correctness

- Add `RiskState`, persistence, hydrate/snapshot, open/close hooks.
- Wire both runners; record P&L exactly once; update peak/day/positions.
- Test daily-loss/drawdown firing and restart continuity.
- **Gate:** risk gates demonstrably work; no double counting.

### PR 3 — One profile, startup seed

- Introduce `TradingProfile`; default paper to Strategy A 1H (or explicitly relabel the experimental
  profile); remove hard-coded symbol/timeframe; persist profile/version.
- Add runner startup seeding/idempotency; test restart.
- **Gate:** config, DB, runner, dashboard agree; no auto-entry on restart.

### PR 4 — Required limits and kill switch

- Enforce cooldown, max trades/day, max order notional, exposure, spread/slippage, sessions,
  freshness, duplicate keys.
- Add persisted kill switch and CLI; authenticated resume only.
- **Gate:** one test per control; halt survives restart.

### PR 5 — Execution guard and paper reconciliation

- Add `ExecutionGuard`; require risk decision at every order boundary; reconcile paper state on
  startup.
- **Gate:** no bypass; mismatch halts/reports.

After these, proceed to Phase 2. Do not start the AI/self-improvement modules before Phases 1–3 are
complete.

---

## 12. Implementation checklist

### Phase 1 — safety

- [x] 1.1 Validated signal model + fail-closed paper boundary
- [x] 1.2 Risk state persistence, lifecycle hooks, restart restore
- [x] 1.3 Single trading profile (symbol/timeframe/strategy/params)
- [x] 1.4 Max order/daily trades/cooldown/exposure/spread/slippage/session/freshness checks
- [x] 1.5 Execution guard, kill switch, startup seed, paper reconciliation
- [x] 1.6 Data-quality/provenance minimum gate
- [x] 1.7 README/docstring reconciliation and regression tests

### Phase 2 — validation

- [x] 2.1 Unified `DataQualityValidator` + ingestion quarantine
- [x] 2.2 Dataset versions/hash/provenance
- [x] 2.3 TTL cache, rate limiter, Retry-After, circuit breaker
- [x] 2.4 Feature-set versioning + causal feature tests
- [x] 2.5 SMA/Bollinger/volume/volatility/regime features
- [x] 2.6 News persistence/dedupe/provider abstraction
- [x] 2.7 Timeframe-correct Sharpe/Sortino and full-bar returns
- [x] 2.8 Next-bar execution, MTM equity, fees on entry/exit/final position
- [ ] 2.9 Funding/session/partial-fill/cost-sensitivity models
- [x] 2.10 CAGR/expectancy/exposure/turnover/benchmark metrics
- [x] 2.11 Rolling walk-forward with re-fit and fold artifacts
- [x] 2.12 Reusable optimizer with trial registry and deterministic seed
- [x] 2.13 Remove hard-coded "VALIDATED"; derive from artifacts
- [x] 2.14 Reproducible experiment artifact bundle

### Phase 3 — persistence/observability

- [ ] 3.1 Alembic baseline + tables/indexes + SQLite WAL
- [x] 3.2 Order/fill/risk-decision/equity audit trail
- [x] 3.3 Paper reconciliation and restart tests
- [x] 3.4 Structured logs + correlation IDs + metrics
- [ ] 3.5 Alert adapter and alert rules
- [x] 3.6 Dashboard equity/DD/metrics
- [x] 3.7 Dashboard backtest/walk-forward/experiment views
- [ ] 3.8 Dashboard health/alerts/kill switch
- [x] 3.9 API contract tests + build/type-check

### Phase 4 — execution/portfolio

- [x] 4.1 Broker protocol + paper/testnet adapters + host interlock
- [x] 4.2 Persistent order manager as sole entry point
- [x] 4.3 Portfolio/exposure/correlation manager
- [x] 4.4 Live adapter refusal tests (still disabled)

### Phase 5 — AI research

- [x] 5.1 LLM provider interface + offline stub + budget/secret policy
- [x] 5.2 Market Analyst agent
- [x] 5.3 Strategy Researcher agent
- [x] 5.4 Code Agent (proposal-only sandbox)
- [x] 5.5 Research Reviewer agent
- [x] 5.6 Performance Analyst agent
- [x] 5.7 Orchestrator + schema validation + human approval
- [x] 5.8 Prompt-injection and policy tests

### Phase 6 — self-improvement

- [x] 6.1 Experiment/proposal/review/approval registry
- [x] 6.2 Validation gates and canary
- [x] 6.3 Promotion/rollback with version pinning
- [x] 6.4 Audit trail and replay tests

### Phase 7 — operations

- [x] 7.1 Auth, rate limiting, CORS, localhost bind
- [x] 7.2 Environment profiles and startup safety validation
- [x] 7.3 Dependency pinning/lock, ruff/mypy/coverage, CI
- [x] 7.4 Dashboard/soak services + healthchecks
- [x] 7.5 Log rotation, backups, restore drill
- [x] 7.6 Runbooks and incident procedures
- [x] 7.7 Separate live-readiness review (no automatic live enablement)

---

## 13. Definition of done (every PR)

- [ ] Tests added/updated; full suite green.
- [ ] Dashboard build/type-check green if UI/API types changed.
- [ ] No live mode, no real order, no credentials/secrets.
- [ ] Failure modes are fail-closed and tested.
- [ ] Docs/README updated; audit findings referenced.
- [ ] Audit trail/logging added for new decisions.
- [ ] Rollback plan stated.
- [ ] No unrelated rewrites; existing public behavior preserved unless explicitly approved.

## 14. Dependencies and blockers

| Dependency | Needed by | Status/blocker |
|---|---|---|
| Working Python runtime | all phases | Host venv broken; Docker `python:3.11-slim` works |
| `git` on host PATH | all commits | Missing; WSL Ubuntu works |
| Alembic | Phase 3 | To add |
| LLM provider + keys | Phase 5 | Not configured; agents must remain testable with a stub |
| News/economic-calendar provider | Phase 2/5 | Not configured; optional |
| Testnet credentials | Phase 4 validation | Not configured; no order round-trip verified |
| Committed dataset/artifact | Phase 2 | Missing; must be produced, not fabricated |
| Dashboard chart approach | Phase 3 | Prefer SVG/React; no chart dependency required |
| Human approver | Phases 6–7 | Required for promotion/limit/live decisions |

## 15. Risk register

| Risk | Impact | Mitigation |
|---|---|---|
| Overfitting/leakage | False confidence, losses | rolling OOS, purged folds, reviewer veto, artifacts, min trades |
| Stale/bad data | Wrong trades | quality validator, freshness gate, source labels, alerts |
| Risk state drift | Excess losses | persisted lifecycle state, reconciliation, restart tests |
| Accidental live enablement | Real loss | flags, testnet-only guard, single order manager, signed readiness gate |
| AI proposal harmful | Bad strategy/patch | read-only agents, schema, sandbox, reviewer, human approval, no auto-promotion |
| Credential leak | Account compromise | env/secret manager, no logging, rotation, auth, tests |
| API abuse | Data/CPU/DB impact | auth, rate limits, localhost/reverse proxy, input validation |
| SQLite lock/corruption | Downtime/data loss | WAL, busy timeout, backups, restore drill, Postgres path |
| Dependency drift | Breakage/supply chain | pin/lock, audit, CI, minimal dependencies |
| Unverified performance claims | Wrong decisions | label unvalidated, require artifacts, separate research from execution |

---

**Approval requested:** approve Phase 1 only (PRs 1–5 above) before any application code changes.
