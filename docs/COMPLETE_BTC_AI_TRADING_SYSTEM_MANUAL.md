# BTC AI Trading System — Complete Technical Manual

**Repository:** [StartBot.vbn](https://github.com/yerragudipaduabhishek/StartBot.vbn)  
**Branch:** `main`  
**Reviewed source revision:** `1713ec6`  
**System focus:** Delta Exchange BTC perpetuals, Strategy A, 1-hour timeframe  
**Execution status:** Paper-first; live trading disabled in code.

## 1. What this system is

This repository contains a Python trading backend, market-data services, technical and derivatives feature calculations, a multi-agent signal engine, risk controls, a local paper account, a testnet execution-validation layer, a backtesting/research subsystem, a FastAPI API, and a Next.js dashboard.

The principal operational flow is:

`Delta public market data → validated candles/ticker → causal features → strategy agents → weighted decision → deterministic risk decision → paper execution`

The research subsystem is intentionally separated from the trading/execution boundary. Research agents can analyze data and propose hypotheses or patches, but they cannot apply code, change risk limits, or submit orders.

## 2. Active strategy profile

The locked Strategy A identity is defined in `app/strategy/strategy_a.py`:

| Property | Value |
|---|---|
| Strategy ID / version | `strategy_a` / `1.0.0` |
| Symbol | `BTCUSDT` |
| Timeframe | `1h` |
| Contract value | `0.001` |
| EMA spans | 8 / 40 / 50 |
| Decision threshold | 0.25 |
| Minimum confidence parameter | 0.50 |
| Stop distance | 1.0 ATR |
| Target distance | 2.0 ATR |
| Strategy weights | trend 0.40, structure 0.00, sentiment 0.15, derivatives 0.15 |

The strategy constants are centralized so the strategy profile, paper runner, research/backtest and testnet validation code can refer to the same identity and parameters. The profile validator locks Strategy A to BTCUSDT and 1h.

The README identifies the 1H trend-regime strategy as the validated candidate and says 5m/15m ensembles did not show measurable edge in validation. That statement is a project-reported research conclusion, not a guarantee of future returns.

## 3. Signal generation

The signal engine computes a causal feature set from OHLCV candles. Its agents return structured `Vote` records containing direction, score, confidence, explanation and details.

- **Trend agent:** EMA alignment, +DI/-DI, price versus rolling VWAP, MACD histogram; ADX contributes to confidence.
- **Structure agent:** swing pivot highs/lows and higher-high/higher-low or lower-high/lower-low structure; range position can provide a mild lean.
- **Sentiment agent:** scores supplied headlines through a provider. VADER is used when available; otherwise neutral sentiment is used. No headline source means neutral sentiment.
- **Derivatives agent:** funding rate is treated contrarian at extremes; open-interest change contributes positioning/conviction context.
- **Decision agent:** normalizes a weighted sum of agent scores and weighted confidence; returns long, short or neutral/no-trade using the threshold.

The engine creates an entry reference from the latest candle close and ATR-based stop/target levels. It then asks the deterministic risk manager to evaluate the candidate. A directional vote is not, by itself, permission to trade.

## 4. Market data and feature pipeline

`MarketData` combines Delta public WebSocket ticker/funding messages with REST candle polling. The WebSocket layer normalizes current and legacy message shapes, reconnects and resubscribes, and reports transport/handler health. REST fallback is restricted to public read endpoints. Source and fallback metadata are retained in snapshots/candle records.

`FeaturePipeline` is the shared OHLCV-to-feature path. It validates required columns, numeric values, positive prices, non-negative volume, OHLC relationships, unique timestamps and ascending order. It computes indicators without future-row inputs and tags outputs with a feature-pipeline version.

## 5. Risk and portfolio controls

The risk manager owns the risk decision. Its configured controls include fixed-fractional sizing, maximum leverage, daily-loss and drawdown halts, trade/position limits, cooldown, minimum confidence/equity, candle freshness/streaming requirements, allowed sessions, volatility skip and exposure limits. The position sizer derives quantity from equity, entry-to-stop distance, risk percentage and contract value, then caps notional using maximum leverage.

The execution guard validates the signal against the immutable trading profile, checks the persistent kill switch, obtains a risk decision, checks portfolio exposure and rejects size mismatches. The order adapters validate that an order exactly matches its approved risk decision.

## 6. Execution modes

- **Paper:** local simulated account; no Delta order endpoint and no exchange credentials required.
- **Testnet:** contained Delta testnet broker/validation path; requires testnet configuration and credentials for authenticated exchange actions.
- **Research/backtest:** cannot submit orders or mutate trading account state.
- **Live:** explicitly disabled. `LIVE_TRADING_ENABLED` is hard-coded false; live mode is rejected by the execution-mode policy and profile construction.

Paper fills are simulations using configured fees and adverse slippage. They are not evidence of actual exchange fills, funding, latency, liquidation or live execution behavior.

## 7. Backtesting and research

The backtest engine uses causal next-bar execution: a signal from a closed candle is filled at the next bar open; stops/targets are evaluated against the next bar high/low, with gap-through levels filled at the worse open. It accounts for entry/exit fees, slippage, funding at 8-hour boundaries and final-position closure. Results include returns, drawdown, win rate, profit factor, expectancy, Sharpe/Sortino, exposure, turnover and cost fields.

Datasets are validated, hashed and stored with source/venue/transport/fallback provenance in a research database separate from trading state. Walk-forward validation separates chronological training and later test windows, supports rolling/expanding folds, and stores fold/trial/metric artifacts. Research agents use strict schemas and human-review gates.

## 8. API and dashboard

FastAPI starts the market-data service, multi-symbol monitor and paper trading runner through its lifespan. The API exposes health, market, decision, paper account, risk, signal, trade, execution, order, audit, event, equity and research/governance information. Admin mutations require `X-Admin-Token`.

The Next.js dashboard provides the trading control center and a read-only research/governance view. It displays market health, signals, paper positions/P&L, risk/safety state, orders/executions, strategy/watchlist/instrument information, experiments, walk-forward runs, gates, versions and promotion history.

## 9. Persistence

SQLAlchemy models store candles, signals, paper account/position/trades, orders, fills, order events, risk decisions/state/events, execution intents, testnet execution records, equity snapshots, alerts, audit/system events, kill-switch state, and strategy/governance records. Research datasets and experiments use the separately configured `RESEARCH_DATABASE_URL`.

## 10. Running locally

See [Deployment Guide](BOT_DEPLOYMENT_GUIDE.md). In brief: create a Python 3.12 virtual environment, install `requirements.txt`, copy `.env.example` to `.env`, keep the live-safety flags disabled, then run `python -m app` or `uvicorn app.main:app --reload`. Dashboard setup is in `dashboard/package.json`; Docker Compose can start backend and dashboard.

## 11. Safety and readiness

The repository's [Live Readiness Review](LIVE_READINESS_REVIEW.md) states **NOT APPROVED for live trading**. The live path is intentionally unavailable in this release. The existence of a testnet client, broker abstraction, risk controls or a dashboard must not be interpreted as live authorization.

Before considering a future live release, the project requires evidence for continuous paper soak, reviewed out-of-sample validation, tested risk limits, testnet round-trips/recovery, data quality, security, backup restoration, observability and named human approvals. No AI agent can waive these gates.

## 12. Detailed documents

- [Architecture](BOT_ARCHITECTURE.md)
- [Trading Logic](BOT_TRADING_LOGIC.md)
- [AI Agents](BOT_AI_AGENTS.md)
- [Risk Management](BOT_RISK_MANAGEMENT.md)
- [Execution Engine](BOT_EXECUTION_ENGINE.md)
- [Backtesting and Research](BOT_BACKTESTING_AND_RESEARCH.md)
- [Market Data Pipeline](BOT_DATA_PIPELINE.md)
- [API Reference](BOT_API_REFERENCE.md)
- [Dashboard Guide](BOT_DASHBOARD_GUIDE.md)
- [Security and Safety](BOT_SECURITY_AND_SAFETY.md)
- [Deployment Guide](BOT_DEPLOYMENT_GUIDE.md)
- [Limitations and Roadmap](BOT_LIMITATIONS_AND_ROADMAP.md)

---
*This manual documents the code and project reports at the reviewed revision. Where behavior depends on runtime configuration or external services, validate it in the target environment.*
