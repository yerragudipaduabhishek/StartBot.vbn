# Project Audit — BTC AI Trading System

**Audit date:** 2026-10-28
**Repository:** `C:\Users\abhia\btc-ai-trading`
**Branch / HEAD:** `master` / `91623ef` (`fix: harden websocket and market health data handling`, 2026-09-20)
**Audit type:** read-only code, configuration, test, and safety review
**Phase status:** Phase 1 (audit) complete. No application code was changed. This document and the other files in `docs/` are new untracked files.

---

## 1. Executive verdict

This repository is a **credible paper-trading and testnet-validation prototype**, not yet the
requested self-improving AI trading platform and not ready for live capital.

What is genuinely present:

- A working market-data, indicator, multi-agent signal, deterministic risk, and paper-trading
  pipeline.
- A well-guarded Delta **testnet** execution-validation layer that is deliberately hard to point at
  production.
- A backtesting and parameter-study stack, a FastAPI API, a Next.js dashboard, SQLite persistence,
  and a 24/7 paper-soak CLI.
- A fast regression suite: **366/366 tests pass**.

What is missing or unsafe:

- All five requested AI **research** agents, the AI orchestrator, LLM providers, the
  self-improvement loop, the experiment registry, the portfolio manager, a unified data-quality
  validator, and alerting/kill-switch infrastructure are absent.
- The default `python -m app` paper pipeline is **not** the validated 1H Strategy A used by the
  analysis/study stack. It runs a 5m configuration that the README itself calls untradeable.
- Several paper/risk boundary defects can produce incorrect sizing or entries after a restart, and
  an incomplete signal defaults to `risk_allowed=True`. Closed paper trades are never reported to
  the risk engine, so its daily-loss and drawdown gates do not update even without a restart.
- The backtest metrics are not timeframe-correct and are missing required accounting, so the
  repository's "validated" claim cannot be independently reproduced from committed artifacts.
- The API is unauthenticated and binds `0.0.0.0`; there is no kill switch and no alerting.

**Recommendation:** freeze any live-execution work, complete the Phase 1 safety fixes in
`docs/IMPLEMENTATION_PLAN.md`, then make validation reproducible before adding AI or
self-improvement. The current 366-test baseline should be preserved throughout.

---

## 2. Audit method and evidence

### 2.1 Environment

- Windows host. The repository's existing `.venv` is unusable because its base Microsoft Store
  Python interpreter is missing; there is no working host Python.
- `git` is not available on the Windows `PATH`; repository history was inspected through WSL
  Ubuntu at `/mnt/c/Users/abhia/btc-ai-trading`.
- Tests and byte-compilation were run in Docker Desktop using `python:3.11-slim`, with the repo
  bind-mounted at `/app`.
- `node`/`npm` are available for the dashboard. `dashboard/node_modules` is present.

### 2.2 Exact commands and results

```text
docker run -d --name btcai-test -v "C:\Users\abhia\btc-ai-trading:/app" -w /app python:3.11-slim sleep infinity
docker exec btcai-test pip install --disable-pip-version-check -q -r requirements.txt

docker exec btcai-test python -m pytest --collect-only -q
  -> 366 tests collected

docker exec btcai-test python -m pytest -q
  -> 366 passed, 6 warnings in 24.08s

docker exec btcai-test python -m compileall -q app
  -> OK

npm run build   (workdir: dashboard)
  -> Next.js 15.5.25 compiled successfully
  -> "Linting and checking validity of types" passed
  -> 4/4 static pages generated
```

Warnings (not failures):

- `PytestCollectionWarning` for classes whose names start with `Testnet...` in
  `app/execution/`: `TestnetConnectivityError`, `TestnetExecutionClient`, `TestnetOrderFeed`,
  `TestnetValidationLoop`, `TestnetValidator`. Pytest tries to collect them as test classes.
- One Starlette/httpx deprecation warning.

No tests were skipped. The suite ran without API keys or live credentials and uses isolated
temporary SQLite databases. No real-money order was placed during this audit. No testnet order
round-trip was attempted; credentials were not provided to the audit container.

### 2.3 Repository state

The worktree was already dirty before the audit:

