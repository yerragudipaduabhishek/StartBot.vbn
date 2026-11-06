import type {
  AnalysisResult,
  Decision,
  ExecutionRow,
  HealthInfo,
  InstrumentInfo,
  MonitorInfo,
  Paper,
  PromotionRow,
  ResearchExperiment,
  ResearchGates,
  ResearchProposal,
  ResearchWalkForwardRow,
  RiskInfo,
  SafetyInfo,
  SignalRow,
  StrategiesResponse,
  StrategyVersionRow,
  Ticker,
  TradeRow,
  WatchlistResponse,
} from "./types";

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(`/api/${path}`);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json() as Promise<T>;
}

async function postJson<T>(path: string, token: string, body: Record<string, unknown> = {}): Promise<T> {
  const res = await fetch(`/api/${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Admin-Token": token },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json() as Promise<T>;
}

export interface BotStatus {
  status: string;
  running: boolean;
  paused: boolean;
  mode: string;
  symbol?: string;
  timeframe?: string;
  cycle_count?: number;
  last_cycle_at?: string | null;
  risk_halted?: boolean;
  kill_switch_halted?: boolean;
  live_trading_enabled?: boolean;
}

export function fetchBotStatus(): Promise<BotStatus> {
  return getJson<BotStatus>("bot/status");
}

export function controlBot(action: "start" | "pause" | "emergency-halt", token: string, reason: string): Promise<BotStatus> {
  return postJson<BotStatus>(`bot/${action}`, token, { reason });
}

export interface DashboardData {
  ticker: Ticker;
  decision: Decision;
  paper: Paper;
  risk: RiskInfo;
  signals: SignalRow[];
  trades: TradeRow[];
  safety: SafetyInfo;
  executions: ExecutionRow[];
}

export async function fetchDashboard(): Promise<DashboardData> {
  const [ticker, decision, paper, risk, signals, trades, safety, executions] = await Promise.all([
    getJson<Ticker>("ticker"),
    getJson<Decision>("decision"),
    getJson<Paper>("paper"),
    getJson<RiskInfo>("risk"),
    getJson<SignalRow[]>("signals?limit=12"),
    getJson<TradeRow[]>("trades?limit=12"),
    getJson<SafetyInfo>("safety"),
    getJson<ExecutionRow[]>("executions?limit=8"),
  ]);
  return { ticker, decision, paper, risk, signals, trades, safety, executions };
}

export function fetchStrategies(): Promise<StrategiesResponse> {
  return getJson<StrategiesResponse>("strategies");
}

export function fetchWatchlist(): Promise<WatchlistResponse> {
  return getJson<WatchlistResponse>("watchlist");
}

export function fetchInstruments(): Promise<{ instruments: InstrumentInfo[]; error: string | null }> {
  return getJson<{ instruments: InstrumentInfo[]; error: string | null }>("instruments");
}

export function fetchAnalysis(
  symbol: string,
  strategy: string,
  fund: number,
  risk: number
): Promise<AnalysisResult> {
  const q = new URLSearchParams({ strategy, fund: String(fund), risk: String(risk) });
  return getJson<AnalysisResult>(`analysis/${symbol}?${q.toString()}`);
}

export function fetchHealth(): Promise<HealthInfo> {
  return getJson<HealthInfo>("health");
}

export function fetchMonitor(): Promise<MonitorInfo> {
  return getJson<MonitorInfo>("monitor");
}

export function fetchPaperAccount(): Promise<{
  starting_balance: number;
  balance: number;
  realized_pnl: number;
  unrealized_pnl: number;
  total_equity: number;
  currency: string;
  fee_pct: number;
  slippage_pct: number;
  fund_separate: boolean;
}> {
  return getJson("paper/account");
}

export function fetchResearchExperiments(): Promise<ResearchExperiment[]> {
  return getJson<ResearchExperiment[]>("research/experiments?limit=50");
}

export function fetchResearchProposals(): Promise<ResearchProposal[]> {
  return getJson<ResearchProposal[]>("research/proposals?limit=50");
}

export function fetchResearchGates(proposalId: string): Promise<ResearchGates> {
  return getJson<ResearchGates>(`research/proposals/${encodeURIComponent(proposalId)}/gates`);
}

export function fetchResearchStrategies(): Promise<StrategyVersionRow[]> {
  return getJson<StrategyVersionRow[]>("research/strategies");
}

export function fetchResearchPromotions(): Promise<PromotionRow[]> {
  return getJson<PromotionRow[]>("research/promotions?limit=50");
}

export function researchAction<T = Record<string, unknown>>(
  path: string,
  token: string,
  body: Record<string, unknown> = {}
): Promise<T> {
  return postJson<T>(path, token, body);
}

export function fetchResearchWalkForward(): Promise<ResearchWalkForwardRow[]> {
  return getJson<ResearchWalkForwardRow[]>("research/walk-forward?limit=50");
}
