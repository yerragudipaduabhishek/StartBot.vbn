# Execution Engine

## Execution modes

Execution mode policy is centralized in `app/execution/modes.py`:

| Mode | Purpose | Order submission |
|---|---|---|
| paper | Local simulation | Through PaperBroker/PaperTrader only |
| testnet | Delta testnet validation | Allowed only under testnet guards |
| research | Offline analysis | Forbidden |
| backtest | Historical simulation | Forbidden |
| live | Production exchange | Disabled in this release |

`LIVE_TRADING_ENABLED` is hard-coded false. Requests for live mode raise a typed rejection. Research/backtest modes cannot submit orders or mutate trading state.

## Broker abstraction

`app/execution/broker.py` defines a broker protocol with place, cancel, lookup, positions and balances operations. Implementations include:
- `PaperBroker`: local simulation adapter; no exchange client or API credentials.
- `DeltaTestnetBroker`: requires testnet settings/profile and a testnet adapter.
- Live broker construction is intentionally rejected.

## Adapters and order manager

`app/execution/adapters.py` provides the common adapter contract and validated submission boundary. It checks that:
- the execution mode matches the adapter;
- the risk decision is approved and has a size;
- order size exactly matches risk-approved quantity;
- the request references the same risk decision/signal key;
- the request is marked risk-approved;
- expected entry, stop, target, symbol, timeframe, strategy ID and version match the decision/profile.

The order manager is the single idempotent entry point for paper/testnet order submission. The guard reserves a signal key once to prevent duplicate execution. Persistent execution-intent/order/fill/event records support recovery and audit.

## Paper execution

PaperTrader is deterministic local simulation. It does not call a Delta order endpoint, does not require API keys and does not query or modify an exchange position. It models adverse slippage and fees on fills. The paper account, open position and closed trades are persisted and restored after restart. Reconciliation compares persisted account/position state with trade history; mismatch is recorded and should halt new entries through the startup risk path.

Paper money is separate from the dashboard Trading Fund and is labelled simulated. Repository defaults include a ₹5,000 paper fund, 0.5% paper-risk setting, 0.05% fee per side and 0.02% slippage setting; effective profile risk values should be checked from runtime configuration.

## Delta testnet validation

The testnet subsystem includes:
- strict testnet client and production-host checks;
- duplicate client-order protection;
- authenticated order feed with reconnect/resubscribe;
- REST reconciliation after reconnect;
- simulated local stop/target monitoring when native testnet SL/TP is disabled;
- restart recovery and orphan-order handling;
- execution reports recording signal time, expected/actual entry, slippage, SL/TP, fees, latency, order/position status and P&L;
- JSONL/CSV/database journaling and a validation sweep.

The README documents CLI entry points:

```bash
python -m app.execution sweep
python -m app.execution sweep --place-orders
python -m app.execution report
```

A credentialed testnet round-trip is a separate operator action. It is not part of ordinary offline tests and does not imply production readiness.

## Order lifecycle and recovery

Order state is persisted with client order IDs, signal keys, venue, mode, requested/filled size, average fill, fees, slippage and strategy identity. Fill and order-event records preserve the execution timeline. Recovery reconciles remote testnet state and local records to reduce orphan/duplicate risk.

## No live trading

Do not change flags or remove the hard-coded live gate. No test, demo, soak run or documentation procedure should submit real-money orders. Live enablement requires a separately reviewed change and human sign-off.
