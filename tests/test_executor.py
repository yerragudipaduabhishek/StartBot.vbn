import time

import pytest

from app.config.settings import get_settings
from app.trading.executor import Executor
from app.trading.paper_trader import PaperTrader


def _settings():
    # ensure keys are absent so can_trade_real is False
    s = get_settings()
    return s.model_copy(update={"live_mode": False, "delta_api_key": "", "delta_api_secret": ""})


def _signal(**overrides):
    sig = {
        "direction": "long",
        "stop_loss": 49000,
        "target": 52000,
        "entry": 50000,
        "accepted_size": 50,
        "risk_allowed": True,
        "confidence": 0.8,
        "score": 1.0,
        "candle_time": int(time.time()),
        "data_quality": {"status": "streaming", "price_valid": True, "source": "test", "last_candle_age_secs": 10},
        "product_id": 0,
    }
    sig.update(overrides)
    return sig


def test_executor_paper_when_not_live():
    ex = Executor(settings=_settings(), paper=PaperTrader(starting_equity=10000))
    res = ex.execute(_signal(), price=50000, confirm_real_orders=True)
    assert res.venue == "paper"
    assert ex.paper.position is not None


def test_executor_refuses_neutral():
    ex = Executor(settings=_settings())
    assert ex.execute({"direction": "none"}, 5, 50000) is None


def test_executor_requires_keys_for_real():
    s = _settings().model_copy(update={"live_mode": True})
    ex = Executor(settings=s, paper=PaperTrader(starting_equity=10000))
    assert not ex.can_trade_real  # no keys -> still paper
    res = ex.execute(_signal(), price=50000, confirm_real_orders=True)
    assert res.venue == "paper"


def test_executor_no_confirm_never_real():
    """Even with everything enabled, missing explicit confirm blocks real orders."""
    ex = Executor(settings=_settings(), paper=PaperTrader(starting_equity=10000))
    res = ex.execute(_signal(), price=50000, confirm_real_orders=False)
    assert res.venue == "paper"


def test_order_spec_validation():
    from pydantic import ValidationError

    from app.trading.orders import OrderSpec

    with pytest.raises(ValidationError):
        OrderSpec(product_id=1, side="hold", size=-1)