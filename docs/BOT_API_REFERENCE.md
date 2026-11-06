# FastAPI API Reference

Base URL defaults to `http://127.0.0.1:8000`. The dashboard proxies API requests through its `/api` route. Request/response shapes are defined in `app/main.py` and dashboard types.

## Authentication

Read endpoints are generally available without an admin token. Mutating control/watchlist/research-governance endpoints use the `require_admin` dependency and require:

`X-Admin-Token: <ADMIN_TOKEN>`

If `ADMIN_TOKEN` is unset, protected endpoints fail closed with HTTP 403. Configure CORS origins explicitly for the dashboard. The API middleware enforces a per-client in-memory request rate limit and maximum request body size; `/health` and `/metrics` are exempt.

## Health and control

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Readiness, data source/status, candle age, WebSocket and paper status |
| GET | `/monitor` | Per-symbol market health |
| GET | `/safety` | Mode flags, configured credentials presence, bind host, profile and kill switch |
| GET | `/kill-switch` | Persistent kill-switch status |
| POST | `/kill-switch/halt` | Halt new entries; admin token required |
| POST | `/kill-switch/resume` | Resume after operator action; admin token required |
| GET | `/bot/status` | Runner state and safety halt flags |
| POST | `/bot/pause` | Pause runner; admin token required |
| POST | `/bot/start` | Resume runner only when risk and kill switch are clear; admin token required |
| POST | `/bot/emergency-halt` | Pause runner and halt risk/kill switch; admin token required |

## Market, strategy and paper account

| Method | Path | Purpose |
|---|---|---|
| GET | `/ticker` | MarketData snapshot |
| GET | `/decision` | Latest signal/decision |
| GET | `/paper` | Extended simulated account snapshot |
| GET | `/paper/account` | Starting balance, balance, P&L, equity and modeled costs |
| GET | `/paper/position` | Current simulated position |
| GET | `/risk` | Equity, P&L, positions, trade count and halt limits/state |
| GET | `/signals` | Recent signal records |
| GET | `/signals/stats` | Signal count |
| GET | `/trades` | Recent paper trades |
| GET | `/executions` | Testnet execution journal |
| GET | `/orders` | Orders, optionally filtered by status |
| GET | `/orders/{client_order_id}` | Order with fills and events |
| GET | `/risk-decisions` | Recent persisted risk decisions |
| GET | `/metrics` | Operational counts, equity, order statuses and runner state |
| GET | `/alerts` | Alert records |
| GET | `/audit` | Audit events |
| GET | `/system-events` | System event records |
| GET | `/equity` | Equity snapshots |

The source also exposes dashboard-consumed strategy, instrument, watchlist and analysis resources. Their precise request parameters should be read from the corresponding route definitions and dashboard API client before scripting against them.

## Research and governance

| Method | Path | Purpose |
|---|---|---|
| GET | `/research/experiments` | Recorded experiments |
| GET | `/research/walk-forward` | Persisted walk-forward runs and aggregate metrics |
| GET | `/research/proposals` | Research proposals |
| GET | `/research/proposals/{proposal_id}` | Proposal details |
| GET | `/research/proposals/{proposal_id}/gates` | Gate status |
| GET | `/research/strategies` | Strategy versions/status |
| GET | `/research/promotions` | Promotion/rollback history |
| POST | `/research/proposals` | Create proposal; admin token required |
| POST | `/research/proposals/{proposal_id}/reviews` | Record review/veto; admin token required |
| POST | `/research/proposals/{proposal_id}/approve` | Record named approval/rationale; admin token required |
| POST | `/research/proposals/{proposal_id}/canary/start` | Start canary record; admin token required |
| POST | `/research/proposals/{proposal_id}/canary/record` | Record canary outcomes; admin token required |
| POST | `/research/proposals/{proposal_id}/canary/finish` | Finish canary; admin token required |
| POST | `/research/proposals/{proposal_id}/promote` | Promote after gates; admin token required |
| POST | `/research/strategies/{strategy_id}/rollback` | Roll back paper strategy pin; admin token required |

## Common response behavior

- Health readiness is data-pipeline readiness, not trading profitability or live readiness.
- `/decision` may report awaiting first closed candle.
- Paper endpoints explicitly represent simulated funds.
- Control endpoints return conflict/service-unavailable errors if runner is unavailable or safety state is halted.
- Research mutations record audit events and do not submit orders.

## Operational note

Do not expose the backend directly to the public internet. Use loopback binding or a TLS reverse proxy with authentication, strict CORS, firewall rules and a strong admin token. The API is not a live-trading interface.
