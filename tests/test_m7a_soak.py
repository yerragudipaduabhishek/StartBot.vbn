"""Phase 7A validation: local paper-trading soak-test runner.

Covers the required guarantees without touching the network:
  * startup + graceful shutdown,
  * persistence + restart recovery,
  * restart never auto-trades,
  * unavailable / stale / invalid-price data can never become a trade,
  * a single broken watchlist symbol does not stop the runner,
  * no order/account endpoint is ever used,
  * telemetry JSONL generation with all required fields.

The runner is exercised with pre-populated stub feeds and a stub signal engine;
the Strategy A *wiring* (weights, thresholds, EMAs, risk) is asserted directly.
"""

import asyncio
import inspect
import json
import time

import pytest

from app.config.settings import get_settings
from app.database.database import SessionLocal
from app.database.models import PaperTrade
from app.risk.config import RiskSettings
from app.risk.risk_manager import RiskManager
from app.soak.__main__ import build_parser
from app.soak.feed import SoakFeed
from app.soak.runner import BANNER, SoakRunner, TELEMETRY_FIELDS
from app.strategy.signal_engine import SignalEngine
from app.trading.paper_trader import PaperTrader

from tests.m3_fixtures import SingleSymbolStub, SymbolStub, make_candles


def _fresh_candles(n=200, base=50000.0, step=0.0, vol=0.005, age_secs=3500):
    last = int(time.time()) - age_secs
    return make_candles(n=n, base=base, step=step, vol=vol, t0=last - (n - 1) * 3600)


def _zero_close_candles(n=200):
    last = int(time.time()) - 60
    return [
        {"time": last - (n - 1) * 3600 + i * 3600, "open": 0.0, "high": 0.0, "low": 0.0, "close": 0.0, "volume": 0.0}
        for i in range(n)
    ]


def _advance(feed, step=3600):
    candles = [dict(c) for c in feed.candles]
    target = int(candles[-1]["time"]) + step
    # Keep synthetic candles in the past so fail-closed future-candle checks
    # exercise real logic; the runner still sees a genuinely newer bar.
    latest_allowed = int(time.time()) - 60
    if target > latest_allowed:
        target = max(int(candles[-1]["time"]) + 1, latest_allowed)
    candles[-1]["time"] = target
    feed.candles = candles


class FakeEngine:
    def __init__(self, sig=None, raise_on_call=False) -> None:
        self.sig = sig or {
            "direction": "long",
            "score": 1.0,
            "confidence": 0.8,
            "entry": 50000.0,
            "stop_loss": 49000.0,
            "target": 52000.0,
            "accepted_size": 25.0,
            "risk_allowed": True,
            "explanation": "stub engine",
        }
        self.calls = 0
        self.raise_on_call = raise_on_call

    def analyze_candle(self, df, ticker=None, headlines=None, end_idx=None):
        self.calls += 1
        if self.raise_on_call:
            raise RuntimeError("boom")
        return dict(self.sig)


class RaisingClient:
    def get_candles(self, *a, **k):
        raise RuntimeError("connection refused")

    def get_ticker(self, *a, **k):
        raise RuntimeError("connection refused")

    def get_products(self, *a, **k):
        raise RuntimeError("connection refused")


def _stub_feed(symbol, candles, fails=False):
    if fails:
        return SoakFeed(symbol, client=RaisingClient(), enable_ws=False)
    if candles:
        ticker = {"close": candles[-1]["close"], "oi_value_usd": 100, "funding_rate_8h": 0.0001}
    else:
        ticker = {}
    return SoakFeed(symbol, client=SingleSymbolStub(candles=candles, ticker=ticker), enable_ws=False)


def _runner(
    symbol="BTCUSDT",
    candles_by_symbol=None,
    engine=None,
    risk=None,
    session_factory=None,
    out_path=None,
    fails=(),
):
    candles_by_symbol = candles_by_symbol or {
        "BTCUSDT": _fresh_candles(),
        "ETHUSDT": _fresh_candles(base=3000.0, step=3.0),
    }
    feeds = {s: _stub_feed(s, candles_by_symbol[s], fails=(s in fails)) for s in candles_by_symbol}
    for f in feeds.values():  # mimic a real boot: feeds poll once before the first cycle
        try:
            f.poll()
        except Exception:
            pass
    risk = risk or RiskManager(
        starting_equity=5000.0,
        settings=RiskSettings(
            risk_per_trade_pct=0.5,
            min_confidence=0.5,
            sl_atr_multiple=1.0,
            target_atr_multiple=2.0,
        ),
    )
    engine = engine or FakeEngine()
    paper = PaperTrader(
        starting_equity=5000.0,
        session_factory=session_factory,
        fee_pct=0.05,
        slippage_pct=0.02,
        symbol=symbol,
    )
    return SoakRunner(
        symbol=symbol,
        watchlist=list(candles_by_symbol),
        risk=risk,
        engine=engine,
        paper=paper,
        feeds=feeds,
        telemetry_path=out_path,
    )


