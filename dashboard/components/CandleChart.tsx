"use client";

import { useMemo } from "react";
import type { Candle } from "@/lib/types";
import { emaLine } from "@/lib/ema";

const W = 720;
const H = 320;
const P = { t: 12, r: 10, b: 22, l: 8 };

const EMA_META = [
  { period: 8, color: "#f59e0b", label: "EMA8" },
  { period: 40, color: "#3b82f6", label: "EMA40" },
  { period: 50, color: "#a78bfa", label: "EMA50" },
];

export default function CandleChart({
  candles,
  symbol,
  timeframe = "1H",
}: {
  candles: Candle[];
  symbol?: string;
  timeframe?: string;
}) {
  const view = useMemo(() => {
    const n = candles.length;
    if (n === 0) return null;

    let min = Infinity;
    let max = -Infinity;
    for (const c of candles) {
      if (c.low < min) min = c.low;
      if (c.high > max) max = c.high;
    }
    const pad = (max - min) * 0.05 || 1;
    min -= pad;
    max += pad;

    const plotW = W - P.l - P.r;
    const plotH = H - P.t - P.b;
    const step = plotW / n;
    const bw = Math.max(2, step * 0.6);

    const x = (i: number) => P.l + i * step + step / 2;
    const y = (p: number) => P.t + (1 - (p - min) / (max - min)) * plotH;

    const closes = candles.map((c) => c.close);
    const emas = EMA_META.map((m) => ({ ...m, line: emaLine(closes, m.period) }));

    const last = candles[n - 1].close;
    const yLast = y(last);
    const ts0 = candles[0].time;
    const tsN = candles[n - 1].time;

    return { n, x, y, min, max, bw, emas, yLast, last, ts0, tsN };
  }, [candles]);

  if (!view) {
    return <div className="muted">no candles yet</div>;
  }

  const fmt = (p: number) =>
    p.toLocaleString("en-US", { maximumFractionDigits: 2 });

  const lastLabels = view.emas
    .filter((m) => m.line[view.n - 1] != null)
    .map((m) => (
      <span key={m.period} className="chart-legend" style={{ color: m.color }}>
        {m.label} {fmt(m.line[view.n - 1] as number)}
      </span>
    ));

  return (
    <div className="chart-wrap">
      <div className="chart-legend-row">
        <span className="chart-legend" style={{ color: "var(--text)" }}>
          {symbol ?? "BTCUSDT"} {timeframe}
        </span>
        {lastLabels}
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} className="chart-svg" role="img" aria-label="BTC candlestick chart with EMAs">
        <text x={P.l} y={P.t + 10} className="chart-axis" textAnchor="start">
          {fmt(view.max)}
        </text>
        <text x={P.l} y={H - P.b - 4} className="chart-axis" textAnchor="start">
          {fmt(view.min)}
        </text>
        <text x={W - P.r} y={H - 4} className="chart-axis" textAnchor="end">
          {new Date(view.tsN * 1000).toLocaleString()}
        </text>

        <line
          x1={P.l}
          y1={view.yLast}
          x2={W - P.r}
          y2={view.yLast}
          className="chart-last"
        />
        <text x={W - P.r} y={view.yLast - 4} className="chart-axis" textAnchor="end">
          {fmt(view.last)}
        </text>

        {candles.map((c, i) => {
          const cx = view.x(i);
          const up = c.close >= c.open;
          const color = up ? "var(--green)" : "var(--red)";
          const yO = view.y(c.open);
          const yC = view.y(c.close);
          const top = Math.min(yO, yC);
          const bodyH = Math.max(1, Math.abs(yO - yC));
          return (
            <g key={c.time}>
              <line x1={cx} y1={view.y(c.high)} x2={cx} y2={view.y(c.low)} stroke={color} strokeWidth={1} />
              <rect x={cx - view.bw / 2} y={top} width={view.bw} height={bodyH} fill={color} />
            </g>
          );
        })}

        {view.emas.map((m) => (
          <polyline
            key={m.period}
            fill="none"
            stroke={m.color}
            strokeWidth={1.4}
            strokeOpacity={0.9}
            points={m.line
              .map((v, i) => (v != null ? `${view.x(i)},${view.y(v as number)}` : null))
              .filter(Boolean)
              .join(" ")}
          />
        ))}
      </svg>
    </div>
  );
}