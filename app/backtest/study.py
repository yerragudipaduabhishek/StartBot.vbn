"""Parameter study: in-sample scan + out-of-sample validation.

Protocol
--------
1. Load candles (ascending), tag regimes on the full window (descriptive only,
   it never influences trade decisions).
2. Temporal split, newest data is reserved as the out-of-sample validation set:
      selection  = the `scan_bars` before the OOS window   (used to fit params)
      oos        = the last `oob_bars`                     (used to validate)
   The OOS slice starts `warmup` bars earlier so indicators have history.
3. Sample a fixed-seed set of parameter combinations from the grid, run each on
   the selection window and score with a robustness objective:
        obj = mean_trade_pnl_usd * sqrt(n_trades) - 0.5 * |max_drawdown_pct|
   (fewer than `min_trades` => combo discarded).
4. Re-run the top-K combos on the OOS window.  Good train + bad OOS = curve fit.
5. Baseline (current production defaults) is always reported alongside.
"""

import argparse
import itertools
import logging
import random
import sys
import time

import pandas as pd

from app.backtest.engine import run_backtest
from app.backtest.regimes import regime_summary, tag_regimes
from app.backtest.walkforward import run_walk_forward
from app.config.settings import get_settings
from app.data.dataset import DatasetMetadata, build_dataset, load_dataset
from app.database.database import init_db
from app.features.pipeline import FEATURE_PIPELINE_VERSION
from app.research.store import ResearchStore
from app.risk.config import RiskSettings
from app.risk.risk_manager import RiskManager
from app.strategy.signal_engine import SignalEngine
from app.strategy.strategy_a import (
    STRATEGY_A_CONTRACT_VALUE,
    STRATEGY_A_ID,
    STRATEGY_A_PARAMS,
    STRATEGY_A_TIMEFRAME,
    STRATEGY_A_VERSION,
)

logger = logging.getLogger(__name__)

CONTRACT_VALUE = STRATEGY_A_CONTRACT_VALUE
COMMISSION_PCT = 0.05
SLIPPAGE_BPS = 1.0
WARMUP = 120
MIN_TRADES = 8

# The study baseline is the same locked Strategy A production profile.
BASELINE = dict(STRATEGY_A_PARAMS)

# best combo from the 1h in-sample scan, validated on held-out data.
# Aliased to the single source of truth so study/dashboard/execution cannot drift.
WINNER = dict(STRATEGY_A_PARAMS)

GRID = {
    "ema_fast": (8, 12, 20),
    "ema_slow": (26, 40),
    "ema_mid": (50,),
    "threshold": (0.15, 0.25, 0.35),
    "min_confidence": (0.3, 0.4, 0.5),
    "sl_atr": (1.0, 1.5, 2.0),
    "target_atr": (1.5, 2.0, 3.0),
    "trend_w": (0.4, 0.5, 0.6),
    "structure_w": (0.0, 0.15, 0.25),
}


def load_candles(resolution: str = STRATEGY_A_TIMEFRAME, limit: int | None = None) -> pd.DataFrame:
    """Load a validated database dataset and attach its reproducibility metadata."""
    settings = get_settings()
    init_db()
    try:
        df, metadata = load_dataset(
            settings.trading_symbol,
            resolution,
            limit=limit,
            source="study-database",
            strict=False,
        )
    except ValueError as exc:
        raise SystemExit(
            f"dataset unavailable: {exc}. Backfill candles first, e.g. "
            f"python -m app.data.historical"
        ) from exc
    df.attrs["dataset_metadata"] = metadata.as_dict()
    return df


