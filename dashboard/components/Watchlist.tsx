"use client";

import { useEffect, useMemo, useState } from "react";
import type { InstrumentInfo } from "@/lib/types";
import Card from "./Card";

const LS_KEY = "btcai-watchlist";
const DEFAULTS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT"];

export function loadWatchlist(): string[] {
  if (typeof window === "undefined") return DEFAULTS;
  try {
    const raw = window.localStorage.getItem(LS_KEY);
    if (!raw) return DEFAULTS;
    const arr = JSON.parse(raw);
    if (Array.isArray(arr) && arr.length > 0) return arr.map(String);
    return DEFAULTS;
  } catch {
    return DEFAULTS;
  }
}

export function persistWatchlist(list: string[]): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(LS_KEY, JSON.stringify(list));
  } catch {
    /* localStorage unavailable — non-fatal */
  }
}

export default function Watchlist({
  instruments,
  selected,
  onSelect,
}: {
  instruments: InstrumentInfo[];
  selected: string;
  onSelect: (symbol: string) => void;
}) {
  const [items, setItems] = useState<string[]>(DEFAULTS);
  const [query, setQuery] = useState("");

  useEffect(() => {
    setItems(loadWatchlist());
  }, []);

  const availability = useMemo(() => {
    const map: Record<string, boolean> = {};
    for (const i of instruments) map[i.symbol] = i.available !== false;
    return map;
  }, [instruments]);

  const candidates = useMemo(() => {
    const known = instruments.map((i) => i.symbol).filter(Boolean);
    const set = new Set<string>([...known, ...DEFAULTS, ...items]);
    const q = query.trim().toUpperCase();
    return [...set]
      .filter((s) => (q ? s.includes(q) : true))
      .sort((a, b) => a.localeCompare(b));
  }, [instruments, query, items]);

  const add = (symbol: string) => {
    if (!symbol) return;
    const next = items.includes(symbol) ? items : [...items, symbol];
    setItems(next);
    persistWatchlist(next);
    setQuery("");
    onSelect(symbol);
  };

  const remove = (symbol: string) => {
    const next = items.filter((s) => s !== symbol);
    setItems(next.length ? next : DEFAULTS);
    persistWatchlist(next.length ? next : DEFAULTS);
  };

  return (
    <Card title="Watchlist" className="watchlist-card">
      <div className="wl-search">
        <input
          className="wl-input"
          type="text"
          value={query}
          placeholder="Search symbol…"
          onChange={(e) => setQuery(e.target.value)}
          aria-label="Search instrument symbol"
        />
        {query.trim() && (
          <ul className="wl-results">
            {candidates
              .filter((s) => !items.includes(s))
              .map((s) => (
                <li key={s}>
                  <button type="button" className="wl-item-none" onClick={() => add(s)}>
                    <span>{s}</span>
                    <span className="wl-add">+ add</span>
                  </button>
                </li>
              ))}
            {candidates.filter((s) => !items.includes(s)).length === 0 && (
              <li className="wl-empty muted">no matches</li>
            )}
          </ul>
        )}
      </div>
      <ul className="wl-list">
        {items.map((symbol) => {
          const active = selected === symbol;
          const ok = availability[symbol] !== false;
          return (
            <li key={symbol}>
              <button
                type="button"
                className={`wl-item ${active ? "wl-item-active" : ""}`}
                onClick={() => onSelect(symbol)}
                title={ok ? "available" : "market data unavailable"}
              >
                <span className="wl-symbol">{symbol}</span>
                <span className={`wl-badge ${ok ? "badge-green" : "badge-red"}`}>
                  {ok ? "•" : "!"}
                </span>
              </button>
              <button
                type="button"
                className="wl-remove"
                onClick={() => remove(symbol)}
                aria-label={`Remove ${symbol}`}
              >
                ×
              </button>
            </li>
          );
        })}
      </ul>
      <div className="wl-note muted small">
        Add/remove/search is saved locally. Selection stays read-only.
      </div>
    </Card>
  );
}