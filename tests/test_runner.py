from types import SimpleNamespace

from sqlalchemy import func, select

from app.database.database import SessionLocal, init_db
from app.database.models import Signal
from app.risk.config import RiskSettings
from app.risk.risk_manager import RiskManager
from app.strategy.signal_engine import SignalEngine
from app.trading.paper_trader import PaperTrader
from app.trading.runner import TradingRunner


def _candles(n=140):
    base = 50000.0
    return [
        {"time": 1700000000 + i * 300, "open": base + i * 20, "high": base + i * 20 + 40, "low": base + i * 20 - 40, "close": base + i * 20, "volume": 10.0}
        for i in range(n)
    ]


def _runner():
    init_db()
    risk = RiskManager(starting_equity=10000, settings=RiskSettings(min_confidence=0.0))
    engine = SignalEngine(risk_manager=risk, contract_value=0.001)
    paper = PaperTrader(starting_equity=10000, session_factory=SessionLocal)
    market = SimpleNamespace(candles=_candles(), ticker={"close": 50000.0, "funding_rate": 0.0, "funding_rate_8h": 0.0})
    runner = TradingRunner(
        market=market,
        engine=engine,
        risk_manager=risk,
        paper_trader=paper,
        news_getter=lambda: [],
        seed_on_startup=False,
    )
    return runner, risk, paper


def test_runner_processes_new_candle_and_persists():
    runner, risk, paper = _runner()
    assert runner.last_signal is None
    runner._process_cycle()
    assert runner.last_signal is not None
    assert runner.last_signal["direction"] in ("long", "short", "none")

    with SessionLocal() as db:
        total = db.execute(select(func.count()).select_from(Signal)).scalar_one()
        assert total == 1


def test_runner_idempotent_on_same_candle():
    runner, risk, paper = _runner()
    runner._process_cycle()
    runner._process_cycle()  # same candle again -> no new signal
    with SessionLocal() as db:
        total = db.execute(select(func.count()).select_from(Signal)).scalar_one()
        assert total == 1


def test_runner_processes_next_candle():
    runner, risk, paper = _runner()
    runner._process_cycle()
    runner.market.candles = _candles(141)  # one more closed bar
    runner._process_cycle()
    with SessionLocal() as db:
        total = db.execute(select(func.count()).select_from(Signal)).scalar_one()
        assert total == 2


def test_runner_manages_stop_via_candle():
    runner, risk, paper = _runner()
    paper.open_position("long", 10, 50000, 49500, 51000)
    c = {"time": 0, "open": 49000, "high": 49200, "low": 48900, "close": 49000, "volume": 5.0}
    res = paper.on_candle(c)
    assert res is not None and res["reason"] == "stop"