def _record_backtest(
    store: ResearchStore,
    df: pd.DataFrame,
    params: dict,
    res,
    *,
    resolution: str,
    label: str,
) -> str:
    settings = get_settings()
    metadata: DatasetMetadata
    df, metadata = build_dataset(
        df,
        symbol=settings.trading_symbol,
        timeframe=resolution,
        source=f"study:{label}",
        venue="local-db",
        transport="database",
        closed_only=False,
    )
    store.record_dataset(metadata)
    experiment = store.record_experiment(
        strategy_id=STRATEGY_A_ID,
        strategy_version=STRATEGY_A_VERSION,
        symbol=settings.trading_symbol,
        timeframe=resolution,
        dataset_hash=metadata.dataset_hash,
        dataset_start=metadata.first_candle_time,
        dataset_end=metadata.last_candle_time,
        params=params,
        metrics=res.metrics(),
        assumptions={
            "label": label,
            "commission_pct": COMMISSION_PCT,
            "dataset_rows": len(df),
        },
        profile={},
        fee_pct=COMMISSION_PCT,
        slippage_bps=SLIPPAGE_BPS,
        feature_pipeline_version=FEATURE_PIPELINE_VERSION,
    )
    return experiment.experiment_id


def valid_combos() -> list[dict]:
    combos = []
    for vals in itertools.product(*GRID.values()):
        p = dict(zip(GRID.keys(), vals))
        if p["ema_fast"] < p["ema_slow"] < p["ema_mid"]:
            combos.append(p)
    return combos


def build_engine(p: dict):
    settings = RiskSettings(
        min_confidence=p["min_confidence"],
        sl_atr_multiple=p["sl_atr"],
        target_atr_multiple=p["target_atr"],
    )
    rm = RiskManager(starting_equity=10_000.0, settings=settings)
    weights = {"trend": p["trend_w"], "structure": p["structure_w"], "sentiment": 0.15, "derivatives": 0.15}
    engine = SignalEngine(weights=weights, risk_manager=rm, decision_threshold=p["threshold"], contract_value=CONTRACT_VALUE)
    return engine, rm


def evaluate(df: pd.DataFrame, p: dict):
    engine, rm = build_engine(p)
    return run_backtest(
        df,
        engine,
        rm,
        warmup_bars=WARMUP,
        commission_pct=COMMISSION_PCT,
        slippage_bps=SLIPPAGE_BPS,
        ema_spans=(p["ema_fast"], p["ema_slow"], p["ema_mid"]),
    )


def robustness(res) -> float:
    trades = res.trades
    if len(trades) < MIN_TRADES:
        return float("-inf")
    mean_pnl = sum(t.pnl for t in trades) / len(trades)
    return mean_pnl * (len(trades) ** 0.5) - 0.5 * abs(res.max_drawdown_pct)


def run_train_scan(train: pd.DataFrame, n_samples: int, seed: int, budget_seconds: float) -> list[tuple[dict, object]]:
    combos = valid_combos()
    rng = random.Random(seed)
    pool = rng.sample(combos, min(n_samples, len(combos)))
    results: list[tuple[dict, object]] = []
    started = time.monotonic()
    for i, p in enumerate(pool):
        if time.monotonic() - started > budget_seconds:
            logger.warning("budget exhausted after %d/%d combos", i, len(pool))
            break
        results.append((p, evaluate(train, p)))
    return results


def fmt_row(p: dict, res, show_test: bool) -> list[str]:
    m = res.metrics()
    out = [f"{m['return_pct']:+.2f}", f"{m['win_rate']*100:.0f}%", f"{m['profit_factor']:.2f}", f"{m['max_drawdown_pct']:.1f}", f"{m['sharpe']:.1f}", str(m["trades"])]
    return out


def print_results(rows: list[tuple[dict, object, object]]) -> None:
    params_w = 44
    header = f"{'params':<{params_w}}{'sel R%':>8}{'sel W%':>7}{'sel PF':>8}{'sel MD':>8}{'sel SF':>8}{'sel tr':>7}{'OOS R%':>9}{'OOS W%':>8}{'OOS PF':>8}{'OOS MD':>8}{'OOS tr':>7}"
    print(header)
    print("-" * len(header))
    for p, sel, oos in rows:
        s = fmt_row(p, sel, False)
        o = fmt_row(p, oos, True)
        ps = " ".join(f"{k}={v}" for k, v in p.items())
        print(f"{ps:<{params_w}}{s[0]:>8}{s[1]:>7}{s[2]:>8}{s[3]:>8}{s[4]:>8}{s[5]:>7}{o[0]:>9}{o[1]:>8}{o[2]:>8}{o[3]:>8}{o[5]:>7}")