| Path | Change | Notes |
|---|---|---|
| `app/database/models.py` | modified | adds `WatchlistItem` |
| `app/main.py` | modified | watchlist/instrument endpoints and wiring |
| `dashboard/lib/types.ts` | modified | watchlist/instrument types |
| `tests/conftest.py` | modified | test isolation adjustment |
| `app/execution/client.py` | modified | **whitespace/line-ending only** (`--ignore-all-space` empty) |
| `app/execution/loop.py` | modified | **whitespace/line-ending only** |
| `app/execution/validator.py` | modified | **whitespace/line-ending only** |
| `app/data/__init__.py`, `app/data/news.py`, `app/data/resample.py`, `app/instruments/`, `tests/test_instruments.py` | untracked | working instrument/watchlist/news/resample feature |

The passing test result therefore describes **HEAD plus uncommitted work**, not a clean commit.
This worktree should be committed or stashed deliberately before implementation begins.

### 2.4 What was reviewed

Core Python modules under `app/`, the full test layout, `README.md`, `.env.example`,
`requirements.txt`, `Dockerfile`, `docker-compose.yml`, dashboard source/types/package scripts,
repository history, ignored runtime data, and the tracked/untracked file inventory. No source file
was modified by this audit.

---

## 3. Current capabilities

| Area | State | Evidence | Important caveat |
|---|---|---|---|
| Market data | Functional | Delta WebSocket ticker/funding + REST candle polling, market-health monitor | No cache, no provider abstraction, no per-row provenance |
| Historical data | Functional | SQLite candle store, bulk/deep backfill, resampling | No source/ingest/quality columns, no dataset registry |
| Indicators | Functional but narrow | EMA, RSI, MACD, ATR, ADX/DI, VWAP | No SMA, Bollinger, volume/volatility/regime features in the live path |
| Signal generation | Functional | Trend/structure/sentiment/derivatives agents plus weighted decision | Deterministic scoring, not LLM "AI"; no strategy version in the signal |
| Risk engine | Partial | Position sizing, confidence, daily loss, drawdown, volatility, max positions | Several required limits absent; state is in-memory and not restored |
| Paper trading | Functional | Persistent account/position/trades, fees, slippage, restart restore | Unsafe signal default, no order/risk audit trail, main runner can act on restart |
| Testnet execution | Functional, well guarded | Strict testnet client, host guard, duplicate protection, recovery, SL/TP monitor, reports | Only reachable through CLI/loop; not wired as a general broker layer |
| Backtesting | Partial | Event replay, fees/slippage, stops/targets, OOS CLI, regime tags | Timeframe annualization bug, incomplete accounting, no artifacts |
| Dashboard | Partial | Paper/analysis/monitor/watchlist UI, clear SIMULATED labeling | Missing equity curve, risk/backtest/experiment/alert views |
| API | Functional | Read endpoints plus watchlist mutation endpoints | Unauthenticated, binds `0.0.0.0`, no rate limiting |
| Operations | Partial | Docker app, health/readiness, SOAK CLI, telemetry JSONL | No dashboard container, no alerting, no kill switch, no migrations |

---

## 4. Module-by-module disposition (the 26 requested modules)

Status meanings: **Functional** = usable for its current paper/testnet scope; **Partial** = material
required behavior missing; **Missing** = no implementation; **Disabled** = deliberately present but
blocked.

