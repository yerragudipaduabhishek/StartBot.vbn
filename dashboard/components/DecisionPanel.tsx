import type { AnalysisDecision } from "@/lib/types";
import Card from "./Card";
import { fmtNum, dirClass, dirLabel } from "@/lib/format";

function VoteRow({ vote }: { vote: { agent: string; direction: string; score: number; explanation: string } }) {
  return (
    <div className="vet-row">
      <span className="muted">{vote.agent}</span>
      <span className={dirClass(vote.direction)}>{dirLabel(vote.direction)}</span>
      <span className="muted">{vote.score > 0 ? "+" : ""}{fmtNum(vote.score)}</span>
      <span className="vet-explain muted" title={vote.explanation}>{vote.explanation}</span>
    </div>
  );
}

export default function DecisionPanel({
  decision,
  loaded,
}: {
  decision: AnalysisDecision | null;
  loaded: boolean;
}) {
  if (!decision) {
    return (
      <Card title="AI Decision">
        <div className="big muted">---</div>
        <div className="mono">awaiting analysis</div>
      </Card>
    );
  }

  const dir = String(decision.direction ?? "none");
  const none = dir === "none" || dir === "n/a" || dir === "neutral";
  const votes = Object.values(decision.votes ?? {}).map((v) => ({
    agent: v.agent,
    direction: v.direction,
    score: v.score,
    explanation: v.explanation,
  }));

  return (
    <Card title="AI Decision">
      <div className="signal-row">
        <div className={`big ${none ? "muted" : dirClass(dir)}`}>
          {loaded ? dirLabel(dir) : "---"}
        </div>
        {!none && decision.confidence != null && (
          <div className="signal-meta">
            <span className="signal-pill">
              CONF {fmtNum(decision.confidence * 100, 0)}%
            </span>
            {decision.score != null && (
              <span className="signal-pill">
                SCORE {decision.score > 0 ? "+" : ""}
                {fmtNum(decision.score)}
              </span>
            )}
          </div>
        )}
      </div>
      {none && decision.reason && (
        <div className="mono short">{decision.reason}</div>
      )}
      {votes.length > 0 && (
        <div className="vet-list">
          {votes.map((v) => (
            <VoteRow key={v.agent} vote={v} />
          ))}
        </div>
      )}
      {decision.reasoning && !none && <div className="mono">{decision.reasoning}</div>}
    </Card>
  );
}