def diagnostic(df: pd.DataFrame, p: dict, forward_bars: int = 12) -> None:
    """Does the signal have any forecasting power? For every actionable bar,
    record the direction and the realized forward return (next `forward_bars`
    bars), then summarize long vs short vs magnitude."""

    import numpy as np

    from app.indicators.technical import add_all

    df = add_all(df, ema_spans=(p["ema_fast"], p["ema_slow"], p["ema_mid"]))
    engine, rm = build_engine(p)
    sigs = []
    closes = df["close"].to_numpy()
    for i in range(WARMUP, len(df) - forward_bars):
        sig = engine.analyze_candle(df, end_idx=i)
        if sig["direction"] in ("long", "short") and sig["risk_allowed"]:
            fwd = closes[i + forward_bars] / closes[i] - 1.0
            sigs.append((sig["direction"], fwd))
    if not sigs:
        print("diag: no actionable signals in window")
        return
    longs = [f for d, f in sigs if d == "long"]
    shorts = [f for d, f in sigs if d == "short"]
    import statistics

    def probe(name, arr):
        if not arr:
            print(f"  {name:<10} n=0")
            return
        m = statistics.mean(arr)
        print(f"  {name:<10} n={len(arr):>4}  fwd_mean={m:+.4%}  hit%={sum(1 for x in arr if x > 0)/len(arr):.0%}")
    print(f"diag: actionable signals on window (fwd over {forward_bars} bars)")
    probe("long", longs)
    probe("short", shorts)
    total = [f for _, f in sigs]
    probe("all", total)
    # correlation between signal direction (+1/-1) and forward return
    if len(sigs) >= 8:
        arr = np.array([(1 if d == "long" else -1, f) for d, f in sigs])
        if arr[:, 1].std() > 0:
            corr = np.corrcoef(arr[:, 0], arr[:, 1])[0, 1]
            print(f"  corr(direction, fwd): {corr:+.3f}   (positive = signals align with forward moves)")

    # per-agent predictive power (which individual vote has edge?)
    print("\nper-agent correlation with forward return (non-neutral votes):")
    agents = {}
    for i in range(WARMUP, len(df) - forward_bars):
        sig = engine.analyze_candle(df, end_idx=i)
        for v in sig.get("votes", []):
            if v["direction"] in ("long", "short"):
                sign = 1 if v["direction"] == "long" else -1
                agents.setdefault(v["agent"], []).append((sign, closes[i + forward_bars] / closes[i] - 1.0))
    for agent, pairs in agents.items():
        if len(pairs) < 20:
            print(f"  {agent:<12} n={len(pairs):>4}  (too few votes)")
            continue
        signs = np.array([s for s, _ in pairs])
        fwds = np.array([f for _, f in pairs])
        corr = np.corrcoef(signs, fwds)[0, 1] if fwds.std() > 0 else 0.0
        longs = np.mean(fwds[signs > 0]) if (signs > 0).any() else 0.0
        shorts = np.mean(fwds[signs < 0]) if (signs < 0).any() else 0.0
        print(f"  {agent:<12} n={len(pairs):>5}  corr={corr:+.3f}  long_fwd={longs:+.4%}  short_fwd={shorts:+.4%}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Backtest validation + parameter study")
    ap.add_argument("--resolution", default=STRATEGY_A_TIMEFRAME)
    ap.add_argument("--mode", choices=["baseline", "study", "diag", "walk"], default="study")
    ap.add_argument("--scan-bars", type=int, default=8000, help="in-sample bars used to fit parameters")
    ap.add_argument("--oob-bars", type=int, default=8000, help="out-of-sample bars held out")
    ap.add_argument("--fold-select", type=int, default=500, help="walk-forward: bars per selection fold")
    ap.add_argument("--fold-oob", type=int, default=250, help="walk-forward: bars per validation fold")
    ap.add_argument("--step", type=int, default=200, help="walk-forward: stride between folds")
    ap.add_argument("--samples", type=int, default=40)
    ap.add_argument("--budget", type=float, default=420.0, help="max scan seconds")
    ap.add_argument("--topk", type=int, default=8)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--keyset", choices=["baseline", "winner"], default="winner", help="walk-forward: fixed parameter set")
    ap.add_argument("--refit", action="store_true", help="walk-forward: re-fit parameters on every training fold")
    ap.add_argument("--artifact-dir", default="artifacts/walkforward", help="walk-forward artifact bundle root")
    ap.add_argument("--no-artifacts", action="store_true", help="do not write the walk-forward artifact bundle")
    ap.add_argument("--no-record", action="store_true", help="do not write dataset/experiment records to research storage")
    args = ap.parse_args()

    df = load_candles(args.resolution)
    print(f"data: {len(df)} bars  {df['time'].iloc[0]} -> {df['time'].iloc[-1]}")
    metadata = df.attrs.get("dataset_metadata") or {}
    dataset_hash = str(metadata.get("dataset_hash") or "")
    store = None if args.no_record else ResearchStore()
    if store is not None:
        if metadata:
            store.record_dataset(metadata)
        print(f"research storage: {store.database_url}")

    df = tag_regimes(df)

    if args.mode == "walk":
        keys = WINNER if args.keyset == "winner" else BASELINE
        if args.refit:
            space = dict(GRID)
            optimization_mode = "random"
            max_trials = max(1, args.samples)
            print(f"\nwalk-forward (re-fit per fold, max {max_trials} trials/fold, seed {args.seed}):")
        else:
            space = {name: [value] for name, value in keys.items()}
            optimization_mode = "grid"
            max_trials = 1
            print(f"\nwalk-forward (fixed {args.keyset}, no fitting per fold):")
        result = run_walk_forward(
            df,
            evaluate,
            space,
            train_bars=args.fold_select,
            test_bars=args.fold_oob,
            step=args.step,
            warmup=WARMUP,
            optimization_mode=optimization_mode,
            max_trials=max_trials,
            seed=args.seed,
            min_trades=MIN_TRADES,
            max_drawdown_pct=25.0,
            artifact_dir=None if args.no_artifacts else args.artifact_dir,
            dataset_hash=dataset_hash,
            strategy_id=STRATEGY_A_ID,
            command=" ".join(sys.argv),
        )
        print(f"{'fold':<6}{'select start':<16}{'oob end':<16}{'sel R%':>8}{'OOS R%':>9}{'OOS W%':>8}{'OOS PF':>8}{'OOS MD':>8}{'OOS tr':>7}")
        for fold in result.folds:
            s = fold.train_metrics
            o = fold.test_metrics
            if not o:
                print(f"{fold.span.index:<6}{'-':<16}{'-':<16}  no accepted parameters: {fold.reason}")
                continue
            sel_start = df["time"].iloc[fold.span.train_start]
            oob_end = df["time"].iloc[fold.span.test_end - 1]
            print(
                f"{fold.span.index:<6}{str(sel_start):<16}{str(oob_end):<16}{s.get('return_pct', 0.0):>8.2f}"
                f"{o.get('return_pct', 0.0):>9.2f}{o.get('win_rate', 0.0) * 100:>7.0f}%"
                f"{o.get('profit_factor', 0.0):>8.2f}{o.get('max_drawdown_pct', 0.0):>8.2f}"
                f"{int(o.get('trades', 0)):>7}"
            )
        aggregate = result.aggregate()
        print(
            f"\naggregate: {aggregate['folds']} folds ({aggregate['accepted_folds']} accepted)  "
            f"mean RR {aggregate['mean_test_return_pct']:+.2f}%  mean PF {aggregate['mean_profit_factor']:.2f}  "
            f"positive folds {aggregate['positive_folds']}/{aggregate['accepted_folds']}  "
            f"mean MD {aggregate['mean_max_drawdown_pct']:.2f}%  worst MD {aggregate['worst_max_drawdown_pct']:.2f}%  "
            f"trades {aggregate['total_test_trades']}"
        )
        print(f"reproducibility hash: {result.reproducibility_hash}")
        if result.artifact_path:
            print(f"artifacts: {result.artifact_path}")
        if store is not None:
            metrics = dict(aggregate)
            metrics["reproducibility_hash"] = result.reproducibility_hash
            experiment = store.record_experiment(
                strategy_id=STRATEGY_A_ID,
                strategy_version=STRATEGY_A_VERSION,
                symbol=get_settings().trading_symbol,
                timeframe=args.resolution,
                dataset_hash=dataset_hash,
                dataset_start=metadata.get("first_candle_time"),
                dataset_end=metadata.get("last_candle_time"),
                params={"space": space, "keyset": args.keyset, "refit": bool(args.refit)},
                metrics=metrics,
                assumptions={"walk_forward": True, "folds": result.config},
                feature_pipeline_version=FEATURE_PIPELINE_VERSION,
            ) if dataset_hash else None
            if experiment is not None:
                store.record_walk_forward(result, experiment_id=experiment.experiment_id, strategy_id=STRATEGY_A_ID)
                print(f"recorded experiment: {experiment.experiment_id}")
        print("hint: use --refit for per-fold fitting; fixed params isolate regime stability.")
        return

    if len(df) < args.scan_bars + args.oob_bars + WARMUP:
        raise SystemExit("not enough data for the requested split")

    cut = len(df) - args.oob_bars
    selection = df.iloc[cut - args.scan_bars : cut].reset_index(drop=True)
    oos = df.iloc[max(0, cut - WARMUP):].reset_index(drop=True)
    print(f"selection: {len(selection)} bars  {selection['time'].iloc[0]} -> {selection['time'].iloc[-1]}")
    print(f"oob:       {len(oos)} bars  {oos['time'].iloc[0]} -> {oos['time'].iloc[-1]}")

    baseline_sel = evaluate(selection, BASELINE)
    baseline_oos = evaluate(oos, BASELINE)
    print("\nbaseline (defaults):")
    print("  selection:", baseline_sel.summary())
    print("  oob:      ", baseline_oos.summary())
    print("  oob regimes:", regime_summary(baseline_oos.trades))
    if store is not None:
        _record_backtest(store, selection, BASELINE, baseline_sel, resolution=args.resolution, label="baseline-selection")
        _record_backtest(store, oos, BASELINE, baseline_oos, resolution=args.resolution, label="baseline-oob")

    if args.mode == "diag":
        print("\ndiagnostic on selection window:")
        diagnostic(selection, BASELINE)
        return

    if args.mode != "study":
        return

    print(f"\nscanning up to {args.samples} combos (budget {args.budget:.0f}s) ...", flush=True)
    results = run_train_scan(selection, args.samples, args.seed, args.budget)
    scored = [(p, r, robustness(r)) for p, r in results]
    scored.sort(key=lambda x: x[2], reverse=True)
    kept = [x for x in scored if x[2] != float("-inf")]
    print(f"combos run: {len(results)}, viable (>= {MIN_TRADES} trades): {len(kept)}")
    if not kept:
        print("no viable combos found - strategy signals too rarely; try a bigger window")
        return

    top = [(p, r) for p, r, _ in kept[: args.topk]]
    rows = []
    for p, sel_res in top:
        oos_res = evaluate(oos, p)
        rows.append((p, sel_res, oos_res))
        if store is not None:
            _record_backtest(store, selection, p, sel_res, resolution=args.resolution, label="study-selection")
            _record_backtest(store, oos, p, oos_res, resolution=args.resolution, label="study-oob")
    print("\nresults (PF = profit factor, MD = max drawdown %, tr = trades):")
    print_results(rows)
    print("\ninterpretation: a combo is trustworthy only if OOS stays near selection "
          "(positive return, win rate and PF not collapsing). "
          "Selection-only winners with negative OOS are overfit.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    main()
