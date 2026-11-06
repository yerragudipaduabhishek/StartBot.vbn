import type { Decision } from "@/lib/types";
import Card from "./Card";
import { fmtPrice, fmtNum, dirClass, dirLabel } from "@/lib/format";

export default function SignalPanel({
  decision,
  loaded,
}: {
  decision: Decision;
  loaded: boolean;
}) {
  const dir = String(decision.direction ?? "n/a");
  const isNone = dir === "n/a" || dir === "none";
  const explanation = String(decision.explanation ?? decision.reason ?? "awaiting signal");

  return (
    <Card title="Signal">
      <div className="signal-row">
        <div className={`big ${isNone ? "muted" : dirClass(dir)}`}>
          {loaded ? dirLabel(dir) : "---"}
        </div>
        {!isNone && (
          <div className="signal-meta">
            {decision.confidence != null && (
              <span className="signal-pill">
                CONF {fmtNum(decision.confidence * 100, 0)}%
              </span>
            )}
            {decision.score != null && (
              <span className="signal-pill">
                SCORE {decision.score > 0 ? "+" : ""}
                {fmtNum(decision.score)}
              </span>
            )}
          </div>
        )}
      </div>
      {!isNone && (
        <div className="signal-levels">
          <div className="row">
            <span className="muted">Entry</span>
            <span>{fmtPrice(decision.entry)}</span>
          </div>
          <div className="row">
            <span className="muted">Stop Loss</span>
            <span className="short">{fmtPrice(decision.stop_loss)}</span>
          </div>
          <div className="row">
            <span className="muted">Take Profit</span>
            <span className="long">{fmtPrice(decision.target)}</span>
          </div>
        </div>
      )}
      <div className="mono">{explanation}</div>
    </Card>
  );
}