| # | Requested module | Status | Existing evidence and principal gap |
|---|---|---|---|
| 1 | Market Data Engine | Partial | `app/data/market_data.py`, `websocket.py`, `delta_client.py`, `market_health.py`. WS/REST + health exist; no cache, no source per candle, fixed 300s freshness, no provider abstraction |
| 2 | Historical Data Manager | Partial | `app/data/historical.py`, `app/data/resample.py`, `Candle` model. Backfill/resample exist; no provenance, ingest time, quality state, or dataset registry |
| 3 | Data Quality Validator | Partial | Inline checks in `delta_client.py`, `historical.py`, `market_health.py`. No unified validator; OHLCV integrity, gaps, duplicate policy, and per-row status are missing |
| 4 | Feature Engineering | Partial | `app/indicators/technical.py`; regime tagging only in backtest. No feature store, versioning, or provenance |
| 5 | Indicator Engine | Partial | EMA/RSI/MACD/ATR/ADX/VWAP only; SMA, Bollinger, volume, volatility, regime features missing |
| 6 | News and Event Processing | Partial | Untracked `app/data/news.py` RSS fetch + VADER sentiment. No economic calendar, persistence, dedupe, or event history |
| 7 | AI Research Agents | Missing | None of Market Analyst, Strategy Researcher, Code Agent, Research Reviewer, Performance Analyst exists. Existing "agents" are deterministic signal voters |
| 8 | AI Orchestrator | Missing | `llm_provider`/keys exist in settings but are unused; no orchestration, schema validation, or approval workflow |
| 9 | Strategy Registry | Partial | `app/analysis/strategies.py`, `app/instruments/strategy_compat.py`. Hard-coded Strategy A descriptor; no versioned registry, lineage, or promotion state |
| 10 | Strategy Execution Engine | Partial | `app/strategy/signal_engine.py`, `app/trading/runner.py`, `app/soak/runner.py`. Works for paper, but no formal Strategy interface and the main runner's config is inconsistent with Strategy A |
| 11 | Backtesting Engine | Partial | `app/backtest/engine.py`. Costs/stops/targets exist; metric annualization and accounting are incomplete; no partial fills/funding/sessions/benchmark/cost sensitivity |
| 12 | Walk-Forward Analysis | Partial | `app/backtest/study.py --mode walk`. Reuses fixed parameters; no rolling re-fit, fold records, or artifacts |
| 13 | Optimization Engine | Partial | Random in-sample scan inside `study.py`; not a reusable optimizer, no trial persistence or guards |
| 14 | Self-Improvement Engine | Missing | No proposal, review, approval, canary, promotion, or rollback machinery |
| 15 | Risk Management | Partial | `app/risk/risk_manager.py`, `config.py`, `position_sizer.py`. Core gates exist; no max order/trade caps, cooldown, spread/slippage, session, freshness, duplicate, kill-switch, or persistence |
| 16 | Portfolio Management | Missing | Single-position `PaperTrader`; watchlist exists but no allocation, correlation, exposure, or capital rotation |
| 17 | Broker Integration | Partial | `DeltaClient` and `TestnetExecutionClient`; no broker-neutral interface, capability model, or host-enforced order adapter |
| 18 | Order Manager | Partial | `app/trading/orders.py`, testnet loop/recovery. No persistent generic order state machine, cancel/replace, or fill ledger |
| 19 | Paper Trading Engine | Functional | `app/trading/paper_trader.py`, `PaperAccount/Position/Trade`. Missing validated-signal boundary, order/fill/risk persistence, and startup idempotency in the main runner |
| 20 | Live Execution Adapter | Disabled | `app/trading/executor.py` has five gates and stays off by default; not production-ready (no risk gate, no exchange-side stop, no reconciliation) |
| 21 | Trade Reconciliation | Partial | `app/execution/recovery.py` for testnet only; no paper reconciliation loop and not wired to the main app |
| 22 | Logging and Analytics | Partial | Python logging, JSONL journals/telemetry. No structured logs, aggregation, retention, or performance analytics |
| 23 | Monitoring and Alerting | Partial | `/health`, `/monitor` and market health. No alert delivery, no global kill switch, no strategy-heartbeat alert |
| 24 | Database and Experiment Memory | Partial | SQLAlchemy models for candles/signals/paper/testnet. No experiment, validation, audit, equity-snapshot, or system-event tables; ad-hoc SQLite migration only |
| 25 | Web Dashboard | Partial | Next.js paper/monitor/analysis UI. No equity curve, drawdown/win-rate/PF/cost metrics, backtest/walk-forward history, experiments, alerts, or kill switch |
| 26 | Configuration and Secrets | Partial | Pydantic settings, `.env` ignored, `.env.example`; no environment profiles, secret manager, startup safety validator, or pinned Python dependencies |

---

## 5. Findings

Severity is relative to **current reliance on paper results and any future live path**.
`P1` must be fixed before live consideration; `P2` before scaling; `P3` is hygiene.

