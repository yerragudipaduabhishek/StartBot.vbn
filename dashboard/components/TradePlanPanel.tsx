import type { AnalysisPlan } from "@/lib/types";
import Card from "./Card";
import { fmtINR, fmtLeverage, fmtNum, fmtPriceINR } from "@/lib/format";

export default function TradePlanPanel({
  plan,
  fund,
  leverage,
  loaded,
}: {
  plan: AnalysisPlan | null;
  fund: number | null;
  leverage: number | null;
  loaded: boolean;
}) {
  if (!plan) {
    return (
      <Card title="Trade Plan">
        <div className="mono">awaiting analysis</div>
      </Card>
    );
  }

  const hasLevels = plan.entry != null && plan.stop_loss != null;

  return (
    <Card title="Trade Plan">
      {!hasLevels && <div className="mono">{plan.risk_line}</div>}
      {hasLevels && (
        <>
          <div className="row">
            <span className="muted">Entry</span>
            <span>{fmtPriceINR(plan.entry)}</span>
          </div>
          <div className="row">
            <span className="muted">Stop Loss</span>
            <span className="short">{fmtPriceINR(plan.stop_loss)}</span>
          </div>
          <div className="row">
            <span className="muted">Take Profit</span>
            <span className="long">{fmtPriceINR(plan.target)}</span>
          </div>
          <div className="row">
            <span className="muted">R / R</span>
            <span>{plan.rr != null ? `1 : ${fmtNum(plan.rr)}` : "-"}</span>
          </div>
          <div className="row">
            <span className="muted">Position Size</span>
            <span>{plan.position_size != null ? fmtNum(plan.position_size, 4) : "-"}</span>
          </div>
          {plan.notional != null && (
            <div className="row">
              <span className="muted">Notional</span>
              <span>{fmtPriceINR(plan.notional)}</span>
            </div>
          )}
          {plan.margin != null && (
            <div className="row">
              <span className="muted">Margin</span>
              <span>{fmtPriceINR(plan.margin)}</span>
            </div>
          )}
          <div className="row">
            <span className="muted">Leverage (effective)</span>
            <span className={leverage ? "long" : "muted"}>
              {loaded ? fmtLeverage(leverage) : "---"}
            </span>
          </div>
          <div className="row">
            <span className="muted">Trading Fund</span>
            <span>{fund != null ? fmtINR(fund) : "-"}</span>
          </div>
          <div className="row">
            <span className="muted">Risk Allowed</span>
            <span className={plan.risk_allowed ? "long" : "short"}>
              {plan.risk_allowed ? "YES" : "NO"}
            </span>
          </div>
          {plan.reason && <div className="mono">{plan.reason}</div>}
        </>
      )}
    </Card>
  );
}