"use client";

import type { Paper } from "@/lib/types";
import Card from "./Card";
import { fmtPrice, fmtNum } from "@/lib/format";

export default function PaperPositionPanel({
  paper,
  loaded,
}: {
  paper: Paper;
  loaded: boolean;
}) {
  const pos = paper.open_position;
  const entry = pos?.entry_price ?? 0;
  const current = pos?.current_price ?? pos?.entry_price ?? 0;

  return (
    <Card title="Paper Position" className={pos ? "card-pos-open" : ""}>
      <div className="pnl-label-row">
        <span className={`badge ${pos ? "badge-green" : "badge-muted"}`}>
          {loaded ? (pos ? "OPEN (SIMULATED)" : "FLAT") : "---"}
        </span>
        {loaded && todoFallback(pos) && <PaperStaleNote />}
      </div>
      {pos ? (
        <div className="pos-row">
          <div className="pos-side">
            <span className={`pos-dir ${pos.side === "long" ? "long" : "short"}`}>
              {pos.side.toUpperCase()}
            </span>
            <span className="muted">{pos.symbol ?? ""}</span>
          </div>
          <div className="pos-grid">
            <span className="muted">Size</span>
            <span>{fmtNum(pos.size)}</span>
            <span className="muted">Entry</span>
            <span>{fmtPrice(entry)}</span>
            <span className="muted">Mark</span>
            <span>{fmtPrice(current)}</span>
            <span className="muted">Notional</span>
            <span>{pos.notional != null ? fmtPrice(pos.notional) : "-"}</span>
            <span className="muted">Leverage</span>
            <span>{pos.leverage != null ? `${pos.leverage}x` : "-"}</span>
            <span className="muted">Stop Loss</span>
            <span>{pos.stop_loss != null ? fmtPrice(pos.stop_loss) : "-"}</span>
            <span className="muted">Take Profit</span>
            <span>{pos.take_profit != null ? fmtPrice(pos.take_profit) : "-"}</span>
          </div>
          <div className="pos-pnl">
            <span className="muted small">UNREALIZED P&L</span>
            <span className={pos.unrealized_pnl != null && pos.unrealized_pnl >= 0 ? "long" : "short"}>
              {fmtPrice(pos.unrealized_pnl ?? 0)}
            </span>
          </div>
          <div className="muted small">opened {pos.opened_at ? new Date(pos.opened_at).toLocaleString() : "-"}</div>
        </div>
      ) : (
        <div className="muted">No simulated position open. Generated entries track the pipeline in real time.</div>
      )}
    </Card>
  );
}

function todoFallback(pos: Paper["open_position"] | null): boolean {
  return pos != null && pos.current_price == null;
}

function PaperStaleNote() {
  return <span className="muted small">no live mark yet</span>;
}