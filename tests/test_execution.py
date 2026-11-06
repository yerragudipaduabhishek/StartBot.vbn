"""Comprehensive tests for the Delta TESTNET execution-validation layer.

All network interaction is replaced with a FakeRest client so the suite runs
offline and fast; the strict host guard is unit-tested directly.
"""

import json
import time

import pytest

from app.config.settings import get_settings
from app.data.delta_client import DeltaClient
from app.execution.client import DuplicateOrderError, TestnetConnectivityError, TestnetExecutionClient, normalize_order_state
from app.execution.feed import TestnetOrderFeed, ws_auth_params
from app.execution.loop import TestnetValidationLoop
from app.execution.recovery import (
    UsedOrderIdStore,
    build_client_order_id,
    reconcile_exchange_state,
    recover_monitor,
)
from app.execution.report import (
    ExecutionJournal,
    ExecutionReport,
    execution_report_from_order,
    slippage_between,
)
from app.execution.safety import (
    ExecutionSafetyError,
    PROD_REST_HOSTS,
    TESTNET_REST_HOSTS,
    validate_execution_host,
)
from app.execution.sltp import SltpMonitor
from app.execution.validator import TestnetValidator
from app.trading.orders import OrderResult, OrderSpec


def _settings(**updates):
    return get_settings().model_copy(update={"testnet_mode": True, "live_mode": False, **updates})


def _vsettings(**updates):
    """Settings for validator tests: keys must be present for the mode gate,
    and polling is kept immediate so cancel-probe runs are fast."""
    return _settings(
        delta_api_key="k",
        delta_api_secret="s",
        delta_execution_poll_secs=0.0,
        delta_execution_max_polls=3,
        **updates,
    )


def _exec_signal(**overrides):
    sig = {
        "direction": "long",
        "entry": 50000,
        "stop_loss": 49000,
        "target": 52000,
        "accepted_size": 25,
        "risk_allowed": True,
        "confidence": 0.8,
        "score": 1.0,
        "candle_time": int(time.time()),
        "data_quality": {"status": "streaming", "price_valid": True, "source": "test", "last_candle_age_secs": 10},
        "product_id": 138,
    }
    sig.update(overrides)
    return sig


class FakeRest:
    """Stands in for DeltaClient. Every interaction is journaled so tests can
    assert the exact call sequence."""

    base_url = "https://testnet-api.delta.exchange"

    def __init__(self):
        self.calls: list[tuple[str, object]] = []
        self.product = {
            "id": 138,
            "symbol": "BTCUSDT",
            "contract_type": "perpetual_futures",
            "contract_unit_size": 1,
            "underlying_asset": "BTC",
        }
        self.orders: dict[int, dict] = {}
        self.next_order_id = 9000
        self.balances = [{"currency": "BTC", "balance": 1.5, "available_balance": 1.2}]
        self.positions = []
        self.ticker = {"symbol": "BTCUSDT", "close": "50000", "mark_price": "50005"}
        self.fail_next_place = False  # when True -> success False envelope
        self.reject_next_get = False

    def find_product(self, symbol, contract_type="perpetual_futures"):
        self.calls.append(("find_product", (symbol, contract_type)))
        return self.product

    def get_ticker(self, symbol):
        self.calls.append(("get_ticker", symbol))
        return dict(self.ticker)

    def get_balances(self):
        self.calls.append(("get_balances", None))
        return self.balances

    def get_positions(self, product_id=None):
        self.calls.append(("get_positions", product_id))
        return self.positions

    def get_orders(self, product_id, state="open"):
        self.calls.append(("get_orders", (product_id, state)))
        return [o for o in self.orders.values() if o.get("state") == state]

    def get_order(self, order_id):
        self.calls.append(("get_order", order_id))
        if self.reject_next_get:
            self.reject_next_get = False
            return {"success": False, "error": {"code": "auth_bad"}}
        return dict(self.orders.get(order_id, {"id": order_id, "state": "open"}))

    def get_order_trades(self, order_id):
        self.calls.append(("get_order_trades", order_id))
        return []

    def place_order(self, product_id, size, side, order_type="market", price=None, stop_price=None,
                    reduce_only=False, close_on_trigger=False, client_order_id=None, time_in_force="gtc"):
        self.calls.append(("place_order", {"product_id": product_id, "size": size, "side": side,
                                           "order_type": order_type, "client_order_id": client_order_id}))
        if self.fail_next_place:
            self.fail_next_place = False
            return {"success": False, "error": {"code": "invalid_size", "msg": "nope"}}
        oid = self.next_order_id
        self.next_order_id += 1
        filled_size = size if order_type == "market" else 0
        order = {
            "id": oid,
            "product_id": product_id,
            "size": size,
            "filled": filled_size,
            "side": side,
            "order_type": order_type,
            "state": "filled" if order_type == "market" else "open",
            "client_order_id": client_order_id,
            "average_filled_price": "50001.23" if order_type == "market" else None,
            "quote_fees": "0.0500123",
            "contract_fees": "0",
            "reduce_only": reduce_only,
            "close_on_trigger": close_on_trigger,
        }
        self.orders[oid] = order
        if filled_size:
            self.positions = [{"product_id": product_id, "size": filled_size if side == "buy" else -filled_size,
                               "entry_price": 50001.23, "mark_price": 50005.0}]
        return {"success": True, "result": order}

    def cancel_order(self, order_id):
        self.calls.append(("cancel_order", order_id))
        order = self.orders.get(order_id, {"id": order_id, "side": "buy", "size": 1, "state": "cancelled"})
        order["state"] = "cancelled"
        return {"success": True, "result": order}


