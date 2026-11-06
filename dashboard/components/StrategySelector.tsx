import type { StrategyInfo } from "@/lib/types";

export default function StrategySelector({
  strategies,
  selected,
  onChange,
}: {
  strategies: StrategyInfo[];
  selected: string;
  onChange: (id: string) => void;
}) {
  const active = strategies.find((s) => s.id === selected) ?? strategies[0];
  if (!active) {
    return (
      <div className="ctl">
        <span className="ctl-label muted">Strategy</span>
        <span className="muted">---</span>
      </div>
    );
  }
  return (
    <div className="ctl">
      <span className="ctl-label muted">Strategy</span>
      <select
        className="ctl-select"
        value={active.id}
        onChange={(e) => onChange(e.target.value)}
        aria-label="Strategy"
      >
        {strategies.map((s) => (
          <option key={s.id} value={s.id}>
            {s.name} · {s.status} · {s.timeframe}
          </option>
        ))}
      </select>
      <span className="badge badge-green">{active.status}</span>
      <span className="muted small">{active.timeframe}</span>
    </div>
  );
}