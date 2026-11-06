export function sma(values: number[], period: number, index: number): number | null {
  if (index < period - 1) return null;
  let sum = 0;
  for (let i = index - period + 1; i <= index; i++) sum += values[i];
  return sum / period;
}

/** EMA seeded from the SMA of the first `period` samples; null before that. */
export function emaLine(values: number[], period: number): (number | null)[] {
  const k = 2 / (period + 1);
  const out: (number | null)[] = [];
  let prev: number | null = null;
  for (let i = 0; i < values.length; i++) {
    if (i === period - 1) prev = sma(values, period, i);
    else if (i > period - 1 && prev !== null) prev = values[i] * k + prev * (1 - k);
    if (i >= period - 1) out.push(prev);
    else out.push(null);
  }
  return out;
}