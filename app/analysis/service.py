"""Analysis service: symbol -> indicators -> agents -> decision -> risk -> plan.

Read-only. Reuses the validated 1h WINNER wiring from the execution layer
(EMA 8/40/50, threshold 0.25, min_confidence 0.5, SL 1.0 ATR, TP 2.0 ATR,
weights trend 0.4 / structure 0.0). Fetches real Delta market data, never
fabricates a number, and never places an order.
"""

from __future__ import annotations

import logging
import time

import pandas as pd

from app.analysis import leverage as lev
from app.analysis.config import ANALYSIS_CANDLE_LIMIT, DEFAULT_RISK_PCT, DEFAULT_WATCHLIST, HOUR_SECONDS
from app.analysis.strategies import get_strategy
from app.config.settings import Settings, get_settings
from app.data.delta_client import DeltaClient
from app.indicators.technical import add_all
from app.risk.config import RiskSettings
from app.risk.position_sizer import size_position
from app.risk.risk_manager import RiskManager
from app.strategy.scoring import DEFAULT_WEIGHTS
from app.strategy.signal_engine import SignalEngine
from app.strategy.strategy_a import STRATEGY_A_CONTRACT_VALUE, STRATEGY_A_SYMBOL

logger = logging.getLogger(__name__)

KNOWN_CONTRACT_VALUE: dict[str, float] = {
    STRATEGY_A_SYMBOL: STRATEGY_A_CONTRACT_VALUE
}

CONTRACT_TYPE = "perpetual_futures"

# Allow at most 2 missed 1h candles before flagging the candle feed stale.
# Old data presented as "current" would be fabrication; the analysis must
# surface an honest unavailable state instead.
MAX_CANDLE_AGE_SECS = 3 * HOUR_SECONDS


def _num(value) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


