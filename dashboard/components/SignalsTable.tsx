import type { SignalRow } from "@/lib/types";
import Card from "./Card";
import { fmtPrice, fmtNum, dirClass } from "@/lib/format";

export default function SignalsTable({ signals }: { signals: SignalRow[] }) {
  return (
    <Card title="Recent Signals">
      <table>
        <thead>
          <tr>
            <th>Time</th>
            <th>Dir</th>
            <th>Score</th>
            <th>Entry</th>
            <th>Stop</th>
            <th>Target</th>
          </tr>
        </thead>
        <tbody>
          {signals.map((s) => (
            <tr key={s.time}>
              <td className="muted">{new Date(s.time * 1000).toLocaleTimeString()}</td>
              <td>
                <span className={`badge-soft ${dirClass(s.direction)}`}>
                  {String(s.direction).toUpperCase()}
                </span>
              </td>
              <td>{s.score != null ? fmtNum(s.score) : "-"}</td>
              <td>{fmtPrice(s.entry)}</td>
              <td>{fmtPrice(s.stop)}</td>
              <td>{fmtPrice(s.target)}</td>
            </tr>
          ))}
          {signals.length === 0 && (
            <tr>
              <td colSpan={6} className="muted">no signals yet</td>
            </tr>
          )}
        </tbody>
      </table>
    </Card>
  );
}