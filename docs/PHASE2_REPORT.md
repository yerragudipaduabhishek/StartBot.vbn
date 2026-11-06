# Phase 2 Implementation Report

**Date:** 2026-11-01
**Scope:** Phase 1 verification/hardening and Phase 2 market-data/research engine.
**Live trading:** disabled. No production order path was enabled, constructed by paper mode, or
exercised by the test suite.

## Result

- Full suite: **419 passed, 0 failed, 0 skipped** (6 warnings).
- Baseline before Phase 2: 391 passed.
- New coverage: 28 tests (11 Phase 1 gap regressions, 17 Phase 2 data/research).
- Environment: Python 3.12.14 in `C:\Users\abhia\AppData\Local\Temp\opencode\btcvenv`.
- The original `.venv` is unusable because its Python 3.11 WindowsApps base is missing.
- Git CLI is unavailable; repository state was inspected with `dulwich` at HEAD
  `91623ef95ee3c693c282d24c7dee2c3c63f04e68`. No commit was created.

## Implemented

### Phase 1 verification and hardening

- Verified mode policy, profile validation, router authorization, risk gating, research/backtest
  persistence rejection, kill switch, paper isolation, and live-disable flags.
- Hardened `Executor.can_trade_live` to always return `False` and stopped paper mode from constructing
  a `DeltaClient` unless the profile is `None`/testnet and credentials are eligible.
- Added regressions for failed persistence halting entries, local/exchange size mismatch, malformed
  signals, paper router isolation, paper executor client construction, and live-off flags.

### Market data

- `app/data/quality.py`: quality report and validation for duplicates, ordering, OHLCV validity,
  symbol/timeframe identity, alignment, unclosed bars, staleness, and gaps.
- `app/data/dataset.py`: canonical frames, SHA-256 dataset hashes, validated metadata, JSON metadata
  artifacts, database loading, and recursive REST pagination that cannot silently truncate history.
- Candle persistence now records source, venue, transport, fallback use, closed-candle status, and
  fetch time. Existing SQLite databases receive additive migrations.
- Backfill, deep backfill, and resampling preserve provenance.
- `DeltaClient` exposes `last_read_meta` and `get_candles_with_metadata`; `MarketData.snapshot`
  exposes data provenance and fallback state.

### Features and backtests

- `app/features/pipeline.py` (`phase2.v1`) is the single causal feature definition.
- Integrated into live signal generation, backtests, the soak runner, and the execution CLI.
- Backtest results carry dataset hash, dataset metadata, and feature-pipeline version.
- Tests prove prefix/full parity and future-bar independence.

### Research storage and experiments

- `app/research/models.py`: isolated `research_datasets`, `research_experiments`, and
  `research_news_events` tables.
- `app/research/store.py`: dataset/experiment/news recording, dataset-before-experiment enforcement,
  rejection, and optional reproduction-validated promotion to `validated`.
- `app/research/experiment.py`: deterministic experiment runner linking backtest output to dataset
  hash and research storage.
- `RESEARCH_DATABASE_URL` defaults to `sqlite:///./data/research.db`; tests confirm research tables
  are absent from the trading database.
- Parameter study loads datasets with metadata and records selection/OOS experiments; `--no-record`
  disables research writes.

### News

- RSS/Atom parsing with source, URL, publication/fetch timestamps, provider, and symbols.
- Backward-compatible headline views.
- Provider failures return no headlines and never block technical signal generation.

## Verification

```bash
C:\Users\abhia\AppData\Local\Temp\opencode\btcvenv\Scripts\python.exe -m pytest -q
```

Result: `419 passed, 6 warnings in 30.80s` (final run; log at
`C:\Users\abhia\AppData\Local\Temp\opencode\phase2_final.txt`). Warnings are pre-existing pytest
collection warnings for testnet class names plus a Starlette/httpx deprecation warning.

## Remaining risks

- `TestnetValidator --place-orders` can call the testnet order endpoint directly without the strategy
  risk gate. It is a manual diagnostics path, not the paper/live strategy path; it should remain
  disabled for normal operation or be removed in a later phase.
- Research isolation is configuration-level (separate SQLite URL), not OS/process-level. Production
  use should add a separate database role/process.
- Dataset hashes prove content identity; they do not prove vendor correctness.
- Strategy profitability is not asserted or proven by this phase.
- Existing user worktree changes were preserved; no commit or push was performed.
