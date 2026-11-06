import type { AnalysisLeverage } from "@/lib/types";
import Card from "./Card";
import { fmtLeverage, fmtNum } from "@/lib/format";

const STATUS_CLS: Record<string, string> = {
  "CAPPED BY RISK ENGINE": "badge-amber",
  "WITHIN RISK LIMIT": "badge-green",
  "DATA UNAVAILABLE": "badge-red",
};

export default function LeveragePanel({
  leverage,
  loaded,
}: {
  leverage: AnalysisLeverage | null;
  loaded: boolean;
}) {
  if (!leverage) {
    return (
      <Card title="Dynamic Leverage">
        <Big value={null} muted />
        <div className="mono">awaiting analysis</div>
      </Card>
    );
  }

  const unavailable = !leverage.effective && leverage.status === "DATA UNAVAILABLE";
  const capped = leverage.status === "CAPPED BY RISK ENGINE";

  return (
    <Card title="Dynamic Leverage">
      <div className="leverage-head-row">
        <span className="badge badge-muted">AI ADVISOR + RISK ENGINE</span>
        {loaded && (
          <span className={`badge ${STATUS_CLS[leverage.status] ?? "badge-muted"}`}>
            {leverage.status}
          </span>
        )}
      </div>
      <Big value={leverage.effective} muted={unavailable} />

      <div className="leverage-scale">
        <div className="leverage-scale-bar">
          <span className="leverage-scale-track" />
          <span
            className="leverage-scale-fill"
            style={{ width: `${Math.min(100, (leverage.effective ?? 0) * 10)}%` }}
          />
        </div>
        <div className="row">
          <span className="muted">AI Recommended</span>
          <span className={capped ? "amber" : "long"}>
            {loaded ? fmtLeverage(leverage.ai_recommended) : "---"}
          </span>
        </div>
        <div className="row">
          <span className="muted">Risk Engine Max</span>
          <span className="muted">{loaded ? fmtLeverage(leverage.risk_max) : "---"}</span>
        </div>
      </div>

      <div className="leverage-factors-row">
        <span className="badge badge-muted">
          RISK: {leverage.market_risk}
        </span>
        <span className="badge badge-soft neutral">
          TREND: {leverage.trend}
        </span>
        <span className="badge badge-soft neutral">
          VOL: {leverage.volatility}
        </span>
        {leverage.signal_confidence != null && (
          <span className="badge badge-soft neutral">
            CONF: {fmtNum(leverage.signal_confidence * 100, 0)}%
          </span>
        )}
      </div>

      {loaded && leverage.reason && <div className="mono">{leverage.reason}</div>}
    </Card>
  );
}

function Big({ value, muted }: { value: number | null; muted?: boolean }) {
  if (value == null || muted) return <div className="big muted">DATA UNAVAILABLE</div>;
  return <div className="big">{fmtLeverage(value)}</div>;
}