# --------------------------------------------------------------- banner/CLI


def test_banner_declares_paper_only_mode():
    for line in ("PAPER TRADING", "NOT REAL FUNDS", "NO EXCHANGE ORDERS", "LIVE OFF"):
        assert line in BANNER


def test_cli_parser_defaults_are_indefinite_paper_mode():
    p = build_parser()
    args = p.parse_args([])
    assert args.duration == 0.0  # 0 == indefinite
    assert args.tick == 30.0
    assert args.poll == 45.0
    assert args.resolution == "1h"
    assert args.no_ws is False
    assert args.out == "data/soak_telemetry.jsonl"
    long_args = p.parse_args(["--duration", "3600", "--no-ws"])
    assert long_args.duration == 3600.0
    assert long_args.no_ws is True


# ------------------------------------------------------- Strategy-A wiring


def test_runner_wires_strategy_a_exactly():
    runner = SoakRunner(enable_ws=False)
    assert runner.ema_spans == (8, 40, 50)
    assert isinstance(runner.engine, SignalEngine)
    assert runner.engine.feature_pipeline.ema_spans == (8, 40, 50)
    assert runner.engine.decision_agent.weights == {"trend": 0.4, "structure": 0.0, "sentiment": 0.15, "derivatives": 0.15}
    assert runner.engine.decision_threshold == 0.25
    assert runner.engine.contract_value == get_settings().paper_contract_value
    assert runner.risk.settings.risk_per_trade_pct == 0.5
    assert runner.risk.settings.min_confidence == 0.5
    assert runner.risk.settings.sl_atr_multiple == 1.0
    assert runner.risk.settings.target_atr_multiple == 2.0
    assert runner.paper.starting_equity == 5000.0
    assert {"BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT"} <= set(runner.watchlist)
    for sym in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT"):
        assert sym in runner.feeds


# ------------------------------------------------------------ startup flow


def test_startup_seeds_and_never_trades_first_candle(tmp_path):
    runner = _runner(out_path=str(tmp_path / "t.jsonl"))
    result = runner.cycle()
    assert result["action"] == "seeded"
    assert runner.engine.calls == 0
    assert runner.paper.position is None
    assert runner.processed_time is not None
    rows = {r["symbol"]: r for r in runner.telemetry.rows}
    trades = rows["BTCUSDT"]
    assert trades["market_data_status"] == "streaming"
    assert "seeded" in trades["skip_reason"]


def test_new_fresh_candle_opens_paper_position(tmp_path):
    runner = _runner(out_path=str(tmp_path / "t.jsonl"))
    runner.cycle()
    _advance(runner.feeds["BTCUSDT"])
    result = runner.cycle()
    assert runner.engine.calls == 1
    assert result["action"]["action"] == "open"
    assert runner.paper.position is not None
    assert runner.paper.position["side"] == "long"
    assert runner.paper.position["symbol"] == "BTCUSDT"
    assert runner.paper.position["size"] > 0
    assert runner.paper.position["stop_loss"] == 49000.0
    assert runner.paper.position["target"] == 52000.0


# ---------------------------------------------------------------- recovery


def test_restart_recovers_state_and_does_not_auto_trade(tmp_path):
    # A: open a persisted paper position.
    runner_a = _runner(session_factory=SessionLocal, out_path=str(tmp_path / "a.jsonl"))
    runner_a.cycle()
    _advance(runner_a.feeds["BTCUSDT"])
    runner_a.cycle()
    assert runner_a.paper.position is not None
    balance_before = runner_a.paper.balance

    # B: fresh runner over the same DB - must restore, not re-trade.
    runner_b = _runner(session_factory=SessionLocal, out_path=str(tmp_path / "b.jsonl"))
    result = runner_b.cycle()
    assert runner_b.paper.position is not None
    assert runner_b.paper.position["side"] == "long"
    assert result["action"] == "seeded"
    assert runner_b._closed_trade_count() == 0
    assert runner_b.paper.balance == balance_before

    # Even a brand-new fresh candle cannot open a second position or close it.
    _advance(runner_b.feeds["BTCUSDT"])
    result = runner_b.cycle()
    assert result["action"]["action"] == "hold"
    assert runner_b.paper.position is not None
    assert runner_b._closed_trade_count() == 0

    with SessionLocal() as db:
        assert db.query(PaperTrade).count() == 0