def _client(tmp_path, rest=None, settings=None):
    settings = settings or _settings()
    return TestnetExecutionClient(
        settings=settings,
        rest=rest or FakeRest(),
        store=UsedOrderIdStore(path=str(tmp_path / "ids.json")),
        journal=ExecutionJournal(journal_path=str(tmp_path / "events.jsonl")),
    )


def _tmp_journal(tmp_path):
    return ExecutionJournal(journal_path=str(tmp_path / "ex.jsonl"))


# ------------------------------------------------------------------ settings


def test_settings_default_mode_is_safe():
    s = get_settings()
    assert s.live_mode is False
    assert s.testnet_mode is True
    assert s.real_order_confirmation is False
    assert "testnet" in s.delta_base_url


def test_settings_base_urls_follow_testnet_mode():
    s = _settings(testnet_mode=True, delta_region="global")
    assert s.delta_base_url == "https://testnet-api.delta.exchange"
    s2 = _settings(testnet_mode=True, delta_region="india")
    assert s2.delta_base_url == "https://cdn-ind.testnet.deltaex.org"
    s3 = _settings(testnet_mode=False, live_mode=True, delta_region="global")
    assert s3.delta_base_url == "https://api.delta.exchange"


def test_settings_market_data_never_breaks_order_guard():
    s = _settings(testnet_mode=True)
    assert "testnet" in s.delta_market_data_url or s.delta_data_url_override


# ------------------------------------------------------------------- safety


@pytest.mark.parametrize("host", list(TESTNET_REST_HOSTS))
def test_validate_host_allows_testnet(host):
    validate_execution_host(host, testnet_mode=True)


@pytest.mark.parametrize("host", list(PROD_REST_HOSTS))
def test_validate_host_rejects_production_when_testnet(host):
    with pytest.raises(ExecutionSafetyError, match="production host.*forbidden"):
        validate_execution_host(host, testnet_mode=True)


def test_validate_host_rejects_unknown_host():
    with pytest.raises(ExecutionSafetyError):
        validate_execution_host("random.example.com", testnet_mode=True)


def test_validate_host_refuses_when_testnet_off():
    with pytest.raises(ExecutionSafetyError):
        validate_execution_host("testnet-api.delta.exchange", testnet_mode=False)


def test_client_refuses_live_mode(tmp_path):
    with pytest.raises(ExecutionSafetyError, match="LIVE_MODE is on"):
        _client(tmp_path, settings=_settings(live_mode=True))


def test_client_refuses_when_testnet_off(tmp_path):
    with pytest.raises(ExecutionSafetyError, match="TESTNET_MODE is off"):
        _client(tmp_path, settings=_settings(testnet_mode=False))


def test_client_guard_blocks_any_production_based_rest(tmp_path):
    prod_rest = FakeRest()
    prod_rest.base_url = "https://api.delta.exchange"
    with pytest.raises(ExecutionSafetyError, match="production host"):
        _client(tmp_path, rest=prod_rest)