class AnalysisService:
    def __init__(self, settings: Settings | None = None, client: DeltaClient | None = None) -> None:
        self.settings = settings or get_settings()
        self.client = client or DeltaClient(
            self.settings.delta_market_data_url,
            settings=self.settings,
            read_fallback_urls=self.settings.delta_market_data_fallback_urls,
        )

    # ------------------------------------------------------------- instruments

    def list_instruments(self) -> dict:
        symbols = list(DEFAULT_WATCHLIST)
        try:
            products = self.client.get_products()
        except Exception as e:  # noqa: BLE001 - surfaces connectivity cleanly
            logger.warning("instrument discovery failed: %s", e)
            return {
                "instruments": [{"symbol": s, "available": False} for s in symbols],
                "error": f"product discovery failed: {e}",
            }
        index = {p.get("symbol"): p for p in products if p.get("contract_type") == CONTRACT_TYPE}
        out = []
        for sym in symbols:
            p = index.get(sym)
            contract_value = KNOWN_CONTRACT_VALUE.get(sym)
            if contract_value is None and p:
                contract_value = _num(p.get("contract_value"))
            out.append(
                {
                    "symbol": sym,
                    "available": bool(p),
                    "product_id": p.get("id") if p else None,
                    "contract_value": contract_value,
                }
            )
        return {"instruments": out, "error": None}

    def contract_value_for(self, symbol: str) -> float | None:
        if symbol in KNOWN_CONTRACT_VALUE:
            return KNOWN_CONTRACT_VALUE[symbol]
        try:
            product = self.client.find_product(symbol, CONTRACT_TYPE)
        except Exception:  # noqa: BLE001
            return None
        return _num(product.get("contract_value"))

    # ------------------------------------------------------------------ market

    def fetch_candles(self, symbol: str, resolution: str = "1h", limit: int = ANALYSIS_CANDLE_LIMIT) -> list[dict]:
        end = int(time.time())
        start = end - limit * HOUR_SECONDS - HOUR_SECONDS
        candles = self.client.get_candles(symbol, resolution=resolution, start=start, end=end)
        return list(candles[-limit:])

    def fetch_ticker(self, symbol: str) -> dict:
        try:
            return self.client.get_ticker(symbol) or {}
        except Exception as e:  # noqa: BLE001
            logger.warning("ticker fetch failed for %s: %s", symbol, e)
            return {}

    # ----------------------------------------------------------------- analysis

    def analyze(
        self,
        symbol: str,
        strategy_id: str = "strategy_a",
        fund_inr: float = 5000.0,
        risk_pct: float = DEFAULT_RISK_PCT,
    ) -> dict:
        timeline: list[dict] = []

        def step(name: str, detail: str) -> None:
            timeline.append({"time": int(time.time()), "step": name, "detail": detail})

        try:
            strategy = get_strategy(strategy_id)
        except ValueError as e:
            return self._unavailable(symbol, None, f"unknown strategy: {e}")

        params = strategy["parameters"]
        step("STRATEGY", f"{strategy['name']} ({strategy['status']}) loaded")

        # ---- market data (real Delta candles) ----
        try:
            candles = self.fetch_candles(symbol)
        except Exception as e:  # noqa: BLE001
            return self._unavailable(symbol, strategy, f"market data unavailable: {e}")

        if not candles:
            return self._unavailable(symbol, strategy, "market data unavailable: no candles returned")
        if len(candles) < 30:
            return self._unavailable(symbol, strategy, f"insufficient candle history ({len(candles)} < 30)", candles_count=len(candles))

        ticker = self.fetch_ticker(symbol)
        last = candles[-1]
        last_time = _num(last.get("time"))
        if last_time is None or (time.time() - last_time) > MAX_CANDLE_AGE_SECS:
            return self._unavailable(
                symbol,
                strategy,
                f"market data unavailable: stale candle data (last candle {last_time})",
                candles_count=len(candles),
            )
        price = _num(last["close"])
        if price is None or price <= 0:
            return self._unavailable(
                symbol,
                strategy,
                "market data unavailable: invalid market price",
                candles_count=len(candles),
            )
        atr_pct = self._atr_pct(candles)
        step("MARKET DATA", f"{symbol} 1h: {len(candles)} candles, last {price:,}" if price else f"{symbol} 1h: {len(candles)} candles")

        # ---- indicators ----
        contract_value = self.contract_value_for(symbol)
        ema_spans = params["ema_spans"]
        df = add_all(pd.DataFrame(candles), ema_spans=ema_spans)
        step("INDICATORS", f"EMA{ema_spans[0]}/{ema_spans[1]}/{ema_spans[2]} + RSI/MACD/ATR/ADX/VWAP computed")

        # ---- agents -> decision (validated wiring) ----
        risk = RiskManager(
            starting_equity=float(fund_inr),
            settings=RiskSettings(
                min_confidence=params["min_confidence"],
                sl_atr_multiple=params["sl_atr_multiple"],
                target_atr_multiple=params["target_atr_multiple"],
                risk_per_trade_pct=risk_pct,
            ),
        )
        engine = SignalEngine(
            weights={**DEFAULT_WEIGHTS, **params["weights"]},
            risk_manager=risk,
            decision_threshold=params["threshold"],
            contract_value=contract_value or 0.001,
        )
        sig = engine.analyze_candle(df, ticker or {}, None)
        votes = {v["agent"]: v for v in sig.get("votes", [])}
        direction = sig.get("direction", "neutral")
        confidence = _num(sig.get("confidence"))
        for agent in ("trend", "structure", "sentiment"):
            step(f"{agent.upper()} AGENT", self._agent_line(votes.get(agent), agent))
        step("DECISION AGENT", f"{direction.upper()} (score {sig.get('score')}, conf {confidence or 0:.2f})")

        # ---- leverage (AI vs risk engine capped) ----
        stop_pct = None
        entry = _num(sig.get("entry"))
        stop = _num(sig.get("stop_loss"))
        if entry and stop:
            stop_pct = abs(entry - stop) / entry * 100.0

        ctx = lev.LeverageContext(
            trend_score=_num(votes.get("trend", {}).get("score")),
            adx=_num((votes.get("trend", {}).get("details") or {}).get("adx")),
            atr_pct=atr_pct,
            confidence=confidence,
            momentum=_num(self._momentum(df)),
            positioning=abs(_num(votes.get("derivatives", {}).get("score"))) if votes.get("derivatives", {}).get("score") is not None else None,
            liquidity=self._liquidity(ticker, df),
            stop_distance_pct=stop_pct,
            drawdown_pct=0.0,
        )
        leverage_block = lev.resolve_leverage(ctx, max_leverage_setting=risk.settings.max_leverage)
        step(
            "LEVERAGE ADVISOR",
            f"AI rec {leverage_block['ai_recommended']}x vs risk max {leverage_block['risk_max']}x",
        )
        step("RISK ENGINE", f"effective {leverage_block['effective']}x ({leverage_block['status']})")

        # ---- trade plan (sized against the effective leverage, risk-capped) ----
        plan = self._plan(
            sig,
            risk,
            float(fund_inr),
            risk_pct,
            contract_value,
            symbol,
            effective_leverage=leverage_block["effective"] or risk.settings.max_leverage,
        )
        step("TRADE PLAN", plan["risk_line"])

        # ---- execution stays locked ----
        step("EXECUTION", "LOCKED — NO ORDER PLACED")

        return {
            "symbol": symbol,
            "strategy": strategy,
            "fund_inr": float(fund_inr),
            "risk_pct": risk_pct,
            "max_risk_inr": round(float(fund_inr) * risk_pct / 100.0, 2),
            "market": {
                "symbol": symbol,
                "price": price,
                "atr_pct": atr_pct,
                "candles": candles,
                "candles_count": len(candles),
                "last_candle_time": _num(last.get("time")),
                "funding_8h": _num(ticker.get("funding_rate_8h") if ticker.get("funding_rate_8h") is not None else ticker.get("funding_rate")),
                "open_interest": _num(ticker.get("oi_value_usd") if ticker.get("oi_value_usd") is not None else ticker.get("oi_contracts")),
                "contract_value": contract_value,
            },
            "decision": {
                "direction": direction,
                "score": sig.get("score"),
                "confidence": confidence,
                "votes": votes,
                "reasoning": sig.get("explanation"),
                "reason": sig.get("reason"),
            },
            "plan": plan,
            "leverage": {**leverage_block, "market_risk": lev.market_risk_label(atr_pct), "trend": lev.trend_label(direction), "volatility": lev.volatility_label(atr_pct), "signal_confidence": confidence},
            "timeline": timeline,
        }

    # ----------------------------------------------------------------- helpers

    @staticmethod
    def _atr_pct(candles: list[dict]) -> float | None:
        if len(candles) < 2:
            return None
        df = add_all(pd.DataFrame(candles))
        last = df.iloc[-1]
        price = float(last["close"])
        if price <= 0:
            return None
        return round(float(last["atr"]) / price * 100.0, 3)

    @staticmethod
    def _momentum(df: pd.DataFrame) -> float | None:
        try:
            last = df.iloc[-1]
            macd_hist = float(last["macd_hist"])
            return max(-1.0, min(1.0, macd_hist / max(float(last["atr"]), 1e-9)))
        except (KeyError, IndexError, ValueError):
            return None

    @staticmethod
    def _liquidity(ticker: dict, df: pd.DataFrame) -> float | None:
        oi = _num(ticker.get("oi_value_usd") or ticker.get("oi_contracts"))
        if oi is not None:
            return min(1.0, max(0.0, oi / 50_000_000.0))
        try:
            avg_vol = float(df["volume"].iloc[-20:].mean())
            return min(1.0, avg_vol / 1_000_000.0)
        except (KeyError, ValueError, TypeError):
            return None

    @staticmethod
    def _agent_line(vote: dict | None, agent: str) -> str:
        if not vote:
            return f"{agent}: no vote available"
        score = vote.get("score")
        d = vote.get("direction") or "n/a"
        if score is None:
            return f"{agent}: {d} (score n/a)"
        return f"{agent}: {d} (score {float(score):+.2f})"

    def _plan(self, sig: dict, risk: RiskManager, fund: float, risk_pct: float, contract_value: float | None, symbol: str, effective_leverage: float | None = None) -> dict:
        entry = _num(sig.get("entry"))
        stop = _num(sig.get("stop_loss"))
        target = _num(sig.get("target"))
        direction = sig.get("direction")
        rr = None
        if entry and stop and entry != stop:
            rr = round(abs(float(target or 0) - entry) / abs(entry - stop), 2)

        plan: dict = {
            "entry": entry,
            "stop_loss": stop,
            "target": target,
            "rr": rr,
            "position_size": None,
            "notional": None,
            "risk_allowed": bool(sig.get("risk_allowed")),
            "reason": sig.get("reason"),
        }

        if direction not in ("long", "short"):
            plan["risk_line"] = "no trade signal — position sizing skipped"
            return plan

        if not contract_value:
            plan["risk_line"] = f"contract value unknown for {symbol} — position sizing skipped"
            plan["reason"] = "instrument contract value unavailable"
            return plan

        size = size_position(
            equity=fund,
            entry=entry,
            stop=stop,
            risk_pct=risk_pct,
            contract_value=contract_value,
            max_leverage=effective_leverage or risk.settings.max_leverage,
        )
        if size:
            plan["position_size"] = round(size.quantities, 6)
            plan["notional"] = round(size.notional_usd, 2)
            plan["margin"] = round(size.margin_usd, 2)
            plan["risk_line"] = f"max risk {fund * risk_pct / 100:.2f} -> {size.explanation}"
            plan["reason"] = size.explanation
        else:
            plan["risk_line"] = "position sizing failed"
        return plan

    def _unavailable(self, symbol: str, strategy: dict | None, reason: str, candles_count: int = 0) -> dict:
        return {
            "symbol": symbol,
            "strategy": strategy,
            "fund_inr": None,
            "risk_pct": DEFAULT_RISK_PCT,
            "max_risk_inr": None,
            "market": {
                "symbol": symbol,
                "price": None,
                "atr_pct": None,
                "candles": [],
                "candles_count": candles_count,
                "last_candle_time": None,
                "funding_8h": None,
                "open_interest": None,
                "contract_value": None,
            },
            "decision": {"direction": "none", "score": None, "confidence": None, "votes": {}, "reasoning": None, "reason": reason},
            "plan": {
                "entry": None,
                "stop_loss": None,
                "target": None,
                "rr": None,
                "position_size": None,
                "notional": None,
                "risk_allowed": False,
                "reason": reason,
                "risk_line": f"decision unavailable: {reason}",
            },
            "leverage": {
                "ai_recommended": None,
                "risk_max": None,
                "effective": None,
                "status": lev.STATUS_UNAVAILABLE,
                "reason": reason,
                "factors": {},
                "market_risk": lev.market_risk_label(None),
                "trend": "NEUTRAL",
                "volatility": lev.volatility_label(None),
                "signal_confidence": None,
            },
            "timeline": [
                {"time": int(time.time()), "step": "MARKET DATA", "detail": reason},
                {"time": int(time.time()) + 1, "step": "EXECUTION", "detail": "LOCKED — NO ORDER PLACED"},
            ],
        }