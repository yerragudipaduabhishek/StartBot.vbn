import type { TimelineEvent } from "@/lib/types";
import Card from "./Card";
import { fmtNum } from "@/lib/format";

function fmtTime(sec: number): string {
  const d = new Date(sec * 1000);
  if (Number.isNaN(d.getTime())) return "-";
  return d.toISOString().slice(11, 19);
}

export default function TimelinePanel({
  timeline,
  loaded,
}: {
  timeline: TimelineEvent[] | null;
  loaded: boolean;
}) {
  return (
    <Card title="Decision Timeline">
      <ol className="timeline">
        {(!timeline || timeline.length === 0) && (
          <li className="muted">awaiting analysis</li>
        )}
        {timeline?.map((ev, i) => (
          <li key={i} className={`tli ${ev.step === "EXECUTION" ? "tli-safe" : ""}`}>
            <span className="tli-time">{loaded ? fmtTime(ev.time) : fmtNum(ev.time)}</span>
            <span className="tli-step">{ev.step}</span>
            <span className="tli-detail muted">{ev.detail}</span>
          </li>
        ))}
      </ol>
    </Card>
  );
}