# ---------------------------------------------------------------- signature


def test_delta_signature_vector_matches_expected_format():
    import hashlib
    import hmac as hmac_mod


    secret = "testsecret"
    client = DeltaClient("https://testnet-api.delta.exchange", api_key="k", api_secret=secret)
    method, path, query, body = "GET", "/v2/orders", "?product_id=1&state=open", ""
    ts = "1542110948"
    got = client._sign(method, path, query, body, ts)
    expected = hmac_mod.new(secret.encode(), (method + ts + path + query + body).encode(), hashlib.sha256).hexdigest()
    assert got == expected
    assert got == hmac_mod.new(secret.encode(), ("GET1542110948/v2/orders?product_id=1&state=open").encode(), hashlib.sha256).hexdigest()


def test_auth_headers_use_single_timestamp(monkeypatch):
    from app.data import delta_client as dc

    monkeypatch.setattr(dc.time, "time", lambda: 1542110948.0)
    client = DeltaClient("https://testnet-api.delta.exchange", api_key="ak", api_secret="sk")
    headers = client._headers("GET", "/v2/orders", "?product_id=1", "")
    assert headers["timestamp"] == "1542110948"
    assert headers["api-key"] == "ak"
    # signature must embed the SAME timestamp that is sent as a header
    import hashlib
    import hmac as hmac_mod

    expected = hmac_mod.new(b"sk", b"GET1542110948/v2/orders?product_id=1", hashlib.sha256).hexdigest()
    assert headers["signature"] == expected


def test_state_normalization():
    assert normalize_order_state("open") == "open"
    assert normalize_order_state("filled") == "filled"
    assert normalize_order_state("closed") == "filled"
    assert normalize_order_state("cancelled") == "cancelled"
    assert normalize_order_state("canceled") == "cancelled"
    assert normalize_order_state("rejected") == "rejected"


# ------------------------------------------------------------ client basics


def test_balances_and_positions_delegate(tmp_path):
    rest = FakeRest()
    client = _client(tmp_path, rest=rest)
    assert client.fetch_balances() == rest.balances
    assert client.fetch_positions() == []
    assert ("get_balances", None) in rest.calls
    assert ("get_positions", None) in rest.calls


def test_discover_product(tmp_path):
    rest = FakeRest()
    client = _client(tmp_path, rest=rest)
    product = client.discover_product("BTCUSDT")
    assert product["id"] == 138
    assert product["contract_type"] == "perpetual_futures"
    assert ("find_product", ("BTCUSDT", "perpetual_futures")) in rest.calls


def test_mark_price(tmp_path):
    rest = FakeRest()
    client = _client(tmp_path, rest=rest)
    assert client.mark_price() == 50005.0  # mark_price preferred over close


def test_order_placement_maps_fill_fees_and_latency(tmp_path):
    rest = FakeRest()
    client = _client(tmp_path, rest=rest)
    spec = OrderSpec(product_id=138, side="buy", size=2, order_type="market", expected_price=50000.0)
    result = client.place_order(spec)
    assert result.order_id == 9000
    assert result.venue == "delta-testnet"
    assert result.order_state == "filled"
    assert result.fill_price == 50001.23
    assert result.expected_price == 50000.0
    assert result.fees == pytest.approx(0.0500123)
    assert result.latency_ms >= 0
    assert result.client_order_id.startswith("btcai-tn")


def test_slippage_sign_per_side(tmp_path):
    rest = FakeRest()
    client = _client(tmp_path, rest=rest)
    long_result = client.place_order(OrderSpec(product_id=138, side="buy", size=1, order_type="market", expected_price=50000.0))
    assert long_result.fill_price > long_result.expected_price  # filled higher = worse for long
    assert long_result.slippage_bps > 0
    short_result = client.place_order(OrderSpec(product_id=138, side="sell", size=1, order_type="market", expected_price=50000.0))
    assert short_result.slippage_bps < 0  # filled above expected = BETTER for a short entry


def test_place_order_journals_request_and_response(tmp_path):
    rest = FakeRest()
    client = _client(tmp_path, rest=rest)
    client.place_order(OrderSpec(product_id=138, side="buy", size=1, order_type="market"))
    lines = [json.loads(x) for x in open(str(tmp_path / "events.jsonl"), encoding="utf-8")]
    kinds = [l["kind"] for l in lines]
    assert "request" in kinds and "response" in kinds


