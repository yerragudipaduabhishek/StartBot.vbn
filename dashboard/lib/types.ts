export interface Candle {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface Ticker {
  symbol: string;
  product_id: number | null;
  price: number | null;
  mark_price: number | null;
  volume: number | null;
  open_interest: number | null;
  oi_value_usd: number | null;
  funding_rate: number | null;
  funding_8h: number | null;
  candles: Candle[];
  candles_count: number;
  data_status: "streaming" | "stale" | "unavailable";
  last_update_unix: number | null;
  last_error: string | null;
  updated_at: number | null;
}

export interface OpenPosition {
  side: string;
  direction?: string;
  symbol?: string;
  size: number;
  quantity?: number;
  entry_price: number;
  current_price?: number;
  stop_loss: number | null;
  target: number | null;
  take_profit?: number | null;
  notional?: number;
  leverage?: number;
  unrealized_pnl?: number;
  realized_pnl?: number;
  fees?: number;
  slippage?: number;
  status?: string;
  opened_at?: string;
  updated_at?: string;
  reason?: string;
}

export interface Decision {
  direction: string;
  score: number | null;
  confidence: number | null;
  entry: number | null;
  stop_loss: number | null;
  target: number | null;
  accepted_size: number | null;
  risk_allowed: number | null;
  votes: unknown[];
  explanation?: string;
  reason?: string;
  time?: number;
}

export interface Paper {
  mode?: string;
  balance: number;
  starting_equity: number;
  realized_pnl: number;
  unrealized_pnl?: number;
  total_equity?: number;
  currency?: string;
  fee_pct?: number;
  slippage_pct?: number;
  equity?: number;
  open_position: OpenPosition | null;
  position_count?: number;
  fund_separate?: boolean;
  note?: string;
}

export interface RiskInfo {
  equity: number;
  realized_pnl: number;
  day_pnl: number;
  peak_equity: number;
  open_positions: number;
  daily_loss_limit: number;
  trading_halted: boolean;
}

export interface SignalRow {
  time: number;
  direction: string;
  score: number | null;
  entry: number | null;
  stop: number | null;
  target: number | null;
  created_at: string;
}

export interface TradeRow {
  id: number;
  side: string;
  size: number;
  entry: number | null;
  exit: number | null;
  pnl: number;
  status: string;
  exit_reason: string | null;
  fees?: number;
  slippage?: number;
  notional?: number;
  leverage?: number;
}

export interface SafetyInfo {
  live_mode: boolean;
  testnet_mode: boolean;
  real_order_confirmation: boolean;
  execution_allowed: boolean;
  api_keys_configured: boolean;
  delta_base_url: string;
}

export interface ExecutionRow {
  id: number;
  created_at: string;
  client_order_id: string;
  symbol: string;
  side: string;
  size: number;
  expected_entry: number;
  actual_entry: number | null;
  slippage: number;
  slippage_bps: number;
  expected_sl: number | null;
  actual_sl: number | null;
  expected_tp: number | null;
  actual_tp: number | null;
  fees: number;
  latency_ms: number;
  order_status: string;
  position_status: string;
  final_pnl: number;
  exit_reason: string | null;
}

// ---- Milestone 2: watchlist / strategy / fund / dynamic leverage ------------

export interface StrategyInfo {
  id: string;
  name: string;
  status: string;
  timeframe: string;
  description: string;
  parameters: {
    ema_fast: number;
    ema_slow: number;
    ema_mid: number;
    ema_spans: number[];
    threshold: number;
    min_confidence: number;
    sl_atr_multiple: number;
    target_atr_multiple: number;
    weights: Record<string, number>;
  };
}

export interface StrategiesResponse {
  strategies: StrategyInfo[];
  default: string;
}

export interface InstrumentInfo {
  symbol: string;
  available: boolean;
  product_id?: number | null;
  contract_value?: number | null;
  id?: string;
  display_name?: string | null;
  exchange?: string | null;
  market?: string | null;
  asset_type?: string | null;
  base_asset?: string | null;
  quote_currency?: string | null;
  data_status?: string | null;
}

export interface WatchlistResponse {
  default: string[];
  default_symbol: string;
  note: string;
  selected?: string | null;
  items?: InstrumentInfo[];
}

export interface AgentVote {
  agent: string;
  direction: string;
  score: number;
  confidence: number;
  explanation: string;
  details?: Record<string, unknown>;
}

export interface AnalysisMarket {
  symbol: string;
  price: number | null;
  atr_pct: number | null;
  candles: Candle[];
  candles_count: number;
  last_candle_time: number | null;
  funding_8h: number | null;
  open_interest: number | null;
  contract_value: number | null;
}

export interface AnalysisPlan {
  entry: number | null;
  stop_loss: number | null;
  target: number | null;
  rr: number | null;
  position_size: number | null;
  notional: number | null;
  margin: number | null;
  risk_allowed: boolean;
  reason: string | null;
  risk_line: string;
}

export interface AnalysisDecision {
  direction: string;
  score: number | null;
  confidence: number | null;
  votes: Record<string, AgentVote>;
  reasoning: string | null;
  reason: string | null;
}

export interface AnalysisLeverage {
  ai_recommended: number | null;
  risk_max: number | null;
  effective: number | null;
  status: string;
  reason: string;
  factors: Record<string, number | null>;
  market_risk: string;
  trend: string;
  volatility: string;
  signal_confidence: number | null;
}

export interface TimelineEvent {
  time: number;
  step: string;
  detail: string;
}

export interface AnalysisResult {
  symbol: string;
  strategy: StrategyInfo | null;
  fund_inr: number | null;
  risk_pct: number;
  max_risk_inr: number | null;
  market: AnalysisMarket;
  decision: AnalysisDecision;
  plan: AnalysisPlan;
  leverage: AnalysisLeverage;
  timeline: TimelineEvent[];
}

// ---- Phase 6: market data health + paper engine monitoring ----------------

export interface WsStatus {
  url: string;
  running: boolean;
  connected: boolean;
  reconnects: number;
  failures: number;
  last_message_ts: number | null;
  last_message_age_secs: number | null;
  connected_secs: number | null;
}

export interface TickerHealth extends Ticker {
  data_source: string | null;
  price_valid: boolean | null;
  last_candle_age_secs: number | null;
  ws_status: WsStatus | null;
}

export interface HealthInfo {
  status: string;
  ready: boolean;
  live_mode: boolean;
  data_status: string | null;
  candles_count: number;
  data_source: string | null;
  price_valid: boolean | null;
  last_candle_age_secs: number | null;
  ws_status: WsStatus | null;
  monitor: { overall: string | null; covered_symbols: number };
  paper: { mode: string; persisted: boolean; balance: number | null; position_open: boolean };
}

export interface SymbolHealth {
  symbol: string;
  status: "streaming" | "stale" | "unavailable" | "invalid_price";
  status_reason: string | null;
  price: number | null;
  price_valid: boolean;
  candles_count: number;
  last_candle_time: number | null;
  last_candle_age_secs: number | null;
  open_interest: number | null;
  funding: number | null;
  oi_missing: boolean;
  funding_missing: boolean;
  feed_ok: boolean;
  ticker_ok: boolean;
  source: string;
  error: string | null;
  checked_at: number | null;
}

export interface MonitorInfo {
  overall: string;
  symbols: SymbolHealth[];
  poll_secs: number;
  resolution: string;
  fresh_secs: number;
}
export interface ResearchExperiment {
  experiment_id: string;
  created_at: string | null;
  validated_at: string | null;
  strategy_id: string;
  strategy_version: string;
  symbol: string;
  timeframe: string;
  dataset_hash: string;
  status: string;
  metrics: Record<string, unknown>;
}

export interface ResearchProposal {
  proposal_id: string;
  created_at: string | null;
  updated_at: string | null;
  experiment_id: string;
  strategy_id: string;
  strategy_version: string;
  dataset_hash: string;
  artifact_path: string;
  params: Record<string, unknown>;
  metrics: Record<string, unknown>;
  status: string;
  rejection_reason: string | null;
}

export interface ResearchGateCheck {
  passed: boolean;
  reason?: string;
  observed?: unknown;
  required?: unknown;
}

export interface ResearchGates {
  proposal_id?: string;
  passed: boolean;
  failed_reasons: string[];
  [key: string]: unknown;
}

export interface StrategyVersionRow {
  strategy_id: string;
  version: string;
  status: string;
  active: boolean;
  dataset_hash: string;
  artifact_path: string;
  validation_run_id: string | null;
  promoted_at: string | null;
}

export interface PromotionRow {
  proposal_id: string;
  strategy_id: string;
  strategy_version: string;
  previous_version: string | null;
  promoted_at: string | null;
  promoted_by: string | null;
  rolled_back_at: string | null;
  rollback_reason: string | null;
  status: string;
}

export interface ResearchWalkForwardRow {
  run_id: string;
  experiment_id: string;
  strategy_id: string;
  dataset_hash: string;
  artifact_path: string;
  command: string;
  code_version: string;
  created_at: string | null;
  config: Record<string, unknown>;
  aggregate: Record<string, unknown>;
  folds: number;
}
