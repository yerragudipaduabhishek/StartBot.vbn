import type { ExecutionRow } from "@/lib/types";
import Card from "./Card";
import { fmtPrice, fmtNum } from "@/lib/format";

function StateBadge({ label, value }: { label: string; value: string }) {
  const s = value.toLowerCase();
  const cls =
    s === "filled"
      ? "badge-long"
      : s === "cancelled" || s === "rejected"
        ? "badge-red"
        : s === "open"
          ? "badge-amber"
          : "badge-muted";
  return (
    <span className={`badge ${cls}`}>
      {label}: {value}
    </span>
  );
}

function LatestRow({ row }: { row: ExecutionRow }) {
  return (
    <div className="exec-latest">
      <div className="exec-states">
        <StateBadge label="ORDER" value={row.order_status} />
        <StateBadge label="POS" value={row.position_status} />
      </div>
      <div className="row">
        <span className="muted">Side / Size</span>
        <span className={row.side.toLowerCase() === "long" || row.side.toLowerCase() === "buy" ? "long" : "short"}>
          {row.side.toUpperCase()} {fmtNum(row.size)}
        </span>
      </div>
      <div className="row">
        <span className="muted">Entry (expect/actual)</span>
        <span>
          {fmtPrice(row.expected_entry)} / {fmtPrice(row.actual_entry)}
        </span>
      </div>
      <div className="row">
        <span className="muted">Slippage</span>
        <span>{fmtNum(row.slippage_bps)} bps</span>
      </div>
      <div className="row">
        <span className="muted">Fees</span>
        <span>{fmtPrice(row.fees)}</span>
      </div>
      <div className="row">
        <span className="muted">Latency</span>
        <span>{fmtNum(row.latency_ms, 0)} ms</span>
      </div>
      <div className="row">
        <span className="muted">Final PnL</span>
        <span className={row.final_pnl >= 0 ? "long" : "short"}>{fmtPrice(row.final_pnl)}</span>
      </div>
      {row.exit_reason && (
        <div className="row">
          <span className="muted">Exit Reason</span>
          <span>{row.exit_reason}</span>
        </div>
      )}
    </div>
  );
}

export default function ExecutionPanel({
  executions,
  safety,
  loaded,
}: {
  executions: ExecutionRow[];
  safety: {
    testnet_mode: boolean;
    api_keys_configured: boolean;
    execution_allowed: boolean;
  } | null;
  loaded: boolean;
}) {
  const latest = executions[0] ?? null;
  const ready = loaded && safety != null;

  return (
    <Card title="Execution / Testnet">
      <div className="exec-status">
        <span className={`badge ${safety?.testnet_mode ? "badge-green" : "badge-muted"}`}>
          TESTNET: {safety?.testnet_mode ? "ON" : "OFF"}
        </span>
        <span className={`badge ${safety?.api_keys_configured ? "badge-amber" : "badge-green"}`}>
          KEYS: {safety?.api_keys_configured ? "READY" : "PENDING"}
        </span>
        <span className="badge badge-green">PLACEMENT: LOCKED</span>
      </div>
      {!safety?.api_keys_configured && (
        <div className="exec-note muted">
          No API keys configured — awaiting credentials for live testnet validation.
        </div>
      )}
      {latest && <LatestRow row={latest} />}
      {!latest && ready && (
        <div className="exec-note muted">No testnet executions recorded yet.</div>
      )}
      {!ready && <div className="exec-note muted">---</div>}
    </Card>
  );
}