def test_order_rejection_raises_and_journals(tmp_path):
    rest = FakeRest()
    client = _client(tmp_path, rest=rest)
    rest.fail_next_place = True
    with pytest.raises(TestnetConnectivityError, match="rejected"):
        client.place_order(OrderSpec(product_id=138, side="buy", size=1, order_type="market"))
    lines = [json.loads(x) for x in open(str(tmp_path / "events.jsonl"), encoding="utf-8")]
    assert any(l["kind"] == "rejection" for l in lines)


# -------------------------------------------------- duplicate-order protection


def test_duplicate_order_id_raises(tmp_path):
    rest = FakeRest()
    client = _client(tmp_path, rest=rest)
    spec = OrderSpec(product_id=138, side="buy", size=1, order_type="market", client_order_id="dup-1")
    result = client.place_order(spec)
    assert result.order_id == 9000
    with pytest.raises(DuplicateOrderError):
        client.place_order(spec.model_copy())


def test_duplicate_protection_survives_restart(tmp_path):
    rest = FakeRest()
    store1 = UsedOrderIdStore(path=str(tmp_path / "ids.json"))
    client1 = TestnetExecutionClient(settings=_settings(), rest=rest, store=store1,
                                     journal=ExecutionJournal(journal_path=""))
    client1.place_order(OrderSpec(product_id=138, side="buy", size=1, order_type="market", client_order_id="persist-1"))

    store2 = UsedOrderIdStore(path=str(tmp_path / "ids.json"))
    assert store2.contains("persist-1")
    client2 = TestnetExecutionClient(settings=_settings(), rest=FakeRest(), store=store2,
                                     journal=ExecutionJournal(journal_path=""))
    with pytest.raises(DuplicateOrderError):
        client2.place_order(OrderSpec(product_id=138, side="buy", size=1, order_type="market", client_order_id="persist-1"))


def test_build_client_order_id_is_signal_deterministic():
    assert build_client_order_id("btcai-tn", 138, 1700000000) == "btcai-tn-138-1700000000"
    assert build_client_order_id("btcai-tn", 138, 1700000000) == build_client_order_id("btcai-tn", 138, 1700000000)


# --------------------------------------------------------- order polls/cancel


def test_watch_order_polls_until_final(tmp_path):
    rest = FakeRest()
    client = _client(tmp_path, rest=rest)
    result = client.place_order(OrderSpec(product_id=138, side="buy", size=1, order_type="market"))
    snap = client.watch_order(result.order_id, max_polls=5, poll_secs=0)
    assert snap["state"] == "filled"


def test_watch_order_times_out(tmp_path):
    rest = FakeRest()
    client = _client(tmp_path, rest=rest)
    # A resting open order never reaches a final state in the poll budget.
    with pytest.raises(TestnetConnectivityError, match="did not reach a final state"):
        client.watch_order(999999, max_polls=2, poll_secs=0)


def test_cancel_order(tmp_path):
    rest = FakeRest()
    client = _client(tmp_path, rest=rest)
    # seed a resting limit order directly
    spec = OrderSpec(product_id=138, side="buy", size=1, order_type="limit_order", limit_price=1.0)
    created = client.place_order(spec)
    cancelled = client.cancel_order(created.order_id)
    assert cancelled.order_state == "cancelled"
    assert ("cancel_order", created.order_id) in rest.calls


# -------------------------------------------------------------- sltp monitor


def test_sltp_long_first_bound_hit_wins():
    monitor = SltpMonitor()
    monitor.arm("c1", "long", 1, entry_price=100, stop_price=98, target_price=110)
    assert monitor.check("c1", 105) is None  # inside the band -> no trigger
    assert monitor.check("c1", 97) == ("stop", 98)  # first bound hit closes as the stop
    assert monitor.positions["c1"].exit_reason == "stop"
    assert monitor.check("c1", 97) is None  # closed positions are inert


def test_sltp_bar_checks_stop_first():
    monitor = SltpMonitor()
    monitor.arm("c1", "long", 1, entry_price=100, stop_price=98, target_price=110)
    # Bar spikes BOTH ways in one candle: worst-case first = stop.
    trigger = monitor.check_bar("c1", high=120, low=97)
    assert trigger == ("stop", 98)


