"""CLI for the testnet execution-validation layer.

Usage
-----
  python -m app.execution sweep                       # mode+auth+product+positions
  python -m app.execution sweep --place-orders        # + a real testnet round-trip
  python -m app.execution loop                        # manual wiring of 1h signals -> testnet
  python -m app.execution report --limit 25           # print the execution journal

Safety: every command double-checks the mode flags and the strict testnet
client can never reach a production order endpoint. No command enables live
trading, ever.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
from pathlib import Path

from app.config.settings import get_settings
from app.data.market_data import MarketData
from app.database.database import init_db
from app.execution.client import TestnetExecutionClient
from app.execution.loop import TestnetValidationLoop
from app.execution.report import ExecutionJournal
from app.execution.safety import ExecutionSafetyError
from app.execution.validator import ApprovedTestnetOrder, TestnetValidator

logger = logging.getLogger(__name__)


def _approved_order(settings, validator: TestnetValidator, args: argparse.Namespace) -> ApprovedTestnetOrder:
    if not args.signal_file:
        raise ExecutionSafetyError("--place-orders requires --signal-file with a validated strategy signal")
    if not args.reason:
        raise ExecutionSafetyError("--place-orders requires --reason for the audit trail")
    try:
        payload = json.loads(Path(args.signal_file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExecutionSafetyError(f"cannot read approved signal file: {exc}") from exc
    if not isinstance(payload, dict):
        raise ExecutionSafetyError("approved signal file must contain a JSON object")

    from app.config.trading import profile_from_settings
    from app.execution.guard import TradeGuard
    from app.risk.risk_manager import RiskManager

    profile = profile_from_settings(settings, mode="testnet")
    risk = RiskManager(starting_equity=10_000.0, settings=profile.risk, profile=profile)
    guard = TradeGuard(profile, risk)
    product = validator.client.discover_product(settings.trading_symbol)
    return ApprovedTestnetOrder.from_signal(
        payload,
        guard,
        product_id=int(product["id"]),
        requested_by=args.requested_by or getpass.getuser(),
        reason=args.reason,
        client_order_prefix=settings.delta_client_id_prefix,
    )


def _cmd_sweep(args: argparse.Namespace) -> int:
    settings = get_settings()
    validator = TestnetValidator(settings=settings)
    mode = validator.check_mode()
    if not mode["ok"]:
        print("mode check FAILED:")
        for p in mode["problems"]:
            print("  -", p)
        raise SystemExit(2)
    print(f"mode ok: live={settings.live_mode} testnet={settings.testnet_mode} "
          f"confirm={settings.real_order_confirmation} host={settings.delta_base_url}")

    approved_order = None
    if args.place_orders:
        approved_order = _approved_order(settings, validator, args)
    result = validator.validate(
        approved_order=approved_order,
        place_orders=args.place_orders,
        cancel_test=args.cancel_test,
        diagnostic_only=args.diagnostic_only,
    )
    print("auth:", result["auth"])
    print("product:", result["product"])
    print("positions_before:", result["positions_before"])
    if result.get("order"):
        print("order:", result["order"]["result"])
        print("report:", result["report"])
    if result.get("cancel_test"):
        print("cancel_test:", result["cancel_test"])
    if result.get("positions_after"):
        print("positions_after:", result["positions_after"])
    print("validated report summary:", result.get("report", {}).get("summary", "no order placed"))
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    settings = get_settings()
    journal = ExecutionJournal(journal_path=settings.delta_execution_journal)
    rows = journal.read_reports(limit=args.limit)
    if not rows:
        print("no execution reports recorded yet")
        return 0
    for row in reversed(rows):
        print(
            f"[{row.get('signal_time')}] {str(row.get('side')):<5} exp={row.get('expected_entry'):,} "
            f"act={row.get('actual_entry'):,.2f} slip={row.get('slippage_bps'):+.1f}bps "
            f"fees={row.get('fees'):.6f} lat={row.get('latency_ms'):.0f}ms "
            f"{row.get('order_status')}/{row.get('position_status')} pnl={row.get('final_pnl'):+.4f}"
        )
    return 0


async def _cmd_loop(args: argparse.Namespace) -> int:
    settings = get_settings()
    client = TestnetExecutionClient(settings=settings)

    from app.config.trading import profile_from_settings
    from app.database.database import SessionLocal
    from app.execution.guard import TradeGuard
    from app.risk.kill_switch import KillSwitch
    from app.risk.risk_manager import RiskManager
    from app.strategy.signal_engine import SignalEngine
    from app.strategy.timeframes import timeframe_seconds
    from app.strategy.validation import data_quality_from_snapshot

    profile = profile_from_settings(settings, mode="testnet")
    kill_switch = KillSwitch(SessionLocal, account_key="testnet")
    risk = RiskManager(
        starting_equity=10_000,
        settings=profile.risk,
        session_factory=SessionLocal,
        account_key="testnet",
        kill_switch=kill_switch,
    )
    guard = TradeGuard(profile, risk, kill_switch=kill_switch, session_factory=SessionLocal)
    engine = SignalEngine(
        weights=profile.weights,
        risk_manager=risk,
        decision_threshold=profile.decision_threshold,
        contract_value=profile.contract_value,
        profile=profile,
    )
    loop = TestnetValidationLoop(
        settings=settings,
        client=client,
        profile=profile,
        risk_manager=risk,
        guard=guard,
    )
    loop.recover()  # reconcile + re-arm monitor; anything of note is already journaled

    market = MarketData(
        settings=settings,
        resolution=profile.timeframe,
        candle_poll_secs=args.poll,
        candle_buffer=200,
    )
    product = market.client.find_product(market.symbol)
    print(f"product: {market.symbol} id={product.get('id')} resolution={profile.timeframe}")

    signal_time = 0
    n_cycles = max(1, args.cycles)
    for _ in range(n_cycles):
        end = int(__import__("time").time())
        window = 200 * timeframe_seconds(profile.timeframe)
        candles = market.client.get_candles(market.symbol, resolution=profile.timeframe, start=end - window, end=end)
        if not candles:
            await asyncio.sleep(args.poll)
            continue
        closed = candles[:-1]
        mark = float(candles[-1]["close"])

        # SL/TP on closed candles first (worst case first).
        for c in closed:
            for ev in loop.on_candle(c):
                print("closed:", ev["reason"], ev["report"]["final_pnl"])

        last_time = int(closed[-1]["time"])
        if last_time != signal_time:
            signal_time = last_time
            df = __import__("pandas").DataFrame(closed)
            sig = engine.analyze_candle(df)
            age = max(0.0, float(end - last_time))
            sig["time"] = last_time
            sig["symbol"] = profile.symbol
            sig["timeframe"] = profile.timeframe
            sig["strategy_id"] = profile.strategy_id
            sig["strategy_version"] = profile.strategy_version
            sig["data_quality"] = data_quality_from_snapshot(
                {
                    "data_status": "streaming" if age <= 2 * timeframe_seconds(profile.timeframe) and mark > 0 else "stale",
                    "price_valid": mark > 0,
                    "data_source": "rest",
                    "last_candle_age_secs": age,
                },
                profile=profile,
            ).model_dump()
            print(f"signal @ {last_time}: {sig['direction']} (score {sig.get('score'):+.2f})")
            action = loop.on_signal(sig, last_time, price=mark)
            print("execution:", action.get("action"), action.get("report", {}).get("summary", ""))
        await asyncio.sleep(args.poll)
    return 0


def main() -> None:
    init_db()  # create tables (incl. testnet_executions) through the normal app path
    parser = argparse.ArgumentParser(prog="app.execution", description="Delta testnet execution validation")
    sub = parser.add_subparsers(dest="command", required=True)

    sweep = sub.add_parser("sweep", help="run the validation checklist")
    sweep.add_argument("--place-orders", action="store_true", help="place a testnet order from an approved signal")
    sweep.add_argument("--signal-file", default="", help="JSON strategy signal approved by RiskManager/TradeGuard")
    sweep.add_argument("--requested-by", default="", help="audit identity for the order (defaults to OS user)")
    sweep.add_argument("--reason", default="", help="audit reason for the order (required with --place-orders)")
    sweep.add_argument("--diagnostic-only", action="store_true", help="read-only checks; never place or cancel orders")
    sweep.add_argument("--no-cancel-test", dest="cancel_test", action="store_false", default=True,
                       help="do not cancel an approved resting order")
    sweep.set_defaults(fn=_cmd_sweep)

    loop = sub.add_parser("loop", help="drive 1h signals onto testnet manually")
    loop.add_argument("--poll", type=float, default=15.0, help="poll seconds")
    loop.add_argument("--cycles", type=int, default=1, help="number of poll cycles")
    loop.set_defaults(fn=_cmd_loop)

    report = sub.add_parser("report", help="print the execution journal")
    report.add_argument("--limit", type=int, default=25)
    report.set_defaults(fn=_cmd_report)

    args = parser.parse_args()
    logging.basicConfig(level=get_settings().log_level, format="%(levelname)s %(message)s")
    try:
        if args.command == "loop":
            raise SystemExit(asyncio.run(_cmd_loop(args)))
        raise SystemExit(args.fn(args))
    except ExecutionSafetyError as e:
        print("safety error:", e)
        raise SystemExit(2) from e


if __name__ == "__main__":
    main()