# BTC AI Trading — 1H Trend-Regime BTC Strategy

Multi-agent AI trading system for BTC perps on Delta Exchange.

The validated edge is a **1H Trend-Regime BTC strategy** (single timeframe):
trend agents + EMA/ADX/ATR regime gating. 5m/15m ensembles showed no measurable
edge in validation and are treated as untradeable. Full detail in `app/backtest/study.py`
(WINNER config) and the validation stack committed at `526a842`.

Pipeline: live market data -> indicators -> agents (trend/sentiment/structure/decision) -> risk engine -> LONG / SHORT / NO TRADE. Paper-trading first; live execution is guarded and off by default.

## Setup

```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env   # Windows
```

## Run

```bash
uvicorn app.main:app --reload    # FastAPI backend + paper runner
# or (reads BACKEND_HOST / BACKEND_PORT from env):
python -m app
```

The backend binds loopback (`127.0.0.1:8000`) by default. Set
`BACKEND_HOST=0.0.0.0` only when running inside a container or behind a
same-host reverse proxy. Override with env vars:

```bash
BACKEND_HOST=127.0.0.1
BACKEND_PORT=8000
ADMIN_TOKEN=change-me   # required for watchlist mutations / kill switch HTTP
```

Readiness probe: `GET /health` returns `{"status":"ok","ready":true,...}`
where `ready` reflects the market-data pipeline; `data_status` reports
`streaming`, `stale`, or degraded states. Extended keys report the data source
(`data_source`: `websocket`/`rest`/`none`), the live WebSocket state
(`ws_status`), the 24/7 multi-symbol market-data health (`monitor`), and the
paper account (`paper`).

## Paper account (Phase 6: deterministic local simulation)

The pipeline executes trades **simulated locally** against real market data:

- Entries/exits are computed deterministically with adverse slippage
  (`PAPER_SLIPPAGE_PCT`) and fees on both sides (`PAPER_FEE_PCT`).
- The paper account runs on its own simulated ₹ fund (`PAPER_FUND_INR=5000`,
  `PAPER_RISK_PCT=0.5%`), always shown as `NOT REAL FUNDS` and never confused
  with the Trading Fund used for decision sizing.
- State persists in SQLite (`PaperAccount`, `PaperPosition`, `PaperTrade`) so a
  VPS/container restart restores the balance + open position — it never
  auto-generates an order on restart.
- The paper path never invokes a Delta order endpoint and needs no API keys.

Monitoring endpoints: `GET /monitor` (per-symbol 24/7 health over
BTCUSDT/ETHUSDT/SOLUSDT/XRPUSDT/BNBUSDT), `GET /paper/account`,
`GET /paper/position`, `GET /paper` (full extended snapshot).

## Research engine (Phase 2)

Offline research is isolated from trading state and never places orders:

- Candle validation rejects duplicates, out-of-order, malformed, misaligned, or incomplete data.
- Datasets carry a deterministic SHA-256 hash plus source/venue/transport/fallback provenance.
- `app/features/pipeline.py` is the single causal feature path for live, backtest, and soak.
- Datasets and experiments live in `RESEARCH_DATABASE_URL` (default `data/research.db`), separate
  from the paper/trading database.
- Experiments require a recorded dataset; validation can require a reproduction callback.

```bash
python -m app.backtest.study --mode baseline --no-record   # no research rows
python -m app.backtest.study --mode baseline               # record datasets + experiments
python -m app.backtest.study --mode walk                   # fixed winner params, persisted folds
python -m app.backtest.study --mode walk --refit           # re-fit every fold + trials
pytest -q
```

Walk-forward mode writes a reproducible artifact bundle (manifest, folds,
metrics, trials, command) under `artifacts/walkforward/<hash>/` and persists
the run, every fold and every trial in `RESEARCH_DATABASE_URL`. The test
window is strictly later than the training window, and engine warmup bars are
prepended so no training-period trade reaches test metrics. See
[`docs/RESEARCH_ENGINE.md`](docs/RESEARCH_ENGINE.md) for the data contract and
experiment lifecycle.

The dashboard exposes a read-only Research & Governance view at
`/research`: experiments, persisted walk-forward runs, gate truth, strategy
versions and promotion/rollback history. Promotion, canary, review and rollback
actions require the admin token and stored gates; AI agents cannot perform them.

## Deploy (Docker)

