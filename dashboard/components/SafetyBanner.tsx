import type { SafetyInfo } from "@/lib/types";
import { fmtNum } from "@/lib/format";

function HostLabel({ url }: { url: string }) {
  try {
    const h = new URL(url).hostname;
    return <span className="muted">{h}</span>;
  } catch {
    return <span className="muted">{url}</span>;
  }
}

export default function SafetyBanner({
  safety,
  loaded,
}: {
  safety: SafetyInfo | null;
  loaded: boolean;
}) {
  return (
    <div className="banner">
      <span className="badge badge-green" title="Paper-only engine: simulated fills, SQLite-persisted">
        PAPER TRADING
      </span>

      <span className="badge badge-amber" title="Paper balance uses a separate ₹ fund, never the selected Trading Fund">
        NOT REAL FUNDS
      </span>

      <span className="badge badge-green" title="The paper engine never places orders on Delta">
        NO EXCHANGE ORDERS
      </span>

      <span
        className={`badge ${safety?.live_mode ? "badge-red" : "badge-green"}`}
        title="LIVE_MODE"
      >
        LIVE TRADING: {loaded ? (safety?.live_mode ? "ON" : "OFF") : "---"}
      </span>

      <span
        className={`badge ${safety?.real_order_confirmation ? "badge-red" : "badge-green"}`}
        title="REAL_ORDER_CONFIRMATION"
      >
        REAL ORDER CONFIRMATION:{" "}
        {loaded ? (safety?.real_order_confirmation ? "ON" : "OFF") : "---"}
      </span>

      <span
        className={`badge ${safety?.execution_allowed ? "badge-green" : "badge-green"}`}
        title="Pipeline execution venue is paper (testnet)"
      >
        EXECUTION: TESTNET/PAPER
      </span>

      <span className="badge badge-muted" title="API keys configured">
        KEYS:{" "}
        {loaded ? (safety?.api_keys_configured ? "CONFIGURED" : "NONE") : "---"}
      </span>

      {safety?.delta_base_url && (
        <HostLabel url={safety.delta_base_url} />
      )}
    </div>
  );
}