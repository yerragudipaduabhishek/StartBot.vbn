import type { Paper, RiskInfo } from "@/lib/types";
import Card from "./Card";
import { fmtPrice } from "@/lib/format";

export default function PnlPanel({
  paper,
  risk,
  loaded,
}: {
  paper: Paper;
  risk: RiskInfo;
  loaded: boolean;
}) {
  const pos = paper.open_position;
  const dayColor = risk.day_pnl >= 0 ? "long" : "short";
  const equity = paper.total_equity ?? paper.equity ?? paper.balance;
  const unrealized = pos?.unrealized_pnl ?? paper.unrealized_pnl ?? 0;

  return (
    <Card title="Paper Portfolio & P&L">
      <div className="pnl-label-row">
        <span className="badge badge-amber">SIMULATED MONEY</span>
        <span className="muted small">paper ₹ fund · separate from the selected Trading Fund</span>
      </div>
      <div className="big">{loaded ? fmtPrice(equity) : "---"}</div>
      <div className="row">
        <span className="muted">Paper Balance</span>
        <span>{loaded ? fmtPrice(paper.balance) : "---"}</span>
      </div>
      <div className="row">
        <span className="muted">Paper Realized PnL</span>
        <span className={paper.realized_pnl >= 0 ? "long" : "short"}>
          {loaded ? fmtPrice(paper.realized_pnl) : "---"}
        </span>
      </div>
      <div className="row">
        <span className="muted">Paper Unrealized PnL</span>
        <span className={unrealized >= 0 ? "long" : "short"}>
          {loaded ? fmtPrice(unrealized) : "---"}
        </span>
      </div>
      <div className="row">
        <span className="muted">Paper Day PnL</span>
        <span className={dayColor}>{loaded ? fmtPrice(risk.day_pnl) : "---"}</span>
      </div>
      <div className="row">
        <span className="muted">Open Position</span>
        <span>
          {pos
            ? `${pos.side.toUpperCase()} ${pos.size} @ ${fmtPrice(pos.entry_price)}`
            : "flat"}
        </span>
      </div>
      <div className="row">
        <span className="muted">Peak Paper Equity</span>
        <span>{loaded ? fmtPrice(risk.peak_equity) : "---"}</span>
      </div>
      <div className="row">
        <span className="muted">Halted</span>
        <span className={risk.trading_halted ? "short" : "long"}>
          {risk.trading_halted ? "YES" : "no"}
        </span>
      </div>
    </Card>
  );
}