# -------------------------------------------------------- data reliability


def test_unavailable_data_produces_no_signal(tmp_path):
    feed = SoakFeed("BTCUSDT", client=RaisingClient(), enable_ws=False)
    snap = feed.snapshot()
    assert snap["data_status"] == "unavailable"
    assert snap["price_valid"] is False
    assert snap["candles_count"] == 0

    runner = _runner(candles_by_symbol={"BTCUSDT": _fresh_candles()}, fails=("BTCUSDT",))
    result = runner.cycle()
    assert result["action"] == "noop"
    assert runner.engine.calls == 0
    assert runner.telemetry.latest()["market_data_status"] == "unavailable"
    assert runner.feeds["BTCUSDT"].last_poll_error == "connection refused"
    assert runner.feeds["BTCUSDT"].last_poll_error == "connection refused"

    with pytest.raises(RuntimeError):
        feed.poll()
    assert feed.snapshot()["last_error"] == "connection refused"


def test_stale_new_candle_never_signal(tmp_path):
    old = _fresh_candles(age_secs=7200)
    runner = _runner(candles_by_symbol={"BTCUSDT": old, "ETHUSDT": _fresh_candles(base=3000.0)})
    runner.cycle()  # seeds regardless of staleness
    _advance(runner.feeds["BTCUSDT"])
    result = runner.cycle()
    assert result["action"] == "noop"
    assert "stale" in result["reason"]
    assert runner.engine.calls == 0
    rows = {r["symbol"]: r for r in runner.telemetry.rows}
    assert rows["BTCUSDT"]["skip_reason"].startswith("data_status=stale")


def test_invalid_price_never_signal(tmp_path):
    bad = _zero_close_candles()
    runner = _runner(candles_by_symbol={"BTCUSDT": bad, "ETHUSDT": _fresh_candles(base=3000.0)})
    assert runner.cycle()["action"] == "seeded"
    _advance(runner.feeds["BTCUSDT"])
    result = runner.cycle()
    assert result["action"] == "noop"
    assert "price invalid" in result["reason"]
    assert runner.engine.calls == 0


def test_one_symbol_failure_does_not_stop_others(tmp_path):
    runner = _runner(fails=("ETHUSDT",))
    runner.cycle()
    _advance(runner.feeds["BTCUSDT"])
    result = runner.cycle()  # must not raise even though ETH is down
    assert runner.engine.calls == 1
    assert result["action"]["action"] == "open"

    rows = {r["symbol"]: r for r in runner.telemetry.rows}
    assert rows["BTCUSDT"]["market_data_status"] == "streaming"
    assert rows["ETHUSDT"]["market_data_status"] == "unavailable"
    assert rows["ETHUSDT"]["last_error"] == "connection refused"
    assert rows["ETHUSDT"]["price_valid"] is False


def test_unexpected_signal_error_is_recorded_not_swallowed(tmp_path):
    runner = _runner(engine=FakeEngine(raise_on_call=True), out_path=str(tmp_path / "t.jsonl"))
    runner.cycle()
    _advance(runner.feeds["BTCUSDT"])
    result = runner.cycle()
    assert result["action"] == "noop"
    assert "signal error" in result["reason"]
    assert runner._errors and runner._errors[-1]["source"] == "signal"
    assert "boom" in runner.telemetry.latest()["errors"][-1]


def test_risk_reason_survives_final_validation_rejection(tmp_path):
    engine = FakeEngine(
        sig={
            "direction": "short",
            "score": -0.8,
            "confidence": 0.9,
            "entry": 50000.0,
            "stop_loss": 51000.0,
            "target": 48000.0,
            "accepted_size": 25.0,
            "risk_allowed": False,
            "risk_reason": "daily loss limit hit",
            "explanation": "stub engine",
        }
    )
    runner = _runner(engine=engine, out_path=str(tmp_path / "t.jsonl"))
    runner.cycle()
    _advance(runner.feeds["BTCUSDT"])
    result = runner.cycle()

    assert result["action"]["action"] == "rejected"
    assert result["signal"]["risk_reason"] == "daily loss limit hit"
    row = [r for r in runner.telemetry.rows if r["symbol"] == "BTCUSDT"][-1]
    assert row["risk_reason"] == "daily loss limit hit"
    assert row["action_reason"] == "risk_allowed must be True for an entry signal"


