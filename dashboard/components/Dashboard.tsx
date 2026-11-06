"use client";

import { useEffect, useState, useMemo } from "react";
import {
  fetchDashboard,
  fetchStrategies,
  fetchInstruments,
  fetchAnalysis,
  fetchHealth,
  fetchMonitor,
  type DashboardData,
} from "@/lib/api";
import type {
  AnalysisResult,
  Candle,
  HealthInfo,
  InstrumentInfo,
  MonitorInfo,
  StrategyInfo,
} from "@/lib/types";
import Card from "./Card";
import SafetyBanner from "./SafetyBanner";
import CandleChart from "./CandleChart";
import SignalPanel from "./SignalPanel";
import PnlPanel from "./PnlPanel";
import ExecutionPanel from "./ExecutionPanel";
import SignalsTable from "./SignalsTable";
import TradesTable from "./TradesTable";
import Watchlist from "./Watchlist";
import StrategySelector from "./StrategySelector";
import FundSelector from "./FundSelector";
import DecisionPanel from "./DecisionPanel";
import LeveragePanel from "./LeveragePanel";
import TradePlanPanel from "./TradePlanPanel";
import TimelinePanel from "./TimelinePanel";
import MarketHealthPanel from "./MarketHealthPanel";
import PaperPositionPanel from "./PaperPositionPanel";
import BotControlCenter from "./BotControlCenter";
import { fmtINR, fmtPrice } from "@/lib/format";

const POLL_MS = 5000;
const DEFAULT_SYMBOL = "BTCUSDT";
const DEFAULT_FUND = 5000;
const DEFAULT_RISK_PCT = 0.5;
const DEFAULT_STRATEGY = "strategy_a";

const EMPTY: DashboardData = {
  ticker: { symbol: "BTCUSDT", product_id: null, price: null, mark_price: null, volume: null, open_interest: null, oi_value_usd: null, funding_rate: null, funding_8h: null, candles: [], candles_count: 0, data_status: "unavailable", last_update_unix: null, last_error: null, updated_at: null },
  decision: { direction: "n/a", score: null, confidence: null, entry: null, stop_loss: null, target: null, accepted_size: null, risk_allowed: null, votes: [] },
  paper: {
    mode: "paper",
    balance: 5000,
    starting_equity: 5000,
    realized_pnl: 0,
    unrealized_pnl: 0,
    total_equity: 5000,
    open_position: null,
    fund_separate: true,
  },
  risk: { equity: 5000, realized_pnl: 0, day_pnl: 0, peak_equity: 5000, open_positions: 0, daily_loss_limit: 0.5, trading_halted: false },
  signals: [],
  trades: [],
  safety: null as unknown as DashboardData["safety"],
  executions: [],
};

const DATA_STATUS_LABELS: Record<string, string> = {
  streaming: "STREAMING",
  stale: "STALE",
  unavailable: "UNAVAILABLE",
};

const DATA_STATUS_CLS: Record<string, string> = {
  streaming: "badge-green",
  stale: "badge-amber",
  unavailable: "badge-red",
};

function lastUpdateText(sec: number | null | undefined): string {
  if (typeof sec !== "number" || !Number.isFinite(sec) || sec <= 0) {
    return "No market data received";
  }
  const d = new Date(sec * 1000);
  if (Number.isNaN(d.getTime())) return "No market data received";
  return `Last update: ${d.toLocaleString()}`;
}

