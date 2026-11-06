# Dashboard Guide

## Application

The dashboard is a Next.js application in `dashboard/`. Its page entry imports the trading `Dashboard` component. The API client in `dashboard/lib/api.ts` calls the backend through `/api/*` and defines typed fetchers for trading, paper, health, monitor and research resources.

## Trading control center

The dashboard data loader gathers:
- ticker snapshot;
- latest decision;
- paper account;
- risk state;
- recent signals;
- recent trades;
- safety state;
- recent testnet executions.

The UI components include market candle chart, decision panel, signal panel/table, trade plan, paper position, P&L, execution panel, leverage/fund selection, strategy selection, watchlist, market-health panel, timeline, trades table, safety banner and bot control center.

## Bot controls

The API client exposes start, pause and emergency-halt actions. These send the `X-Admin-Token` header. The backend requires an admin token for control operations. The emergency halt pauses the runner and activates the persistent kill switch/risk halt. Start is rejected while risk or kill-switch state remains halted.

Live trading is not enabled. The dashboard must not be interpreted as providing a production order switch.

## Research & Governance page

The `/research` page loads read-only records:
- experiments;
- walk-forward runs;
- proposals and gate status;
- strategy versions;
- promotion/rollback history.

The research dashboard shows experiment provenance and validation evidence. Governance actions use admin-token protected API calls. Promotion/rollback are controlled workflow operations and do not enable live mode.

## Paper account labeling

Paper funds are simulated money, not a wallet or exchange account. The backend returns a `fund_separate` flag and explanatory note. Paper balance, realized/unrealized P&L, open positions, fees and slippage should be read as local simulation results.

## Local development

Install dashboard dependencies in `dashboard/` using the lockfile and package scripts. Configure `BACKEND_URL` for the backend service. In Docker Compose, dashboard uses `http://app:8000` internally and is published on loopback port 3000 by default.

## Troubleshooting

- API errors: verify backend health and `BACKEND_URL`.
- Empty charts/decisions: wait for closed candles and check market-data readiness.
- Control action denied: confirm `ADMIN_TOKEN` matches the backend and that risk/kill-switch state is clear.
- Research page empty: run and persist a research experiment; verify `RESEARCH_DATABASE_URL`.
- Dashboard shows paper status: this is intentional; live trading is disabled in this release.
