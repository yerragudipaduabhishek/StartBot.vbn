"""Phase 2 research isolation, experiment reproducibility and news provenance."""

from __future__ import annotations

import json
import sqlite3
import time

import pytest
import requests

from app.config.trading import profile_from_settings
from app.data.dataset import build_dataset
from app.data.news import NewsItem, fetch_headlines, fetch_news_items, headlines_from
from app.research.experiment import run_experiment
from app.research.store import ResearchStorageError, ResearchStore
from app.risk.risk_manager import RiskManager
from app.strategy.signal_engine import SignalEngine


def _rows(n: int = 10) -> list[dict]:
    interval = 3600
    last = (int(time.time()) // interval) * interval - interval
    out = []
    for i in range(n):
        close = 50_000.0 + i * 25.0
        out.append(
            {
                "time": last - (n - 1 - i) * interval,
                "open": close,
                "high": close + 40,
                "low": close - 40,
                "close": close,
                "volume": 10.0,
            }
        )
    return out


def _store(tmp_path) -> ResearchStore:
    return ResearchStore(database_url=f"sqlite:///{tmp_path / 'research.db'}")


def _dataset(store: ResearchStore):
    _, metadata = build_dataset(_rows(20), symbol="BTCUSDT", timeframe="1h", source="test")
    store.record_dataset(metadata)
    return metadata


def test_research_store_uses_a_database_without_trading_tables(tmp_path):
    store = _store(tmp_path)
    _dataset(store)
    with sqlite3.connect(tmp_path / "research.db") as db:
        tables = {row[0] for row in db.execute("select name from sqlite_master where type='table'")}
    assert "research_datasets" in tables
    assert "research_experiments" in tables
    assert "candles" not in tables
    assert "signals" not in tables
    assert "paper_trades" not in tables
    assert "risk_state" not in tables
    assert "testnet_executions" not in tables


def test_experiment_requires_recorded_dataset_and_can_be_validated(tmp_path):
    store = _store(tmp_path)
    metadata = _dataset(store)
    experiment = store.record_experiment(
        strategy_id="strategy_a",
        strategy_version="1.0.0",
        symbol="BTCUSDT",
        timeframe="1h",
        dataset_hash=metadata.dataset_hash,
        dataset_start=metadata.first_candle_time,
        dataset_end=metadata.last_candle_time,
        params={"threshold": 0.25},
        metrics={"trades": 3, "return_pct": 1.5},
        fee_pct=0.05,
        slippage_bps=1.0,
        feature_pipeline_version="phase2.v1",
    )
    assert experiment.status == "completed"
    assert store.get_dataset(metadata.dataset_hash) is not None
    validated = store.validate_experiment(experiment.experiment_id)
    assert validated.status == "validated"
    assert validated.validated_at is not None

    with pytest.raises(ResearchStorageError, match="dataset"):
        store.record_experiment(
            strategy_id="strategy_a",
            strategy_version="1.0.0",
            symbol="BTCUSDT",
            timeframe="1h",
            dataset_hash="missing",
            params={},
            metrics={},
        )


def test_experiment_validation_requires_reproduction_when_callback_supplied(tmp_path):
    store = _store(tmp_path)
    metadata = _dataset(store)
    experiment = store.record_experiment(
        strategy_id="strategy_a",
        strategy_version="1.0.0",
        symbol="BTCUSDT",
        timeframe="1h",
        dataset_hash=metadata.dataset_hash,
        params={},
        metrics={},
    )
    with pytest.raises(ResearchStorageError, match="reproduce"):
        store.validate_experiment(experiment.experiment_id, reproduce=lambda _exp, _data: False)
    assert store.get_experiment(experiment.experiment_id).status == "completed"


def test_run_experiment_records_dataset_hash_and_metrics(tmp_path):
    store = _store(tmp_path)
    profile = profile_from_settings(mode="backtest")
    risk = RiskManager(starting_equity=10_000, settings=profile.risk, execution_mode=profile.mode)
    engine = SignalEngine(profile=profile, risk_manager=risk)
    candles = _rows(300)
    outcome = run_experiment(
        candles,
        engine,
        risk,
        profile=profile,
        params={"threshold": 0.25},
        store=store,
        initial_equity=10_000,
        warmup_bars=60,
    )
    assert outcome.experiment.dataset_hash == outcome.dataset.dataset_hash
    assert store.get_dataset(outcome.dataset.dataset_hash) is not None
    stored = store.get_experiment(outcome.experiment.experiment_id)
    assert stored is not None
    assert isinstance(json.loads(stored.metrics_json), dict)
    assert stored.feature_pipeline_version
    assert outcome.result.dataset_hash == outcome.dataset.dataset_hash


class _Response:
    def __init__(self, content: bytes):
        self.content = content

    def raise_for_status(self):
        return None


RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
<item><title>Bitcoin rallies</title><link>https://example.test/a</link><pubDate>Tue, 01 Jan 2030 00:00:00 GMT</pubDate></item>
<item><title>Second story</title><link>https://example.test/b</link></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Atom story</title><link href="https://example.test/atom"/><updated>2030-01-01T00:00:00Z</updated></entry>
</feed>"""


def test_news_items_carry_source_timestamp_and_url(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: _Response(RSS))
    items = fetch_news_items("https://news.example.test/rss", limit=5)
    assert len(items) == 2
    assert items[0].headline == "Bitcoin rallies"
    assert items[0].source == "news.example.test"
    assert items[0].url == "https://example.test/a"
    assert items[0].published_at is not None
    assert fetch_headlines("https://news.example.test/rss") == ["Bitcoin rallies", "Second story"]

    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: _Response(ATOM))
    atom_items = fetch_news_items("https://news.example.test/atom")
    assert atom_items[0].headline == "Atom story"
    assert atom_items[0].url == "https://example.test/atom"


def test_news_failure_is_empty_and_never_blocks_the_runner(monkeypatch):
    def fail(*args, **kwargs):
        raise requests.RequestException("feed down")

    monkeypatch.setattr(requests, "get", fail)
    assert fetch_news_items() == []
    assert fetch_headlines() == []

    from app.trading.runner import TradingRunner

    runner = object.__new__(TradingRunner)
    runner.news_getter = lambda: (_ for _ in ()).throw(RuntimeError("provider down"))
    assert runner._headlines() == []


def test_headline_normalisation_accepts_items_dicts_and_strings():
    item = NewsItem(headline="headline", source="source")
    assert headlines_from([item, {"headline": "dict headline"}, "plain", "  "]) == [
        "headline",
        "dict headline",
        "plain",
    ]