export default function Dashboard() {
  const [data, setData] = useState<DashboardData>(EMPTY);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState("");

  const [symbol, setSymbol] = useState(DEFAULT_SYMBOL);
  const [strategyId, setStrategyId] = useState(DEFAULT_STRATEGY);
  const [fund, setFund] = useState(DEFAULT_FUND);
  const [riskPct, setRiskPct] = useState(DEFAULT_RISK_PCT);

  const [strategies, setStrategies] = useState<StrategyInfo[]>([]);
  const [instruments, setInstruments] = useState<InstrumentInfo[]>([]);
  const [analysis, setAnalysis] = useState<AnalysisResult | null>(null);
  const [analysisLoaded, setAnalysisLoaded] = useState(false);
  const [analysisError, setAnalysisError] = useState("");
  const [health, setHealth] = useState<HealthInfo | null>(null);
  const [monitor, setMonitor] = useState<MonitorInfo | null>(null);

  useEffect(() => {
    let alive = true;
    async function tick() {
      try {
        const [d, h, m] = await Promise.all([
          fetchDashboard(),
          fetchHealth(),
          fetchMonitor(),
        ]);
        if (!alive) return;
        setData(d);
        setLoaded(true);
        setHealth(h);
        setMonitor(m);
        setError("");
      } catch (e) {
        if (!alive) return;
        setError(String(e));
      }
    }
    tick();
    const id = setInterval(tick, POLL_MS);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  useEffect(() => {
    let alive = true;
    Promise.all([fetchStrategies(), fetchInstruments()])
      .then(([s, i]) => {
        if (!alive) return;
        setStrategies(s.strategies);
        setInstruments(i.instruments);
        if (s.default && s.strategies.some((x) => x.id === s.default)) {
          setStrategyId(s.default);
        }
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    let alive = true;
    setAnalysisLoaded(false);
    setAnalysisError("");
    fetchAnalysis(symbol, strategyId, fund, riskPct)
      .then((a) => {
        if (!alive) return;
        setAnalysis(a);
        setAnalysisLoaded(true);
      })
      .catch((e) => {
        if (!alive) return;
        setAnalysis(null);
        setAnalysisError(String(e));
      });
    return () => {
      alive = false;
    };
  }, [symbol, strategyId, fund, riskPct]);

  const candles = analysis?.market.candles ?? [];

  const chart = useMemo(() => {
    if (candles.length === 0) return null;
    return <CandleChart candles={candles} symbol={symbol} />;
  }, [candles, symbol]);

  const decision = analysis?.decision;
  const direction = decision?.direction?.toUpperCase() ?? "WAITING";
  const decisionClass = direction === "LONG" ? "long" : direction === "SHORT" ? "short" : "neutral";
  const riskBlocked = data.risk.trading_halted || decision?.reason?.toLowerCase().includes("reject");
  const marketStatus = health?.data_status === "streaming" ? "LIVE DATA" : (health?.data_status ?? "WAITING").toUpperCase();

  return (
    <main>
      <SafetyBanner safety={data.safety} loaded={loaded} />

      <div className={`${error ? "conn-error" : "conn-ok"} conn-strip`}>
        {error ? `backend unreachable: ${error}` : loaded ? "connected to backend" : "connecting…"}
      </div>

      <BotControlCenter data={data} analysis={analysis} health={health} symbol={symbol} loaded={loaded} />

      <details className="advanced-dashboard">
        <summary>Advanced analysis, charts, and raw trade tables</summary>

      {riskBlocked && (
        <div className="risk-banner" role="status">
          <span><strong>Trading is locked.</strong> The risk engine will not place a paper or exchange order.</span>
          <span className="badge badge-amber">SAFETY FIRST</span>
        </div>
      )}

      <section className="quick-grid" aria-label="Bot overview">
        <div className="quick-card">
          <span className="quick-label">Bot status</span>
          <span className="quick-value long">{data.safety?.live_mode ? "LIVE" : "PAPER"}</span>
          <span className="quick-help">No real funds are being used</span>
        </div>
        <div className="quick-card">
          <span className="quick-label">Market</span>
          <span className="quick-value">{data.ticker.price != null ? fmtPrice(data.ticker.price) : "—"}</span>
          <span className="quick-help">{symbol} · {marketStatus}</span>
        </div>
        <div className="quick-card">
          <span className="quick-label">Current decision</span>
          <span className={`quick-value ${decisionClass}`}>{direction}</span>
          <span className="quick-help">
            {decision?.confidence != null ? `${Math.round(decision.confidence * 100)}% confidence` : "Waiting for analysis"}
          </span>
        </div>
        <div className="quick-card">
          <span className="quick-label">Paper equity</span>
          <span className="quick-value">{fmtINR(data.paper.total_equity ?? data.paper.balance)}</span>
          <span className="quick-help">{data.paper.open_position ? "Position open" : "No open position"}</span>
        </div>
      </section>

      <div className="grid">
        <div className="col-watch column">
          <Watchlist
            instruments={instruments}
            selected={symbol}
            onSelect={(s) => setSymbol(s)}
          />
        </div>
        <div className="col-controls column">
          <Card title="Configuration (simulation only)">
            <StrategySelector
              strategies={strategies}
              selected={strategyId}
              onChange={(id) => setStrategyId(id)}
            />
            <FundSelector
              value={fund}
              onChange={setFund}
              riskPct={riskPct}
              onRiskPctChange={setRiskPct}
            />
            <div className="ctl">
              <span className="ctl-label muted">Symbol</span>
              <span className="long">{symbol}</span>
            </div>
            {analysis && (
              <div className="ctl">
                <span className="ctl-label muted">Fund</span>
                <span className="long">{fmtINR(analysis.fund_inr)}</span>
              </div>
            )}
          </Card>
        </div>
        <div className="col-xl column">
          <Card title="Market & AI Analysis" className="card-wide">
            <div className="ticker-status">
              {analysisLoaded && (
                <span className={`badge ${(analysis?.market.price ?? null) != null ? "badge-green" : "badge-red"}`}>
                  PRICE: {analysis?.market.price != null ? fmtPrice(analysis.market.price) : "DATA UNAVAILABLE"}
                </span>
              )}
              {health && (
                <span className={`badge ${health.data_source === "websocket" ? "badge-green" : health.data_source ? "badge-amber" : "badge-muted"}`}>
                  SOURCE: {health.data_source ? health.data_source.toUpperCase() : "NONE"}
                </span>
              )}
              {health?.ws_status && (
                <span className={`badge ${health.ws_status.connected ? "badge-green" : "badge-red"}`}>
                  WS: {health.ws_status.connected ? "CONNECTED" : "DOWN"}
                </span>
              )}
              {analysisError && <span className="badge badge-red">ANALYSIS ERROR</span>}
              <span className="muted">
                {analysis?.market.candles_count
                  ? `${symbol} 1H · ${analysis.market.candles_count} candles`
                  : "analysis pending…"}
              </span>
            </div>
            {chart ? (
              <>
                {chart}
                <div className="chart-legend-row">
                  <span className="chart-legend">{fmtPrice(analysis?.market.price)}</span>
                  <span className="muted">{symbol}</span>
                </div>
              </>
            ) : (
              <div className="chart-empty" role="status">
                <div className="chart-empty-title">Market data unavailable</div>
                <div className="muted small">
                  No candles for {symbol} yet.
                </div>
              </div>
            )}
          </Card>
        </div>
      </div>

      <div className="grid">
        <DecisionPanel decision={analysis?.decision ?? null} loaded={analysisLoaded} />
        <LeveragePanel leverage={analysis?.leverage ?? null} loaded={analysisLoaded} />
      </div>

      {decision?.reason && (
        <div className="grid">
          <Card title="What does this mean?" className="card-wide">
            <details className="explain" open>
              <summary>Decision explanation</summary>
              <p className="mono">{decision.reason}</p>
            </details>
          </Card>
        </div>
      )}

      <div className="grid">
        <MarketHealthPanel monitor={monitor} loaded={loaded} />
      </div>

      <div className="grid">
        <TradePlanPanel
          plan={analysis?.plan ?? null}
          fund={analysis?.fund_inr ?? null}
          leverage={analysis?.leverage.effective ?? null}
          loaded={analysisLoaded}
        />
        <TimelinePanel timeline={analysis?.timeline ?? null} loaded={analysisLoaded} />
        <PaperPositionPanel paper={data.paper} loaded={loaded} />
        <ExecutionPanel
          executions={data.executions}
          safety={data.safety}
          loaded={loaded}
        />
      </div>

      <div className="grid">
        <PnlPanel paper={data.paper} risk={data.risk} loaded={loaded} />
      </div>

      <div className="grid">
        <SignalsTable signals={data.signals} />
        <TradesTable trades={data.trades} />
      </div>
      </details>
    </main>
  );
}
