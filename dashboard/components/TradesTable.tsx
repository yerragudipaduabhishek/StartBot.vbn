import type { TradeRow } from "@/lib/types";
import Card from "./Card";
import { fmtPrice, fmtNum, dirClass } from "@/lib/format";

export default function TradesTable({ trades }: { trades: TradeRow[] }) {
  return (
    <Card title="Trade History">
      <table>
        <thead>
          <tr>
            <th>Side</th>
            <th>Size</th>
            <th>Entry</th>
            <th>Exit</th>
            <th>PnL</th>
            <th>Fees</th>
            <th>Slippage</th>
            <th>Status</th>
            <th>Reason</th>
          </tr>
        </thead>
        <tbody>
          {trades.map((t) => (
            <tr key={t.id}>
              <td>
                <span className={`badge-soft ${dirClass(t.side)}`}>
                  {String(t.side).toUpperCase()}
                </span>
              </td>
              <td>{fmtNum(t.size)}</td>
              <td>{fmtPrice(t.entry)}</td>
              <td>{fmtPrice(t.exit)}</td>
              <td className={t.pnl >= 0 ? "long" : "short"}>{fmtPrice(t.pnl)}</td>
              <td className="muted">{t.fees != null ? fmtPrice(t.fees) : "-"}</td>
              <td className="muted">{t.slippage != null ? fmtPrice(t.slippage) : "-"}</td>
              <td className="muted">{t.status}</td>
              <td className="muted">{t.exit_reason ?? ""}</td>
            </tr>
          ))}
          {trades.length === 0 && (
            <tr>
              <td colSpan={9} className="muted">no trades yet</td>
            </tr>
          )}
        </tbody>
      </table>
    </Card>
  );
}