| ID | Severity | Finding |
|---|---|---|
| F1 | P1 | Main app paper loop runs a different timeframe/strategy from the validated 1H Strategy A |
| F2 | P1 | PaperTrader accepts a signal with no `risk_allowed` and fabricates stop/target values |
| F3 | P1 | RiskManager is disconnected from the paper lifecycle: closed P&L is never recorded, and restored state is not synchronized after a restart |
| F4 | P1 | The execution boundary does not enforce risk limits; the Executor trusts arbitrary size |
| F5 | P1 | `TradingRunner` can auto-enter a paper trade after restart, contrary to the README claim |
| F6 | P1 | Required pre-trade risk controls are absent (cooldown, trade cap, exposure, spread/slippage, sessions, freshness, duplicate orders) |
| F7 | P1 | No global kill switch and no alerting |
| F8 | P1 | API is unauthenticated and binds `0.0.0.0`; watchlist mutations are open |
| F9 | P1 | Data provenance/freshness is not checked at the order boundary; testnet reads can silently use production prices |
| F10 | P2 | Annualized backtest metrics use a fixed 5m constant for every timeframe |
| F11 | P2 | Backtest accounting is optimistic/incomplete (entry at signal close, realized-only equity, no exit fee on the final open trade) |
| F12 | P2 | "Walk-forward" does not re-fit; Strategy A's `"VALIDATED"` status is hard-coded |
| F13 | P2 | No reproducible experiment artifacts or experiment memory |
| F14 | P2 | No unified data-quality validation, cache, rate limiter, or `Retry-After` handling |
| F15 | P2 | Ad-hoc migrations; no Alembic; missing audit/experiment/equity tables and indexes |
| F16 | P2 | Dashboard cannot answer whether the strategy/risk is working |
| F17 | P3 | Pytest collection warnings, unpinned Python dependencies, no Python lint/typecheck config |
| F18 | P3 | Dirty worktree and line-ending-only diffs make the baseline ambiguous |

### F1 — The main paper pipeline is not the validated strategy

`README.md:5-8` says the validated edge is a 1H Trend-Regime strategy and that 5m/15m ensembles
are untradeable. However:

- `app/config/settings.py:42-43` defaults to `BTCUSDT` / `5m`.
- `app/main.py:53` constructs `MarketData` with that default.
- `app/main.py:57-58` constructs `RiskManager` and `SignalEngine` with **default** settings, not the
  `WINNER` parameters used by `app/backtest/study.py`.
- `app/trading/runner.py:87-101` hard-codes the persisted signal to `symbol="BTCUSDT"`,
  `timeframe="5m"` regardless of the live configuration.
- `app/analysis/strategies.py` exposes Strategy A as a separate analysis path.
- `app/backtest/study.py:236` defaults the study CLI to `--resolution 5m`, even though the `WINNER`
  comment says it came from a 1h scan and the README calls the 1h strategy validated.

Consequences: `python -m app` (and the Docker service) does not run the strategy the README calls
validated. A change to `TRADING_TIMEFRAME=1h` would still write `timeframe="5m"` to the database.
The test suite passes because it tests these components in isolation, not for config consistency.

### F2 — Unsafe default at the paper signal boundary

`app/trading/paper_trader.py:410-421`:

```python
if direction in ("long", "short") and sig.get("risk_allowed", True):
    ...
    stop = sig.get("stop_loss") or price * 0.99
    target = sig.get("target") or price * 1.01
```

An incomplete or malformed signal without `risk_allowed` can open a position. Missing risk levels are
replaced with arbitrary 1% values. The risk contract should be explicit and fail closed: a paper
order must require a fresh, validated `risk_allowed is True`, a valid side, positive size, valid
stop/target, and a matching strategy version.

Related observation: a rejected risk decision currently falls through the same branch and closes any
existing position (risk-off). That may be intended, but it is undocumented and untested.

### F3 — RiskManager is disconnected from the paper lifecycle

There are two related defects.

**Closed P&L is never recorded.** Nothing in the paper path calls
`RiskManager.record_trade()`. `TradingRunner` only calls `record_trade_daily_reset()`
(`app/trading/runner.py:84`). `SoakRunner` copies `starting_equity` and `realized_pnl` from the paper
account (`app/soak/runner.py:193-199`) but never updates `day_pnl`, `peak_equity`,
`last_trade_bar_index`, or `open_positions` on a close. As a result, even without a restart:

- `day_pnl` stays at 0, so the daily-loss gate never fires in the paper pipeline;
- `peak_equity` stays at the starting balance, so the drawdown gate never fires;
- `open_positions` is never incremented on entry, so the risk engine's position count is wrong;
- `realized_pnl` in the main app stays at 0 even after losing paper trades.

**Restored state is not synchronized.** `PaperTrader` restores balance and realized P&L from SQLite
(`app/trading/paper_trader.py:93-123`). `RiskManager` is created fresh with
`starting_equity=paper_fund_inr`, `realized_pnl=0`, `day_pnl=0`, `peak_equity=starting_equity`
(`app/main.py:57`). Nothing copies the restored paper state into the risk engine. After a restart,
drawdown/daily-loss gates again see a clean account, and `SignalEngine.risk_equity()` uses
`starting_equity + realized_pnl` (`app/strategy/signal_engine.py:151-152`), so sizing is based on the
wrong equity.