def test_sltp_short_targets_on_low():
    monitor = SltpMonitor()
    monitor.arm("c1", "short", 1, entry_price=100, stop_price=103, target_price=95)
    trigger = monitor.check_bar("c1", high=104, low=94)
    assert trigger == ("stop", 103)  # worst case first for a short is the high


def test_sltp_state_survives_restart(tmp_path):
    path = str(tmp_path / "sltp.json")
    m1 = SltpMonitor(state_path=path)
    m1.arm("c1", "long", 2, 100, 98, 110)
    m2 = SltpMonitor(state_path=path)
    pos = m2.reconcile("c1")
    assert pos is not None and pos.side == "long" and pos.stop_price == 98


def test_recover_monitor_rearms(tmp_path):
    path = str(tmp_path / "sltp.json")
    m1 = SltpMonitor(state_path=path)
    m1.arm("c1", "long", 1, 100, 98, 110)
    m2 = SltpMonitor()
    assert recover_monitor(m2, path) == 1
    assert m2.open_positions()[0].client_order_id == "c1"


# ------------------------------------------------------------------ recovery


def test_reconcile_cancels_orphan_orders_and_keeps_tracked(tmp_path):
    rest = FakeRest()
    # two resting orders we "own", neither tracked
    rest.place_order(product_id=138, size=1, side="buy", order_type="limit_order",
                     price=1.0, client_order_id="btcai-tn-orphan-1")
    rest.place_order(product_id=138, size=1, side="buy", order_type="limit_order",
                     price=1.0, client_order_id="btcai-tn-tracked-1")
    client = _client(tmp_path, rest=rest)
    tracked = {"btcai-tn-tracked-1"}
    summary = reconcile_exchange_state(client, 138, tracked_ids=tracked)
    assert summary["orphan_orders_found"] == 1
    assert summary["orphan_orders_cancelled"] == 1
    remaining = rest.get_orders(138, state="open")
    assert len(remaining) == 1
    assert remaining[0]["client_order_id"] == "btcai-tn-tracked-1"


# ------------------------------------------------------------- report/journal


def test_slippage_between_makes_worse_always_positive():
    signed, bps = slippage_between(100.0, 100.5, "long")
    assert signed > 0 and bps > 0
    signed2, bps2 = slippage_between(100.0, 99.5, "short")
    assert signed2 > 0 and bps2 > 0
    assert slippage_between(None, 100, "long") == (0.0, 0.0)


def test_execution_report_slide_math():
    res = OrderResult(product_id=138, side="buy", size=1, fill_price=50003.0,
                      fees=0.1, latency_ms=25.0, order_state="filled",
                      client_order_id="cid-1", venue="delta-testnet")
    report = execution_report_from_order(signal_time=123, side="long", size=1,
                                         expected_entry=50000.0, result=res,
                                         expected_sl=49500, expected_tp=51000)
    assert report.actual_entry == 50003.0
    assert report.slippage_bps > 0
    assert report.order_status == "filled"
    assert report.expected_sl == 49500 and report.expected_tp == 51000
    assert "slippage" in report.to_dict()


def test_journal_writes_jsonl_csv_and_reads_back(tmp_path):
    journal = _tmp_journal(tmp_path)
    report = ExecutionReport(signal_time=1, side="long", size=1, expected_entry=100,
                             order_status="filled", position_status="open",
                             client_order_id="j1", venue="delta-testnet")
    journal.write_report(report)
    rows = journal.read_reports()
    assert rows and rows[-1]["client_order_id"] == "j1"
    assert (tmp_path / "ex.jsonl").exists()
    assert (tmp_path / "ex.csv").exists()


