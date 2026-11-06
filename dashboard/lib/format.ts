export function fmtPrice(v: number | null | undefined): string {
  const n = Number(v);
  if (!Number.isFinite(n)) return "-";
  return n.toLocaleString("en-US", { style: "currency", currency: "USD" });
}

export function fmtNum(v: number | null | undefined, digits = 2): string {
  const n = Number(v);
  if (!Number.isFinite(n)) return "-";
  return n.toFixed(digits);
}

export function fmtINR(v: number | null | undefined, digits = 0): string {
  const n = Number(v);
  if (!Number.isFinite(n)) return "-";
  return "₹" + n.toLocaleString("en-IN", { maximumFractionDigits: digits });
}

export function fmtLeverage(v: number | null | undefined): string {
  const n = Number(v);
  if (!Number.isFinite(n)) return "-";
  return `${n}x`;
}

export function fmtPriceINR(v: number | null | undefined): string {
  const n = Number(v);
  if (!Number.isFinite(n)) return "-";
  if (n >= 1000) return "₹" + n.toLocaleString("en-IN", { maximumFractionDigits: 0 });
  return "₹" + n.toLocaleString("en-IN", { maximumFractionDigits: 2 });
}

export function dirClass(d: string | null | undefined): string {
  const s = String(d ?? "").toLowerCase();
  if (["long", "buy", "neutral"].includes(s)) return s;
  if (s) return "short";
  return "muted";
}

export function dirLabel(d: string | null | undefined): string {
  const s = String(d ?? "n/a").toLowerCase();
  if (s === "long" || s === "buy") return "LONG";
  if (s === "short" || s === "sell") return "SHORT";
  if (s === "none" || s === "neutral") return "NO TRADE";
  return s === "n/a" ? "NO SIGNAL" : s.toUpperCase();
}