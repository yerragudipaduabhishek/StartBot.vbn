import { useState } from "react";
import { fmtINR } from "@/lib/format";

const PRESETS = [1000, 2500, 5000, 10000, 25000];

export default function FundSelector({
  value,
  onChange,
  riskPct,
  onRiskPctChange,
}: {
  value: number;
  onChange: (fund: number) => void;
  riskPct: number;
  onRiskPctChange: (pct: number) => void;
}) {
  const [custom, setCustom] = useState(false);
  const maxRisk = value * (riskPct / 100);
  const selected =
    value === 5000 ? 5000 : PRESETS.includes(value) ? value : null;
  const preset = selected ?? null;

  return (
    <div className="ctl ctl-stack">
      <div className="ctl-row">
        <span className="ctl-label muted">Trading Fund</span>
        <span>
          <span className="badge badge-muted">PAPER / TEST</span>{" "}
          <span className="muted small">not real money</span>
        </span>
      </div>
      <div className="ctl-row">
        {PRESETS.map((p) => (
          <button
            key={p}
            type="button"
            className={`ctl-pill ${preset === p ? "ctl-pill-active" : ""}`}
            onClick={() => {
              setCustom(false);
              onChange(p);
            }}
          >
            {fmtINR(p)}
          </button>
        ))}
        <button
          type="button"
          className={`ctl-pill ${custom ? "ctl-pill-active" : ""}`}
          onClick={() => {
            setCustom(true);
          }}
        >
          Custom
        </button>
      </div>
      {custom && (
        <input
          className="wl-input"
          type="number"
          min={100}
          step={100}
          value={value}
          placeholder="Amount (₹)"
          onChange={(e) => {
            const n = Number(e.target.value);
            if (Number.isFinite(n) && n >= 0) onChange(n);
          }}
          aria-label="Custom fund amount in INR"
        />
      )}
      <div className="ctl-row">
        <span className="ctl-label muted">Risk Per Trade</span>
        <select
          className="ctl-select"
          value={riskPct}
          onChange={(e) => onRiskPctChange(Number(e.target.value))}
          aria-label="Risk per trade percentage"
        >
          <option value={0.5}>0.5%</option>
          <option value={1}>1.0%</option>
          <option value={2}>2.0%</option>
        </select>
      </div>
      <div className="ctl-row">
        <span className="muted">Maximum Planned Risk</span>
        <span className="long">{fmtINR(maxRisk)}</span>
      </div>
    </div>
  );
}