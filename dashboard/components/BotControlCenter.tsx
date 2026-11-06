"use client";

import { useState } from "react";
import type { DashboardData } from "@/lib/api";
import { controlBot } from "@/lib/api";
import type { AnalysisResult, HealthInfo } from "@/lib/types";
import { fmtINR, fmtPrice } from "@/lib/format";

export default function BotControlCenter({
  data,
  analysis,
  health,
  symbol,
  loaded,
}: {
  data: DashboardData;
  analysis: AnalysisResult | null;
  health: HealthInfo | null;
  symbol: string;
  loaded: boolean;
}) {
  const decision = analysis?.decision;
  const direction = decision?.direction?.toUpperCase() ?? "WAITING";
  const isBlocked = data.risk.trading_halted || decision?.reason?.toLowerCase().includes("reject");
  const decisionClass = direction === "LONG" ? "long" : direction === "SHORT" ? "short" : "neutral";
  const status = data.risk.trading_halted ? "HALTED" : loaded ? "RUNNING" : "STARTING";
  const [adminToken, setAdminToken] = useState("");
  const [controlMessage, setControlMessage] = useState("");
  const [controlBusy, setControlBusy] = useState(false);

  async function runControl(action: "start" | "pause" | "emergency-halt") {
    if (!adminToken.trim()) {
      setControlMessage("Enter the admin token to control the bot.");
      return;
    }
    setControlBusy(true);
    setControlMessage("");
    try {
      await controlBot(action, adminToken.trim(), action === "emergency-halt" ? "dashboard emergency halt" : `dashboard ${action}`);
      const label = action === "emergency-halt" ? "halted" : action === "pause" ? "paused" : "started";
      setControlMessage(`Bot ${label} successfully.`);
    } catch (error) {
      setControlMessage(error instanceof Error ? error.message : "Control request failed");
    } finally {
      setControlBusy(false);
    }
  }

  return (
    <section className="bot-console" aria-label="Bot control center">
      <div className="bot-console-head">
        <div>
          <div className="bot-kicker">AUTOMATED TRADING CONTROL CENTER</div>
          <h1>BTC AI BOT</h1>
          <p>Paper trading · simulated funds · no real orders</p>
        </div>
        <div className="bot-actions">
          <button className="bot-btn bot-btn-green" disabled={controlBusy} onClick={() => runControl("start")}>
            ▶ Start paper bot
          </button>
          <button className="bot-btn" disabled={controlBusy} onClick={() => runControl("pause")}>
            ‖ Pause
          </button>
          <button className="bot-btn bot-btn-red" disabled={controlBusy} onClick={() => runControl("emergency-halt")}>
            ■ Emergency halt
          </button>
        </div>
      </div>

      <div className="bot-control-auth">
        <label htmlFor="admin-token">Admin token</label>
        <input id="admin-token" type="password" value={adminToken} onChange={(event) => setAdminToken(event.target.value)} placeholder="Required for bot controls" />
        <span>{controlMessage || "Controls affect paper automation only."}</span>
      </div>

      <div className="bot-top-status">
        <span className="bot-status-item"><i className={status === "RUNNING" ? "dot dot-green" : "dot dot-amber"} /> Bot: {status}</span>
        <span className="bot-status-item"><i className={health?.ws_status?.connected ? "dot dot-green" : "dot dot-red"} /> Data: {health?.ws_status?.connected ? "Connected" : "Unavailable"}</span>
        <span className="bot-status-item"><i className={data.safety?.live_mode ? "dot dot-red" : "dot dot-green"} /> Live trading: {data.safety?.live_mode ? "ON" : "OFF"}</span>
        <span className="bot-status-item"><i className="dot dot-green" /> Mode: PAPER</span>
      </div>

      <div className="bot-console-grid">
        <article className="bot-panel bot-status-panel">
          <div className="bot-panel-title">Bot status <span>●</span></div>
          <div className="bot-detail-row"><span>Name</span><strong>BTC AI Bot</strong></div>
          <div className="bot-detail-row"><span>Strategy</span><strong>Strategy A</strong></div>
          <div className="bot-detail-row"><span>Market</span><strong>{symbol} · 1H</strong></div>
          <div className="bot-detail-row"><span>Risk state</span><strong className={data.risk.trading_halted ? "short" : "long"}>{data.risk.trading_halted ? "HALTED" : "SAFE"}</strong></div>
          <div className="bot-detail-row"><span>Position</span><strong>{data.paper.open_position ? "OPEN" : "FLAT"}</strong></div>
        </article>

        <article className={`bot-panel bot-decision-panel ${decisionClass}`}>
          <div className="bot-panel-title">Active decision <span className="muted">current cycle</span></div>
          <div className="bot-decision-main">
            <div><span className="bot-muted-label">SIGNAL</span><strong>{direction}</strong></div>
            <div><span className="bot-muted-label">CONFIDENCE</span><strong>{decision?.confidence != null ? `${Math.round(decision.confidence * 100)}%` : "—"}</strong></div>
          </div>
          <div className={`bot-risk-result ${isBlocked ? "blocked" : "approved"}`}>
            <strong>{isBlocked ? "✕ RISK REJECTED" : "✓ AWAITING RISK CHECK"}</strong>
            <span>{isBlocked ? (decision?.reason ?? "Trading is halted by the risk engine") : "No order will be placed without approval"}</span>
          </div>
          <div className="bot-no-order">{isBlocked ? "⊘  NO ORDER PLACED" : "○  PAPER EXECUTION LOCKED UNTIL APPROVAL"}</div>
        </article>

        <article className="bot-panel">
          <div className="bot-panel-title">Paper account <span>₹</span></div>
          <div className="bot-equity">{fmtINR(data.paper.total_equity ?? data.paper.balance)}</div>
          <div className="bot-equity-label">EQUITY</div>
          <div className="bot-account-grid">
            <div><span>Daily P&amp;L</span><strong className={(data.paper.realized_pnl ?? 0) >= 0 ? "long" : "short"}>{fmtINR(data.paper.realized_pnl ?? 0)}</strong></div>
            <div><span>Drawdown</span><strong>{data.risk.peak_equity ? `${Math.max(0, ((data.risk.peak_equity - data.risk.equity) / data.risk.peak_equity) * 100).toFixed(2)}%` : "0.00%"}</strong></div>
            <div><span>Balance</span><strong>{fmtINR(data.paper.balance)}</strong></div>
          </div>
        </article>

        <article className="bot-panel">
          <div className="bot-panel-title">Market data <span className="long">●</span></div>
          <div className="bot-market-price">{data.ticker.price != null ? fmtPrice(data.ticker.price) : "—"}</div>
          <div className="bot-detail-row"><span>Source</span><strong>{health?.data_source ?? "WAITING"}</strong></div>
          <div className="bot-detail-row"><span>WebSocket</span><strong className={health?.ws_status?.connected ? "long" : "short"}>{health?.ws_status?.connected ? "CONNECTED" : "OFFLINE"}</strong></div>
          <div className="bot-detail-row"><span>Candles</span><strong>{data.ticker.candles_count}</strong></div>
        </article>
      </div>

      <div className="bot-bottom-grid">
        <article className="bot-panel bot-flat-panel">
          <div className="bot-panel-title">Open positions <span className="muted">paper</span></div>
          <div className="bot-flat">{data.paper.open_position ? "POSITION OPEN" : "FLAT"}</div>
          <p>{data.paper.open_position ? "Position is being monitored by the paper engine." : "The bot is not currently in a position."}</p>
        </article>
        <article className="bot-panel">
          <div className="bot-panel-title">Recent activity</div>
          <div className="bot-event"><i className="dot dot-green" /><span>Market data</span><small>{health?.ws_status?.connected ? "Connected" : "Waiting"}</small></div>
          <div className="bot-event"><i className="dot dot-amber" /><span>Signal</span><small>{direction}</small></div>
          <div className="bot-event"><i className={isBlocked ? "dot dot-red" : "dot dot-green"} /><span>Risk check</span><small>{isBlocked ? "Rejected" : "Pending"}</small></div>
          <div className="bot-event"><i className="dot dot-red" /><span>Execution</span><small>{isBlocked ? "No order placed" : "Locked"}</small></div>
        </article>
      </div>
    </section>
  );
}