The soak runner's partial equity sync is not a substitute for a full risk lifecycle integration.

### F4 — Risk is advisory, not enforced at the execution boundary

`Executor.execute()` (`app/trading/executor.py:72-100`) accepts a caller-supplied `size` and does not
evaluate risk, confidence, stops, exposure, or data freshness. The testnet path places that size
directly. The live path (`_place_delta`, lines 119-140) submits a naked market order with no
exchange-side stop-loss or target. The only protections are feature flags and caller discipline.

The testnet client has strong host/auth guards, but `DeltaClient.place_order` itself
(`app/data/delta_client.py:185-216`) has no production-host interlock. A direct client constructed
with a production URL can place an order outside the Executor.

### F5 — Restart can auto-enter in the main paper runner

`TradingRunner._processed_candle` starts as `None` (`app/trading/runner.py:39`). On the first cycle
after startup it always evaluates the latest closed candle and calls `paper.on_signal(sig)`
(lines 61-84). If the restored paper account is flat and the signal is actionable, it opens a trade
immediately. There is no "wait for the next new candle" seed.

This contradicts `README.md:53-55` ("it never auto-generates an order on restart") and is not covered
by a test. The soak runner does implement startup seeding (`app/soak/runner.py:348-351`).

### F6 — Missing risk controls

`RiskSettings` (`app/risk/config.py`) defines `cooldown_bars`, but it is never read. Also absent:

- maximum order size/notional and maximum daily trades;
- cooldown between entries and per-symbol cooldown;
- spread/slippage limits;
- session/trading-hours eligibility;
- market-data freshness and quality gate before an order;
- duplicate-order/idempotency guard for paper;
- gross/net exposure caps and correlation limits;
- persisted halt state and a global kill switch.

`RiskManager.open_positions` is not incremented on paper entry; it is only decremented in
`record_trade()` (`app/risk/risk_manager.py:31`). With the current single-position paper trader this
is masked by the paper position check, but it is wrong for portfolio expansion and for the risk
engine's own `max_positions` gate.

### F7 — No kill switch or alerts

There is no process-wide stop that halts new entries and optionally flattens positions. `trading_halted`
is in-memory and only set by the drawdown check. There is no Telegram/email/webhook alert for
drawdown breach, stale data, repeated order rejection, process death, or open-position divergence.
The dashboard cannot trigger or display a halt.

### F8 — Unauthenticated, externally bound API

`settings.backend_host` defaults to `0.0.0.0` and Docker Compose publishes the port
(`docker-compose.yml`). `app/main.py` has no authentication or rate limiting. The uncommitted
watchlist endpoints (`POST /watchlist`, `DELETE /watchlist/{id}`, `POST /watchlist/select`) are
mutations. CORS is restricted to `http://localhost:3000`, but CORS is not authentication; direct HTTP
clients are unaffected. The module docstring still says "All endpoints are read-only", which is no
longer true.

### F9 — Data provenance and staleness are not enforced at the order boundary

`Settings.delta_market_data_fallback_urls` deliberately falls back from the testnet public-data host
to the **production** public host for reads (`app/config/settings.py:117-133`). This is funds-safe
because it is read-only, but candles and tickers can silently come from a different venue. Candle
rows do not store source, ingestion time, or quality status (`app/database/models.py:13-28`).

`MarketData` uses a fixed 300-second freshness check regardless of resolution
(`app/data/market_data.py:34`), even though the soak feed is resolution-aware. Neither the paper
entry path nor the Executor re-checks freshness immediately before acting.

### F10 — Timeframe-incorrect performance metrics

`app/backtest/engine.py:16` defines `BARS_PER_YEAR_5M = 288 * 365` and uses it as the default for
Sharpe and Sortino (`lines 90-109`). The study runs 1H data but does not pass a 1H constant. For 1H
the annualization factor should be `sqrt(24 * 365)`, so the reported Sharpe/Sortino is inflated by
`sqrt(12)` (about 3.46x) versus a correct 1H annualization.

`_bar_returns()` (`lines 81-88`) only includes bars where equity changed. Flat time is omitted, and
the series is event-based rather than a fixed bar interval, so the annualization is not a standard
time-series Sharpe even with the right constant.

### F11 — Backtest accounting gaps that flatter results