```bash
docker compose up --build -d      # backend + dashboard (SQLite + ./data volume)
docker compose --profile soak up -d soak  # optional long-running paper soak
docker compose up --profile infra # + optional Postgres/Redis for development
```

- Backend port published: `${BACKEND_PORT:-8000}:8000` (matches the backend default).
- Dashboard published on `${DASHBOARD_PORT:-3000}:3000` and proxies `/api/*`
  to the `app` service. Local dev still uses `BACKEND_URL=http://127.0.0.1:8000`
  in `dashboard/.env.local`.
- SQLite DB + execution logs persist via `./data` and `./logs` volumes.
  The image runs as uid `10001`; on a Linux host create those directories with
  `mkdir -p data logs && sudo chown -R 10001:10001 data logs` once.
  Docker Desktop on Windows/macOS handles this automatically.
- Set `ENVIRONMENT=paper` and a 24+ character `ADMIN_TOKEN` for a gated
  deployment. `dev` remains bootable without a token; mutations still fail closed.
- Optional `./.env:/app/.env:ro` mount to load settings from the host `.env`.
- Backups/restore: `python -m app.ops.backup --source data/trading.db --source data/research.db --dest backups/`
  and see [`docs/RUNBOOKS.md`](docs/RUNBOOKS.md).

## Safety

- `LIVE_MODE=off` by default. No real orders are ever auto-placed.
- `TESTNET_MODE=on` by default: every ORDER endpoint resolves to the Delta
  testnet host and production order endpoints are hard-blocked.
- `REAL_ORDER_CONFIRMATION=false` by default: an additional explicit gate for
  the (unreachable-anyway) live path.
- Start in paper-trading / testnet mode only.
- Live trading requires live_mode=on + testnet_mode=off + confirmation=true
  + an explicit per-call confirm; this is intentionally hard to reach.
- The API binds loopback by default. Watchlist mutations and
  `POST /kill-switch/halt|resume` require `X-Admin-Token`; they fail closed
  when `ADMIN_TOKEN` is unset. `GET /kill-switch` reports persisted state.
- `ENVIRONMENT=staging|live` refuses to boot without a 24+ character admin
  token, an explicit CORS allowlist, loopback binding (or
  `ALLOW_PUBLIC_BIND=true` behind TLS), and testnet mode. `LIVE_MODE=true` and
  `REAL_ORDER_CONFIRMATION=true` always fail startup in this release.
- Requests are rate limited in-process (`RATE_LIMIT_PER_MINUTE`, default 600)
  and capped at `MAX_REQUEST_BYTES` (default 1 MB); `/health` and `/metrics`
  are exempt so probes never trip the limiter.
- The local kill switch survives restarts and blocks only new entries; open
  paper positions are still managed to stop/target.
  CLI: `python -m app.risk.kill_switch --halt --reason "..."` and
  `python -m app.risk.kill_switch --resume --confirm`.

## Testnet execution-validation layer (`app/execution`)

Validates execution mechanics (entry/exit timing, SL/TP placement, slippage,
fees, latency, failure recovery) with zero risk. Structure:

- `safety.py` — production-host guards; orders can never reach production.
- `client.py` — strict testnet client: single-auth-timestamp signing,
  duplicate-order protection, latency/fee capture, full request/response/
  fill/rejection/error logging.
- `report.py` — `ExecutionReport` (signal time, expected/actual entry,
  slippage, expected/actual SL+TP, fees, latency, order/position status, P&L)
  persisted to JSONL + CSV + the `testnet_executions` DB table.
- `sltp.py` — simulated SL/TP monitor (native testnet orders not required).
- `recovery.py` — restart recovery: re-arm monitor, cancel orphaned orders,
  duplicate-signal protection across restarts.
- `feed.py` — authenticated WebSocket order feed with reconnect/resubscribe
  and REST reconciliation on reconnect.
- `validator.py` — validation checklist sweep.
- `loop.py` — drives live 1h signals onto testnet.

```bash
python -m app.execution sweep                     # mode/auth/product/positions
python -m app.execution sweep --place-orders      # + full testnet order round-trip
python -m app.execution report                    # print the execution journal
```

## Phases

1. Project + env (this)
2. Delta connection
3. Live BTC data
4. Historical database
5. Indicators
6. AI agents
7. Signal engine
8. Risk management
9. Backtesting
10. Paper trading
11. Dashboard
12. Live execution (guarded; stays OFF for now)