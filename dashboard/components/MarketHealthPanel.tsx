"use client";

import type { MonitorInfo, SymbolHealth } from "@/lib/types";
import Card from "./Card";
import { fmtNum, fmtPrice } from "@/lib/format";

const STATUS_BADGE: Record<string, string> = {
  streaming: "badge-green",
  stale: "badge-amber",
  unavailable: "badge-red",
  invalid_price: "badge-red",
};

function ageText(s: SymbolHealth): string {
  if (s.last_candle_age_secs == null) return "-";
  const m = Math.round(s.last_candle_age_secs / 60);
  if (m < 60) return `${m}m`;
  return `${(m / 60).toFixed(1)}h`;
}

export default function MarketHealthPanel({
  monitor,
  loaded,
}: {
  monitor: MonitorInfo | null;
  loaded: boolean;
}) {
  const overall = monitor?.overall ?? null;
  const overallCls =
    overall === "streaming"
      ? "badge-green"
      : overall === "degraded"
      ? "badge-amber"
      : "badge-red";

  return (
    <Card title="Market Data Health" className="card-wide">
      <div className="pnl-label-row">
        <span className={`badge ${overallCls}`}>
          {loaded ? (overall ? overall.toUpperCase() : "---") : "---"}
        </span>
        <span className="muted small">
          {loaded ? `${monitor?.resolution ?? "1h"} · stale if &gt; ${(monitor?.fresh_secs ?? 0) / 60}m` : "24/7 REST+WS monitor"}
        </span>
      </div>
      <table>
        <thead>
          <tr>
            <th>Symbol</th>
            <th>Status</th>
            <th>Price</th>
            <th>Candles</th>
            <th>Age</th>
            <th>OI</th>
            <th>Funding</th>
          </tr>
        </thead>
        <tbody>
          {(monitor?.symbols ?? []).map((s) => (
            <tr key={s.symbol} title={s.status_reason ?? s.error ?? ""}>
              <td>{s.symbol}</td>
              <td>
                <span className={`badge ${STATUS_BADGE[s.status] ?? "badge-muted"}`}>
                  {s.status.toUpperCase()}
                </span>
              </td>
              <td>{s.price != null ? fmtPrice(s.price) : "-"}</td>
              <td className="muted">{s.candles_count}</td>
              <td className="muted">{ageText(s)}</td>
              <td className="muted">
                {s.oi_missing ? "missing" : s.open_interest != null ? fmtNum(s.open_interest) : "-"}
              </td>
              <td className="muted">
                {s.funding_missing ? "missing" : s.funding != null ? `${s.funding}%` : "-"}
              </td>
            </tr>
          ))}
          {monitor && monitor.symbols.length === 0 && (
            <tr>
              <td colSpan={7} className="muted">monitor has no data yet</td>
            </tr>
          )}
        </tbody>
      </table>
    </Card>
  );
}