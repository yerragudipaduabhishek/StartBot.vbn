"""Phase 2 data quality, dataset reproducibility, provenance and features."""

from __future__ import annotations

import time

import pandas as pd
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.backtest.engine import run_backtest
from app.config.trading import profile_from_settings
from app.data.dataset import (
    build_dataset,
    dataset_hash,
    fetch_dataset,
    load_candles_from_db,
    load_metadata,
    save_metadata,
)
from app.data.historical import backfill
from app.data.news import NewsItem
from app.data.quality import DataQualityError, validate_candles
from app.research.models import ResearchNewsEvent
from app.research.store import ResearchStore
from app.database.database import Base
from app.database.models import Candle
from app.features.pipeline import FeaturePipeline, FeaturePipelineError, REQUIRED_FEATURE_COLUMNS
from app.risk.config import RiskSettings
from app.risk.risk_manager import RiskManager
from app.strategy.signal_engine import SignalEngine


def _rows(n: int, *, interval: int = 3600, end_offset: int = 1) -> list[dict]:
    """Aligned closed candles ending ``end_offset`` intervals before now."""
    now = int(time.time())
    last = (now // interval) * interval - end_offset * interval
    out = []
    for i in range(n):
        close = 50_000.0 + i * 25.0
        out.append(
            {
                "time": last - (n - 1 - i) * interval,
                "open": close,
                "high": close + 40.0,
                "low": close - 40.0,
                "close": close,
                "volume": 10.0,
            }
        )
    return out


def test_quality_detects_duplicates_order_gaps_invalid_and_unclosed():
    rows = _rows(6)
    duplicate = validate_candles([rows[0], rows[0], *rows[1:]], symbol="BTCUSDT", timeframe="1h")
    assert duplicate.duplicate_count == 1
    assert duplicate.critical_ok is False

    out_of_order = validate_candles(list(reversed(rows)), symbol="BTCUSDT", timeframe="1h")
    assert out_of_order.out_of_order is True

    gapped = validate_candles([*rows[:2], *rows[3:]], symbol="BTCUSDT", timeframe="1h")
    assert gapped.missing_bars == 1
    assert gapped.complete is False

    invalid = [dict(row) for row in rows]
    invalid[2]["close"] = 0.0
    bad = validate_candles(invalid, symbol="BTCUSDT", timeframe="1h")
    assert bad.invalid_count == 1

    current = (int(time.time()) // 3600) * 3600
    future_bar = [dict(rows[0], time=current)]
    unclosed = validate_candles(future_bar, symbol="BTCUSDT", timeframe="1h")
    assert unclosed.unclosed_count == 1

    with pytest.raises(DataQualityError):
        validate_candles([rows[0], rows[0], *rows[1:]], symbol="BTCUSDT", timeframe="1h", strict=True)


def test_quality_detects_wrong_symbol_timeframe_alignment_and_stale_data():
    rows = _rows(4)
    rows[1] = dict(rows[1], symbol="ETHUSDT", resolution="5m")
    report = validate_candles(rows, symbol="BTCUSDT", timeframe="1h")
    assert report.wrong_symbol_count == 1
    assert report.wrong_timeframe_count == 1

    misaligned = [dict(rows[0], time=rows[0]["time"] + 60)]
    assert validate_candles(misaligned, symbol="BTCUSDT", timeframe="1h", require_closed=False).unaligned_count == 1

    stale = validate_candles(_rows(3, end_offset=10), symbol="BTCUSDT", timeframe="1h", check_stale=True)
    assert stale.stale is True


def test_build_dataset_drops_unclosed_bars_and_hashes_content_only():
    now = int(time.time())
    bucket = (now // 3600) * 3600
    rows = []
    for i in range(10):
        close = 50_000.0 + i * 10
        rows.append(
            {
                "time": bucket - (9 - i) * 3600,
                "open": close,
                "high": close + 20,
                "low": close - 20,
                "close": close,
                "volume": 5.0,
            }
        )
    df, metadata = build_dataset(
        rows,
        symbol="BTCUSDT",
        timeframe="1h",
        now=now,
        source="test",
        venue="test-venue",
        transport="rest",
    )
    assert len(df) == 9
    assert metadata.rows == 9
    assert metadata.quality["unclosed_dropped"] == 1
    assert metadata.dataset_hash == dataset_hash(rows[:-1], symbol="BTCUSDT", timeframe="1h")

    # Hash is independent of input ordering and provenance.
    reverse_hash = dataset_hash(list(reversed(rows[:-1])), symbol="BTCUSDT", timeframe="1h")
    assert reverse_hash == metadata.dataset_hash

    # Critical defects reject, rather than silently producing a research dataset.
    with pytest.raises(DataQualityError):
        build_dataset([rows[0], rows[0], *rows[1:]], symbol="BTCUSDT", timeframe="1h", now=now)
    with pytest.raises(DataQualityError):
        build_dataset(list(reversed(rows)), symbol="BTCUSDT", timeframe="1h", now=now)


def test_dataset_metadata_round_trips(tmp_path):
    _, metadata = build_dataset(_rows(5), symbol="BTCUSDT", timeframe="1h", source="test")
    path = save_metadata(metadata, tmp_path / "dataset.json")
    restored = load_metadata(path)
    assert restored.dataset_hash == metadata.dataset_hash
    assert restored.quality["row_count"] == metadata.rows


def test_fetch_dataset_paginates_and_records_provenance():
    interval = 300
    rows = _rows(1000, interval=interval, end_offset=1)
    by_time = {row["time"]: row for row in rows}

    class PagedClient:
        def __init__(self):
            self.calls = 0
            self.last_read_meta = {
                "source": "delta",
                "venue": "fallback.example",
                "endpoint": "/v2/history/candles",
                "fallback_used": True,
                "fetched_at": int(time.time()),
            }

        def get_candles_with_metadata(self, symbol, resolution, start, end):
            self.calls += 1
            window = [row for t, row in sorted(by_time.items()) if int(start) <= t <= int(end)]
            return window[-100:]

        def get_candles(self, symbol, resolution, start, end):
            return self.get_candles_with_metadata(symbol, resolution, start, end)

    client = PagedClient()
    start = rows[0]["time"]
    end = rows[-1]["time"] + interval
    df, metadata = fetch_dataset(
        client,
        symbol="BTCUSDT",
        timeframe="5m",
        start=start,
        end=end,
        max_rows=100,
        now=int(time.time()),
    )
    assert len(df) == 1000
    assert metadata.rows == 1000
    assert metadata.request_count > 1
    assert metadata.pagination_complete is True
    assert metadata.fallback_used is True
    assert metadata.venue == "fallback.example"


def test_historical_backfill_persists_provenance_and_closed_flag():
    rows = _rows(5)
    fetched_at = int(time.time())

    class StubClient:
        last_read_meta = {
            "source": "delta",
            "venue": "testnet-api.delta.exchange",
            "transport": "rest",
            "fallback_used": True,
            "fetched_at": fetched_at,
        }

        def get_candles(self, symbol, resolution, start, end):
            return rows

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        inserted = backfill("BTCUSDT", "1h", hours=10, client=StubClient(), session=session)
        session.commit()
        assert inserted == 5
        stored = session.execute(select(Candle).order_by(Candle.time)).scalars().all()
        assert len(stored) == 5
        assert all(row.source == "delta" for row in stored)
        assert all(row.venue == "testnet-api.delta.exchange" for row in stored)
        assert all(row.transport == "rest" for row in stored)
        assert all(row.fallback_used is True for row in stored)
        assert all(row.is_closed is True for row in stored)


def test_ingestion_quarantines_invalid_rows_and_excludes_them_from_datasets():
    rows = _rows(5)
    rows[2] = dict(rows[2], close=0.0)

    class StubClient:
        last_read_meta = {"source": "delta", "venue": "testnet", "transport": "rest", "fetched_at": int(time.time())}

        def get_candles(self, symbol, resolution, start, end):
            return rows

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        assert backfill("BTCUSDT", "1h", hours=10, client=StubClient(), session=session) == 5
        stored = session.execute(select(Candle).order_by(Candle.time)).scalars().all()
        assert [row.quality_status for row in stored] == ["ok", "ok", "quarantined", "ok", "ok"]
        assert "invalid_ohlcv" in stored[2].quality_flags
        loaded = load_candles_from_db("BTCUSDT", "1h", session=session, closed_only=False)
        assert len(loaded) == 4
        assert 0.0 not in loaded["close"].tolist()


def test_news_records_are_deduplicated_by_content_hash(tmp_path):
    store = ResearchStore(f"sqlite:///{tmp_path / 'research.db'}")
    item = NewsItem(headline="Bitcoin breaks resistance", source="example.com", url="https://example.com/a")
    first = store.record_news(item)
    second = store.record_news(item)
    assert first.id == second.id
    with store.SessionLocal() as db:
        rows = db.execute(select(ResearchNewsEvent)).scalars().all()
    assert len(rows) == 1
    assert rows[0].headline_hash == item.content_hash


def test_feature_pipeline_prefix_matches_full_and_has_no_lookahead():
    rows = _rows(180, interval=300)
    pipeline = FeaturePipeline(ema_spans=(8, 40, 50))
    full = pipeline.transform(pd.DataFrame(rows), timeframe="5m")
    for index in (60, 100, 150):
        prefix = pipeline.transform(pd.DataFrame(rows[: index + 1]), timeframe="5m")
        for column in REQUIRED_FEATURE_COLUMNS:
            assert full[column].iloc[index] == pytest.approx(prefix[column].iloc[-1])

    altered = [dict(row) for row in rows]
    for row in altered[151:]:
        row["close"] = row["close"] * 10.0
        row["high"] = row["high"] * 10.0
    altered_features = pipeline.transform(pd.DataFrame(altered), timeframe="5m")
    for column in REQUIRED_FEATURE_COLUMNS:
        assert altered_features[column].iloc[150] == pytest.approx(full[column].iloc[150])


def test_feature_pipeline_rejects_duplicate_or_unsorted_rows():
    rows = _rows(10, interval=300)
    with pytest.raises(FeaturePipelineError):
        FeaturePipeline().transform(pd.DataFrame([rows[0], rows[0], *rows[1:]]))
    with pytest.raises(FeaturePipelineError):
        FeaturePipeline().transform(pd.DataFrame(list(reversed(rows))))


def test_signal_engine_auto_builds_features_and_uses_profile_spans():
    profile = profile_from_settings(mode="backtest")
    engine = SignalEngine(profile=profile)
    assert engine.feature_pipeline.ema_spans == profile.ema_spans
    raw = pd.DataFrame(_rows(140))
    signal = engine.analyze_candle(raw)
    assert signal["direction"] in ("long", "short", "none")
    assert signal["entry"] is not None


def test_backtest_records_feature_pipeline_and_dataset_metadata():
    rows = _rows(300)
    engine = SignalEngine(risk_manager=RiskManager(starting_equity=10_000, settings=RiskSettings(min_confidence=0.0)))
    risk = engine.risk_manager
    metadata = {"dataset_hash": "abc123", "rows": len(rows)}
    result = run_backtest(
        rows,
        engine,
        risk,
        initial_equity=10_000,
        warmup_bars=60,
        dataset_metadata=metadata,
    )
    assert result.feature_pipeline_version
    assert result.dataset_hash == "abc123"
    assert result.dataset_metadata == metadata