# ------------------------------------------------------- order-endpoint guard


def test_source_never_touches_order_or_account_endpoints():
    from app.soak import feed, runner

    src = "\n".join([inspect.getsource(SoakRunner), inspect.getsource(SoakFeed), inspect.getsource(feed), inspect.getsource(runner)])
    forbidden = ("place_order", "get_orders", "cancel_order", "get_positions", "get_balances", "get_order_trades", "wallet/balances")
    for token in forbidden:
        assert token not in src, f"soak code must never reference {token}"


def test_paper_run_needs_no_api_credentials_and_only_uses_public_reads(tmp_path):
    feed = SoakFeed("BTCUSDT", enable_ws=False)
    assert feed.client.api_key == ""
    assert feed.client.api_secret == ""

    stub = SymbolStub(["BTCUSDT", "ETHUSDT"], prices={"BTCUSDT": 50000.0, "ETHUSDT": 3000.0})
    feeds = {
        "BTCUSDT": SoakFeed("BTCUSDT", client=stub, enable_ws=False),
        "ETHUSDT": SoakFeed("ETHUSDT", client=stub, enable_ws=False),
    }
    risk = RiskManager(
        starting_equity=5000.0,
        settings=RiskSettings(
            risk_per_trade_pct=0.5,
            min_confidence=0.5,
            sl_atr_multiple=1.0,
            target_atr_multiple=2.0,
        ),
    )
    paper = PaperTrader(starting_equity=5000.0, fee_pct=0.05, slippage_pct=0.02, symbol="BTCUSDT")
    runner = SoakRunner(
        symbol="BTCUSDT",
        watchlist=["BTCUSDT", "ETHUSDT"],
        risk=risk,
        engine=FakeEngine(),
        paper=paper,
        feeds=feeds,
        telemetry_path=str(tmp_path / "t.jsonl"),
    )
    for f in feeds.values():
        f.poll()
    runner.cycle()
    assert {name for name, _ in stub.calls} <= {"get_candles", "get_ticker", "get_products", "find_product"}


# ----------------------------------------------------------------- telemetry


def test_telemetry_rows_include_all_required_fields_and_are_written(tmp_path):
    out = tmp_path / "telemetry.jsonl"
    runner = _runner(out_path=str(out))
    runner.cycle()
    _advance(runner.feeds["BTCUSDT"])
    runner.cycle()
    runner.telemetry.flush()

    trading_rows = [r for r in runner.telemetry.rows if r["symbol"] == "BTCUSDT"]
    assert trading_rows, "expected at least one BTCUSDT telemetry row"
    row = trading_rows[-1]
    for field in TELEMETRY_FIELDS:
        assert field in row, f"missing telemetry field {field}"
    assert row["market_data_status"] == "streaming"
    assert row["data_source"] in ("rest", "websocket", "none")
    assert row["open_position"] == "long"
    assert row["entry_price"] is not None
    assert row["paper_balance"] == 5000.0
    assert row["errors"] == []

    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) >= 2
    parsed = [json.loads(line) for line in lines]
    assert all("timestamp" in p and "symbol" in p for p in parsed)
    assert {p["symbol"] for p in parsed} >= {"BTCUSDT", "ETHUSDT"}


def test_telemetry_reports_each_watchlist_symbol_independently(tmp_path):
    symbols = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT")
    candles = {symbol: _fresh_candles(base=50000.0 if symbol == "BTCUSDT" else 3000.0) for symbol in symbols}
    runner = _runner(candles_by_symbol=candles, out_path=str(tmp_path / "t.jsonl"))
    runner.cycle()

    rows = {row["symbol"]: row for row in runner.telemetry.rows}
    assert set(rows) == set(symbols)
    for symbol in symbols:
        assert rows[symbol]["market_data_status"] == "streaming"
        assert rows[symbol]["data_source"] == "rest"
        assert rows[symbol]["last_candle_age_secs"] < 3600
        assert rows[symbol]["cycle_number"] == 1


# ----------------------------------------------------------- run lifecycle


