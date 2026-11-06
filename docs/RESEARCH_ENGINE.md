# Research Engine

**Status:** Phase 2 implemented (2026-11-01). This layer is offline-only. It cannot submit orders,
write trading state, or enable live execution.

## Guarantees

1. **Validated data.** A dataset is rejected on duplicate timestamps, out-of-order rows, malformed
   OHLCV, wrong symbol/timeframe, unaligned timestamps, unclosed bars, or an empty result. Gap and
   staleness checks are available explicitly.
2. **Reproducible identity.** `dataset_hash` is SHA-256 over canonical candle content and the
   symbol/timeframe schema. It is independent of input ordering, file path, source, and metadata.
3. **Provenance.** Datasets record source, venue, transport, endpoint, fallback use, fetch time,
   pagination counts, row/range bounds, and quality statistics.
4. **No silent truncation.** `fetch_dataset` splits a window whenever the API returns a full page,
   because Delta returns the newest rows of an oversized window.
5. **One feature implementation.** Live signals, backtests, soak runs, and experiments use
   `app/features/pipeline.py` (`FEATURE_PIPELINE_VERSION = "phase2.v1"`). Indicators are causal;
   prefix and full-frame values match at the same bar.
6. **Storage isolation.** Research tables live in `RESEARCH_DATABASE_URL` (default
   `sqlite:///./data/research.db`), never in the paper/trading database.
7. **Lineage before results.** An experiment cannot be recorded unless its dataset hash is already
   recorded. Validation can require a callback proving the stored configuration reproduces.
8. **Optional news.** A news-provider failure yields no headlines and never blocks technical signal
   generation or paper execution.

## Data path

```text
DB candles or Delta REST
  -> build_dataset / fetch_dataset
       -> validate_candles (critical defects reject)
       -> closed-candle filter
       -> canonical ascending frame
       -> dataset_hash + DatasetMetadata
  -> FeaturePipeline (phase2.v1)
  -> run_backtest / run_experiment
  -> ResearchStore
       -> research_datasets
       -> research_experiments
```

### Dataset metadata

`DatasetMetadata.as_dict()` includes symbol, timeframe, row count, hash, requested/actual time
bounds, first/last candle time, source, venue, transport, endpoint, fallback flag, fetch time,
closed-only flag, request/page counts, pagination completeness, quality report, and schema version.
`save_metadata()` / `load_metadata()` provide a portable JSON artifact.

### Quality policy

| Condition | Research dataset |
|---|---|
| Duplicate timestamp | reject |
| Descending input | reject |
| Invalid/zero/NaN OHLCV or high/low inconsistency | reject |
| Wrong symbol or timeframe | reject |
| Timestamp not aligned to timeframe | reject |
| Unclosed bar (with `closed_only=True`) | dropped and counted |
| Gap | allowed by default; reject with `strict=True` |
| Stale final bar | reported; opt-in rejection |

Input with an unclosed final bar is expected for live-rest datasets. The bar is excluded from the
dataset and recorded as `quality.unclosed_dropped`.

## Features

`FeaturePipeline(ema_spans=...)` validates required OHLCV columns, numeric values, unique ascending
timestamps, then applies the existing causal indicators. It adds `bar_index`,
`feature_pipeline_version`, and optional `candle_close_time`/`is_closed`. `ensure_features()` lets
backtests reuse features already computed by the same pipeline.

The backtest accepts an explicit `ema_spans` tuple so parameter studies remain compatible, while live
signals use the profile's `ema_spans`. Every `BacktestResult` records the pipeline version.

## Research storage

```python
from app.research.store import ResearchStore
from app.research.experiment import run_experiment

store = ResearchStore()  # RESEARCH_DATABASE_URL
outcome = run_experiment(
    candles,
    engine,
    risk_manager,
    profile=profile,
    params={"threshold": 0.25},
    store=store,
)
print(outcome.dataset.dataset_hash, outcome.experiment.experiment_id)
```

Experiment records contain strategy identity/version, symbol, timeframe, dataset hash/range,
parameters, assumptions, metrics, profile snapshot, fee/slippage, feature-pipeline version, code
version, status, and rejection/validation timestamps. `validate_experiment()` refuses rejected
experiments and requires a non-empty recorded dataset; when `reproduce` is supplied it must return
`True` against the recorded dataset.

`reject_experiment()` records a terminal rejected status without deleting evidence.

## CLI

```bash
# Validate/record a study without writing research rows
python -m app.backtest.study --mode baseline --no-record

# Run the study and record datasets/experiments
python -m app.backtest.study --mode baseline

# Backfill recent and deep history (provenance-aware)
python -m app.data.historical --symbol BTCUSD --resolution 1h --days 90
```

During a long study, dataset rows are recorded once per hash and experiment rows are appended per
selection/OOS evaluation.

## News

`fetch_news_items()` handles RSS and Atom feeds and returns `NewsItem` objects with headline, source,
URL, publication timestamp, fetch timestamp, provider, and symbols. `fetch_headlines()` remains the
string-only compatibility view. The trading runner catches all news getter failures and continues
with an empty headline list.

## Boundaries and limitations

- The research store and experiment runner have no order/execution path. They operate on
  caller-supplied strategy/risk objects; backtests use a non-persisting research risk copy when
  needed. `app.research` itself does not import `app.execution` or `app.trading` order paths.
- The research database is isolated by configuration, not by a separate OS user/process. A production
  deployment should use a separate database role and process.
- Dataset hashes prove identity of the candle content, not data correctness or vendor honesty.
- News is context only; it is not part of the deterministic entry decision.
- No strategy promotion, canary, or live-readiness path is enabled by this phase.
- The `TestnetValidator --place-orders` diagnostic can call the testnet order endpoint directly. It
  is not the strategy execution path and remains a manual validation tool; it must not be used as a
  trading entry point.

## Verification

```bash
pytest -q
```

Phase 2 coverage includes quality rejection, alignment/staleness, deterministic hashing, metadata
round-trips, recursive pagination, persisted provenance, prefix/full feature parity, future-bar
independence, research-table isolation, dataset-before-experiment enforcement, reproduction
validation, experiment recording, RSS/Atom parsing, and fail-open news.