def test_journal_db_upsert_same_client_order_id(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.database.database import Base
    from app.database.models import TestnetExecution

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    journal = ExecutionJournal(journal_path=str(tmp_path / "ex.jsonl"), session_factory=factory)
    r1 = ExecutionReport(signal_time=1, side="long", size=1, expected_entry=100, order_status="open",
                         position_status="open", client_order_id="cid-u", venue="delta-testnet")
    r2 = ExecutionReport(signal_time=1, side="long", size=1, expected_entry=100, actual_entry=101,
                         slippage_bps=10, order_status="filled", position_status="closed",
                         final_pnl=0.5, client_order_id="cid-u", venue="delta-testnet")
    journal.write_report(r1)
    journal.write_report(r2)
    with factory() as db:
        rows = db.query(TestnetExecution).all()
        assert len(rows) == 1
        assert rows[0].position_status == "closed"
        assert rows[0].final_pnl == 0.5


# ----------------------------------------------------------------- validator


def test_validator_mode_check_flags_unsafe(tmp_path):
    v = TestnetValidator(settings=_vsettings(), client=_client(tmp_path, settings=_vsettings()))
    mode = v.check_mode()
    assert mode["ok"] is True
    bad = TestnetValidator(settings=_vsettings(live_mode=True), client=_client(tmp_path, settings=_vsettings()))
    assert bad.check_mode()["ok"] is False
    assert "LIVE_MODE" in bad.check_mode()["problems"][0]


def test_validator_sweep_without_orders(tmp_path):
    rest = FakeRest()
    client = _client(tmp_path, rest=rest, settings=_vsettings())
    v = TestnetValidator(settings=_vsettings(), client=client, symbol="BTCUSDT")
    steps = v.validate(place_orders=False)
    assert steps["mode"]["ok"]
    assert steps["auth"]["ok"]
    assert steps["product"]["is_perpetual"] is True
    assert steps["order"] is None


def _approved(tmp_path, signal=None, *, limit=False):
    from app.config.trading import profile_from_settings
    from app.execution.guard import TradeGuard
    from app.risk.risk_manager import RiskManager
    from app.execution.validator import ApprovedTestnetOrder

    settings = _vsettings()
    client = _client(tmp_path, settings=settings)
    profile = profile_from_settings(settings, mode="testnet")
    risk = RiskManager(starting_equity=10_000.0, settings=profile.risk, profile=profile)
    guard = TradeGuard(profile, risk)
    payload = _exec_signal(
        accepted_size=50,
        stop_loss=49000,
        target=52000,
        candle_time=int(time.time()),
    )
    if limit:
        payload.update({"order_type": "limit", "limit_price": 49001})
    if signal:
        payload.update(signal)
    approved = ApprovedTestnetOrder.from_signal(
        payload,
        guard,
        product_id=138,
        requested_by="tester",
        reason="validator regression",
        client_order_prefix=settings.delta_client_id_prefix,
    )
    return client, approved


def test_validator_full_round_trip(tmp_path):
    client, approved = _approved(tmp_path)
    v = TestnetValidator(settings=_vsettings(), client=client, symbol="BTCUSDT")
    steps = v.validate(approved_order=approved, place_orders=True, cancel_test=True)
    assert steps["order"]["result"]["order_state"] == "filled"
    assert steps["order"]["result"]["venue"] == "delta-testnet"
    assert steps["order"]["authorized"] is True
    assert steps["cancel_test"]["skipped"] is True
    assert steps["cancel_test"]["reason"] == "order is filled, not open"
    report = steps["report"]
    assert report["actual_entry"] == 50001.23
    expected_slip, expected_bps = slippage_between(50000.0, 50001.23, "long")
    assert report["slippage_bps"] == pytest.approx(expected_bps)
    assert report["order_status"] == "filled"
    assert report["position_status"] in ("open", "flat")


def test_validator_cancel_only_touches_approved_order(tmp_path):
    client, approved = _approved(tmp_path, limit=True)
    v = TestnetValidator(settings=_vsettings(), client=client, symbol="BTCUSDT")
    steps = v.validate(approved_order=approved, place_orders=True, cancel_test=True)
    assert steps["order"]["result"]["order_state"] == "open"
    assert steps["cancel_test"]["cancel_ok"] is True
    cancel_calls = [call for call in client.rest.calls if call[0] == "cancel_order"]
    assert len(cancel_calls) == 1
    assert cancel_calls[0][1] == steps["order"]["result"]["order_id"]


def test_validator_rejects_manual_order_without_approved_chain(tmp_path):
    client = _client(tmp_path, settings=_vsettings())
    v = TestnetValidator(settings=_vsettings(), client=client, symbol="BTCUSDT")
    with pytest.raises(ExecutionSafetyError, match="approved Signal"):
        v.validate(place_orders=True)
    assert not [call for call in client.rest.calls if call[0] == "place_order"]


def test_validator_diagnostic_only_never_submits(tmp_path):
    client = _client(tmp_path, settings=_vsettings())
    v = TestnetValidator(settings=_vsettings(), client=client, symbol="BTCUSDT")
    steps = v.validate(diagnostic_only=True)
    assert steps["diagnostic_only"]["ok"] is True
    assert steps["order"] is None
    assert not [call for call in client.rest.calls if call[0] == "place_order"]
    with pytest.raises(ExecutionSafetyError, match="cannot place orders"):
        v.validate(place_orders=True, diagnostic_only=True, approved_order=object())


def test_validator_aborts_on_unsafe_mode(tmp_path):
    client = _client(tmp_path)  # safe client, unsafe validator settings
    v = TestnetValidator(settings=_settings(live_mode=True), client=client)
    with pytest.raises(ExecutionSafetyError):
        v.validate()


# --------------------------------------------------------------------- loop


def _loop(tmp_path, rest=None):
    from app.config.trading import profile_from_settings
    from app.execution.guard import TradeGuard
    from app.risk.risk_manager import RiskManager

    rest = rest or FakeRest()
    settings = _vsettings(delta_execution_state_file=str(tmp_path / "state.json"))
    client = _client(tmp_path, rest=rest, settings=settings)
    profile = profile_from_settings(settings, mode="testnet")
    risk = RiskManager(starting_equity=10_000.0, settings=profile.risk, profile=profile)
    guard = TradeGuard(profile, risk)
    return TestnetValidationLoop(
        settings=settings,
        client=client,
        monitor=SltpMonitor(state_path=str(tmp_path / "sltp.json")),
        profile=profile,
        risk_manager=risk,
        guard=guard,
    )


def _loop_signal(loop, signal_time, **overrides):
    raw = {
        "direction": "long",
        "risk_allowed": True,
        "entry": 50000.0,
        "stop_loss": 49600.0,
        "target": 50800.0,
        "confidence": 0.8,
        "score": 1.0,
        "candle_time": signal_time,
        "data_quality": {
            "status": "streaming",
            "price_valid": True,
            "source": "test",
            "last_candle_age_secs": 10,
        },
    }
    raw.update(overrides)
    decision = loop.risk_manager.evaluate_decision(
        raw,
        equity=loop.risk_manager.current_equity,
        contract_value=loop.profile.contract_value,
        timeframe=loop.profile.timeframe,
    )
    if decision.allowed:
        raw["accepted_size"] = decision.size.quantities
    else:
        # A second signal while a position is open is intentionally rejected
        # by the risk engine.  The loop should still report the existing
        # position before attempting any guarded execution.
        assert loop._position_open_for, decision.reason
        raw["accepted_size"] = 1.0
    return raw


def test_loop_executes_signal_and_arms_monitor(tmp_path):
    loop = _loop(tmp_path)
    signal_time = int(time.time())
    sig = _loop_signal(loop, signal_time)
    action = loop.on_signal(sig, signal_time=signal_time, price=50000)
    assert action["action"] == "executed"
    assert action["report"]["actual_entry"] == 50001.23
    cid = f"btcai-tn-138-{signal_time}"
    assert loop.monitor.positions[cid].stop_price == 49600


def test_loop_duplicate_signal_ignored(tmp_path):
    loop = _loop(tmp_path)
    signal_time = int(time.time())
    sig = _loop_signal(loop, signal_time, direction="short", stop_loss=50400, target=49000)
    loop.on_signal(sig, signal_time=signal_time, price=50000)
    action = loop.on_signal(sig, signal_time=signal_time, price=50000)
    assert action["action"] == "duplicate"


def test_loop_requires_guard_and_profile(tmp_path):
    settings = _vsettings(delta_execution_state_file=str(tmp_path / "state.json"))
    loop = TestnetValidationLoop(settings=settings, client=_client(tmp_path, settings=settings))
    action = loop.on_signal({"direction": "long", "risk_allowed": True}, signal_time=int(time.time()))
    assert action["action"] == "rejected"
    assert "TradeGuard" in action["reason"]


def test_loop_position_open_blocks_new_entry(tmp_path):
    loop = _loop(tmp_path)
    signal_time = int(time.time())
    sig = _loop_signal(loop, signal_time)
    loop.on_signal(sig, signal_time=signal_time, price=50000)
    sig2 = _loop_signal(loop, signal_time + 1, direction="short", stop_loss=50400, target=49000)
    action = loop.on_signal(sig2, signal_time=signal_time + 1, price=50000)
    assert action["action"] == "position_open"


def test_loop_candle_triggers_stop_and_closes(tmp_path):
    loop = _loop(tmp_path)
    signal_time = int(time.time())
    sig = _loop_signal(loop, signal_time)
    loop.on_signal(sig, signal_time=signal_time, price=50000)
    events = loop.on_candle({"time": signal_time + 1, "high": 50050, "low": 49500, "close": 49550})
    assert len(events) == 1
    ev = events[0]
    assert ev["reason"] == "stop"
    report = ev["report"]
    assert report["expected_sl"] == 49600
    assert report["actual_sl"] == 50001.23  # reduce-only market fill on testnet
    assert report["position_status"] == "closed"
    assert report["final_pnl"] < 0  # stopped out + fees -> loss
    assert loop.monitor.positions.get(f"btcai-tn-138-{signal_time}") is None  # disarmed


def test_loop_recover_reconciles(tmp_path):
    rest = FakeRest()
    # an orphaned resting order from a "previous process"
    rest.place_order(product_id=138, size=1, side="buy", order_type="limit_order",
                     price=1.0, client_order_id="btcai-tn-138-777")
    loop = _loop(tmp_path, rest=rest)
    result = loop.recover()
    assert result["orphan_orders_cancelled"] >= 1
    assert result["product_id"] == 138


# -------------------------------------------------------------------- feed


def test_ws_auth_params_present():
    params = ws_auth_params("ak", "sk", path="/")
    assert set(params) == {"api-key", "timestamp", "signature"}
    assert params["api-key"] == "ak"
    assert len(params["signature"]) == 64


def test_feed_connect_url_embeds_auth():
    feed = TestnetOrderFeed("wss://socket.testnet.example", api_key="ak", api_secret="sk",
                            channels=["v2/user/orders"])
    url = feed._connect_url()
    assert "api-key=ak" in url
    assert "signature=" in url
    assert "timestamp=" in url


# ---------------------------------------------------------------- executor


def test_executor_uses_testnet_client_when_keys_present(tmp_path):
    from app.trading.executor import Executor
    from app.trading.paper_trader import PaperTrader

    class FakeExecClient:
        def __init__(self):
            self.placed = []

        def place_order(self, order):
            self.placed.append(order)
            return OrderResult(product_id=order.product_id, side=order.side, size=order.size,
                               fill_price=50001.0, fees=0.01, expected_price=order.expected_price,
                               client_order_id="tn-x", order_state="filled", venue="delta-testnet")


    fake_rest = FakeRest()
    settings = _settings(delta_api_key="k", delta_api_secret="s")
    exec_client = FakeExecClient()
    ex = Executor(settings=settings, paper=PaperTrader(), order_client=fake_rest, execution_client=exec_client)
    res = ex.execute(_exec_signal(), price=50000)
    assert res.venue == "delta-testnet"
    assert exec_client.placed  # went through the strict testnet client


def test_executor_live_stays_blocked_without_full_gate(tmp_path):
    from app.trading.executor import Executor
    from app.trading.paper_trader import PaperTrader

    rest = FakeRest()
    settings = _settings(live_mode=True, testnet_mode=True, real_order_confirmation=True,
                         delta_api_key="k", delta_api_secret="s")
    ex = Executor(settings=settings, paper=PaperTrader(starting_equity=10000), order_client=rest)
    assert not ex.can_trade_live  # testnet still on -> no live
    res = ex.execute(_exec_signal(accepted_size=50), price=50000, confirm_real_orders=True)
    assert res.venue == "paper"


def test_executor_live_requires_confirmation_even_when_permitted(tmp_path):
    from app.trading.executor import Executor
    from app.trading.paper_trader import PaperTrader

    rest = FakeRest()
    settings = _settings(live_mode=True, testnet_mode=False, real_order_confirmation=False,
                         delta_api_key="k", delta_api_secret="s")
    ex = Executor(settings=settings, paper=PaperTrader(starting_equity=10000), order_client=rest)
    assert not ex.can_trade_live  # confirmation flag false
    res = ex.execute(_exec_signal(accepted_size=50), price=50000, confirm_real_orders=True)
    assert res.venue == "paper"