- Entries execute at the signal bar's close (`engine.py:218-239`), not the next bar's open. This is
  optimistic and not achievable with the live polling loop at the same decision point.
- The equity curve is realized equity only (`line 243`); unrealized drawdown is invisible.
- A position still open at the end is closed at its entry price with `pnl=0` and **no exit fee**
  (`lines 245-252`). Entry fees are also only charged in the exit branch, so the final open trade
  carries no fees at all.
- The engine does not model partial fills, funding, liquidation, session gaps, or order rejection.
- Metrics lack CAGR, expectancy, exposure/time-in-market, turnover, benchmark comparison, and
  transaction-cost sensitivity.

### F12 — Validation claims are not reproducible from the repository

`app/backtest/study.py` contains the `WINNER` parameter set and an in-sample/OOS study. `--mode walk`
evaluates a **fixed parameter set** over later windows; it does not re-optimize per fold. No study
output, fold table, or artifact is committed.

`app/analysis/strategies.py` exposes Strategy A with a literal status `"VALIDATED"`, and
`app/instruments/strategy_compat.py` encodes BTCUSDT/1H compatibility. Nothing in the repository
links that label to a timestamped, reproducible experiment, dataset hash, or out-of-sample report,
and the study CLI default is 5m. The README's "validated edge" claim is therefore **unverified by
this audit**, not disproven.

`study.py` computes regime labels over the full DataFrame before splitting; the labels are used for
descriptive attribution, not decisions, but they are not point-in-time and must not be treated as
tradable features.

### F13 — No experiment memory

There are no tables or files for experiments, parameter trials, fold results, validation runs,
approvals, promotions, or rollbacks. The self-improvement requirement cannot be implemented safely
without an append-only registry and artifact lineage.

### F14 — Data quality, caching, and rate limiting

- No unified validator checks OHLCV invariants (`high >= max(open, close)`, `low <= min(open, close)`,
  positive volume, monotonic time, gap detection, duplicate handling).
- No caching layer, despite dashboard polling and `AnalysisService` fetching candles repeatedly.
- `DeltaClient._request` retries 429 with fixed 2s/4s sleeps, ignores `Retry-After`, and has no
  jitter or global rate limiter (`app/data/delta_client.py:72-110`).
- 5xx responses fail over to the next host immediately; there is no circuit breaker or health score.
- No stale-data alert.

### F15 — Persistence and migration debt

- `app/database/database.py:35-64` performs ad-hoc SQLite `ALTER TABLE` statements at startup.
  There is no Alembic and no schema version.
- The `Postgres`/`Redis` services are optional profiles; the code's JSON/DB assumptions are only
  exercised on SQLite.
- Missing indexes on common signal/trade queries and missing tables for orders, fills, risk decisions,
  equity snapshots, strategy versions, experiments, audit events, and system events.
- SQLite has no WAL/busy-timeout configuration; concurrent API + runner writes are not tested.

### F16 — Dashboard cannot answer core questions

The dashboard clearly labels paper results as simulated and shows balance/P&L/position. It does not
show:

- an equity curve or drawdown chart;
- win rate, profit factor, expectancy, cost totals, exposure, or turnover;
- backtest or walk-forward results, fold stability, or parameter provenance;
- experiment history, approval state, or rollback;
- alerts, system events, error rates, or strategy heartbeat;
- broker/testnet connectivity and order rejection reasons;
- CPU/memory or data freshness.

### F17 — Test and dependency hygiene

- Six pytest collection/deprecation warnings (above).
- `requirements.txt` uses open lower bounds (`>=`) with no lock file; installed versions are not
  reproducible. `dashboard/package-lock.json` exists.
- No `ruff`, `mypy`, `black`, or equivalent configured for Python; the only static check is Next.js
  build/type-check.
- No coverage measurement.

### F18 — Worktree and documentation drift

Uncommitted instrument/watchlist work is part of the passing baseline. Three execution files carry
line-ending-only diffs. `README.md` and code docstrings contain claims that are no longer true
(read-only API, no auto-order on restart). These must be reconciled before implementation.

---

## 6. Test coverage: what the 366 tests do and do not prove

The suite is a strong regression baseline for current behavior, including mocked testnet execution,
paper persistence, risk gates, websocket handling, soak behavior, and dashboard contract types. It
does **not** cover:

- a signal with a missing or non-boolean `risk_allowed`;
- RiskManager/PaperTrader state synchronization after restart;
- closed paper trades updating risk daily P&L, peak equity, and position count;
- `TradingRunner` startup idempotency;
- symbol/timeframe consistency between config, runner, database, and Strategy A;
- correct 1H Sharpe/Sortino annualization;
- final-open-position fees and unrealized drawdown;
- cooldown, max trades/day, max order size, exposure caps, spread/slippage, session filters, or
  data-quality gates;
- API authentication/rate limiting;
- kill switch or alert delivery;
- AI agents, orchestrator, self-improvement, experiment registry, or portfolio allocation (absent);
- SQLite multi-process contention;
- any real testnet or production network round-trip.

Passing tests mean "current behavior is stable", not "the strategy has an edge" and not "the system is
safe for real money".

---

## 7. Verified safety positives

- `LIVE_MODE` defaults off; `TESTNET_MODE` defaults on; `REAL_ORDER_CONFIRMATION` defaults false
  (`app/config/settings.py:59-61`).
- `Executor.can_trade_live` requires all flags and keys; `Executor.can_trade_testnet` excludes live
  mode (`app/trading/executor.py:52-70`).
- `TestnetExecutionClient` validates the venue host and refuses to initialize when live mode is on
  or testnet mode is off (`app/execution/client.py:61-97`); production order hosts are hard-blocked.
- The testnet path records duplicate protection, latencies, fees, order/fill state, and recovery
  state (`app/execution/`).
- The paper path never calls a Delta order endpoint and needs no API keys.
- No `.env` or tracked credentials were found in the repository; `.env` is ignored by git.
- `/health` does not expose API keys or secrets.

A separate local folder, `C:\Users\abhia\tradingBot`, is not part of this repository and contains a
local `.env` with secret-like assignments. It was not printed or modified; it should be reviewed by
the owner for local hygiene.

---

## 8. Blockers and unverified items

| Item | Status / impact |
|---|---|
| Host Python | Broken venv/base interpreter; all Python verification used Docker `python:3.11-slim` |
| Git on Windows PATH | Missing; use WSL Ubuntu for repository operations |
| Testnet credentials | Not configured in this audit; no network order round-trip verified |
| LLM provider keys | Not configured and no provider client exists; AI modules cannot be validated yet |
| News API key | Not configured; only RSS/VADER path exists |
| Historical dataset | `data/trading.db` exists but is ignored by git; no committed dataset or hash; study results not reproducible from the repo |
| Frontend runtime | Production build/type-check passed; not run against a live backend in this audit |
| 24/7 soak deployment | CLI exists; `docker-compose.yml` does not run the soak service; only local telemetry/db artifacts were observed |
| Real-money behavior | Not tested, by design; live path is unreachable under defaults |

---

## 9. Recommended order of work

1. **Phase 1 safety hardening (small, no new dependencies):** fail-closed signal validation, risk
   state restore, runner startup seeding, single source of truth for symbol/timeframe/strategy,
   risk checks at the paper/execution boundary, kill switch, safety tests.
2. **Phase 2 validation correctness:** timeframe-correct metrics, complete backtest accounting,
   reproducible artifacts, real rolling walk-forward, unified data-quality validation and provenance.
3. **Phase 3 persistence/observability:** Alembic, order/fill/risk/equity/audit tables, structured
   logging, alerts, dashboard analytics.
4. **Phase 4 execution/portfolio architecture:** broker interface, order manager, portfolio manager;
   live remains disabled.
5. **Phase 5 AI research:** read-only agents and orchestrator, mocked/offline tests, human approval.
6. **Phase 6 self-improvement:** proposal → validation → review → approval → canary → promotion or
   rollback, with deterministic risk immutable.
7. **Phase 7 product hardening:** auth, rate limiting, secret management, backups, CI, deployment
   runbooks.

The detailed mapping, acceptance criteria, and checklist are in
[`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md). The target design is in
[`ARCHITECTURE.md`](ARCHITECTURE.md). The threat model and hard safety rules are in
[`SECURITY_AND_RISK.md`](SECURITY_AND_RISK.md).

---

## 10. Audit sign-off

- Application code changed: **no**.
- Tests run: **366 passed / 366 collected**, plus Python byte-compilation and a Next.js production
  build with type checking.
- Live trading enabled: **no**.
- Real orders placed: **no**.
- Conclusion: safe to continue development in paper mode; **not** safe to enable live trading or to
  treat the current performance claims as independently validated.