def test_run_loop_starts_and_stops_cleanly(tmp_path):
    runner = _runner(out_path=str(tmp_path / "t.jsonl"))
    asyncio.run(runner.run(duration=0.0))  # one cycle, then graceful shutdown
    assert runner._running is False
    assert runner.cycles_run == 1
    assert runner.telemetry.rows_written >= 2  # BTC + ETH rows flushed


def test_cancelled_run_cleans_up_feeds(tmp_path):
    runner = _runner(out_path=str(tmp_path / "t.jsonl"))

    async def _scenario():
        run_task = asyncio.create_task(runner.run())
        await asyncio.sleep(0.05)
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass

    asyncio.run(_scenario())
    assert runner._running is False
    assert all(not f._tasks for f in runner.feeds.values())
    assert runner.telemetry.rows_written == len(runner.telemetry.rows)


# ------------------------------------------- 1H freshness (soak root cause)


class FakeWs:
    def __init__(self, connected=True, last_message_age=10) -> None:
        self._connected = connected
        self._age = last_message_age

    def status(self):
        return {
            "running": self._connected,
            "connected": self._connected,
            "reconnects": 0,
            "failures": 0,
            "last_message_age_secs": self._age if self._connected else None,
        }


def _noop_ws():
    return FakeWs()


def test_soak_feed_freshness_follows_resolution_for_1h():
    """A 1h feed whose newest candle is the still-forming hour (30 min old)
    must report streaming. Before the fix a fixed 5-minute threshold labelled
    hourly data stale ~92% of the hour, which is exactly the soak telemetry."""
    mid_hour = _fresh_candles(age_secs=1800)  # last candle opened 30 min ago
    feed = SoakFeed("BTCUSDT", client=SingleSymbolStub(candles=mid_hour), enable_ws=False)
    feed.poll()
    snap = feed.snapshot()
    assert snap["data_status"] == "streaming"
    assert 1700 <= snap["last_candle_age_secs"] <= 1900
    assert snap["data_source"] == "rest"  # no ws; not stale


def test_soak_feed_freshness_turns_stale_only_after_a_full_missed_bar():
    """1h feed goes stale only once the newest bar is older than one resolution
    (i.e. the feed stopped updating for over an hour), not mid-hour."""
    too_old = _fresh_candles(age_secs=3700)  # > 1 bar old => feed is dead
    feed = SoakFeed("BTCUSDT", client=SingleSymbolStub(candles=too_old), enable_ws=False)
    feed.poll()
    snap = feed.snapshot()
    assert snap["data_status"] == "stale"
    assert snap["data_source"] == "rest"


def test_soak_feed_explicit_fresh_secs_override_is_honoured():
    mid_hour = _fresh_candles(age_secs=1800)
    feed = SoakFeed(
        "BTCUSDT",
        client=SingleSymbolStub(candles=mid_hour),
        enable_ws=False,
        fresh_secs=120,
    )
    feed.poll()
    assert feed.snapshot()["data_status"] == "stale"  # 1800s > explicit 120s


def test_soak_feed_streaming_plus_fresh_ws_reports_websocket_source():
    mid_hour = _fresh_candles(age_secs=1800)
    feed = SoakFeed(
        "BTCUSDT",
        client=SingleSymbolStub(candles=mid_hour),
        enable_ws=False,
        ws=_noop_ws(),
    )
    feed.poll()
    snap = feed.snapshot()
    assert snap["data_status"] == "streaming"
    assert snap["data_source"] == "websocket"  # ws connected + fresh, candle live
    assert snap["ws_status"]["connected"] is True


def test_runner_trades_on_still_forming_hour_candle(tmp_path):
    """Integration proof: with the resolution-aware freshness gate the runner
    opens a position on a new 1H candle mid-hour instead of skipping with
    data_status=stale (the pre-fix soak never traded after the first 5 min of
    each hour)."""
    mid_hour = _fresh_candles(age_secs=1800)
    runner = _runner(
        candles_by_symbol={"BTCUSDT": mid_hour, "ETHUSDT": _fresh_candles(base=3000.0)},
        out_path=str(tmp_path / "t.jsonl"),
    )
    assert runner.cycle()["action"] == "seeded"
    _advance(runner.feeds["BTCUSDT"])  # new 1H candle appears, 60s old
    result = runner.cycle()
    assert result["action"]["action"] == "open"
    assert runner.engine.calls == 1
    rows = {r["symbol"]: r for r in runner.telemetry.rows}
    assert rows["BTCUSDT"]["market_data_status"] == "streaming"
    assert "stale" not in str(rows["BTCUSDT"]["skip_reason"])
