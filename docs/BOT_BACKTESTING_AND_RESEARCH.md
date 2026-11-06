# Backtesting and Research

## Backtest engine

`app/backtest/engine.py` implements a causal candle-based simulator. It deliberately separates signal time from execution time:

1. A signal is generated from a fully closed bar.
2. Entry is filled at the next bar open.
3. Stop and target checks use the next bar high/low.
4. If the next bar opens beyond a stop/target level, the fill uses the worse opening price.
5. Entry and exit fees are charged on their respective notionals.
6. Funding is charged at 8-hour boundaries while a position is open.
7. Any final open position is closed and charged at the final close.

The result object includes initial/final equity, trades, equity curve, signal count, fees, slippage, funding, benchmark prices, exposure, turnover, rejected orders and feature/dataset metadata. Derived metrics include return, win rate, profit factor, average win/loss, expectancy, maximum drawdown, exposure, average holding bars, turnover, CAGR, Sharpe and Sortino.

## Parameter optimization and studies

`app/backtest/optimize.py` performs bounded parameter search. `app/backtest/study.py` runs the strategy study and provides baseline, walk-forward and refit modes documented in README. Study runs use Strategy A profile/feature configuration and explicit cost assumptions. A research run should be reproducible from its command, dataset identity, code version and parameter set.

## Walk-forward validation

`app/backtest/walkforward.py` creates chronological rolling or expanding folds. Training windows precede their corresponding test windows. Optional embargo and warmup bars are represented in fold metadata. Optimization sees only training data; selected parameters are then evaluated on a later untouched test segment. Results retain fold spans, parameters, train/test metrics, trials, acceptance/rejection reason and aggregate results. Artifact bundles include a manifest, folds, metrics, trials and command.

Do not confuse a fixed-parameter walk-forward replay with a per-fold refit. The study CLI documents `--mode walk` and `--mode walk --refit` as separate workflows.

## Dataset integrity and provenance

The data layer validates candles and builds deterministic dataset metadata with SHA-256 hash and source/venue/transport/fallback provenance. The feature pipeline version and EMA configuration are part of the reproducibility context. Research storage is isolated from the trading database using `RESEARCH_DATABASE_URL`.

## Experiment lifecycle

`app/research/experiment.py` ties together a dataset, profile, engine, risk manager, backtest result and research store. Experiments reference recorded datasets and metrics. Validation can require a reproduction callback; governance requires recorded dataset identity.

## Research agents

Research agents are read-only LLM-assisted components. They produce typed market analysis, strategy hypotheses, code patch proposals, review findings and performance diagnostics. They cannot self-apply code or touch execution/risk/order modules. LLM network access is opt-in and budgeted.

## Governance lifecycle

The governance service supports:
1. proposal creation tied to a recorded dataset/experiment;
2. reviewer findings and veto;
3. named human approval with rationale;
4. canary start and canary result recording;
5. gate evaluation;
6. promotion of a paper strategy version;
7. rollback with actor and reason.

Default gate settings in the governance service include minimum folds, minimum trades, fold coverage, maximum drawdown and positive out-of-sample requirement. Check the active code/config for exact values before submitting a proposal. Governance is not an automatic optimizer-to-production path.

## Commands

```bash
python -m app.backtest.study --mode baseline --no-record
python -m app.backtest.study --mode baseline
python -m app.backtest.study --mode walk
python -m app.backtest.study --mode walk --refit
pytest -q
```

## Interpretation warnings

Backtests are models. They cannot prove future returns or reproduce every exchange microstructure effect. Validate dataset completeness, timeframe, fees, slippage, funding, sample size, fold coverage, regime diversity and implementation parity. The project's live-readiness document states that a 30-day field soak has not been run and live approval has not been granted.
