export type StrategyPoolObjective = 'annual_return' | 'sharpe'
export type StrategyPoolMode = 'full' | 'single' | 'signals' | 'portfolio'

export interface StrategyPoolMetrics {
  total_return?: number | null
  annual_return?: number | null
  sharpe?: number | null
  win_rate?: number | null
  payoff_ratio?: number | null
  max_drawdown?: number | null
  closed_trades?: number | null
  total_trades?: number | null
}

export interface StrategyPoolCandidate {
  candidate_id: string
  candidate_hash?: string
  objective?: StrategyPoolObjective
  buy_rule: Record<string, unknown>
  sell_rule: Record<string, unknown>
  config: Record<string, unknown>
  metrics?: Partial<Record<'full_period' | 'train' | 'validation' | 'holdout', StrategyPoolMetrics>>
  quality?: { badges?: string[]; sample_adequate?: boolean; high_drawdown_risk?: boolean }
  replayable?: boolean
  candidate_ref?: StrategyPoolObjective
}

export interface StrategyPoolBenchmarkPhase {
  start_date?: string
  end_date?: string
  bars?: number
  total_return?: number | null
  annual_return?: number | null
  sharpe?: number | null
  max_drawdown?: number | null
}

export interface StrategyPoolBenchmark {
  label?: string
  method?: string
  full_period?: StrategyPoolBenchmarkPhase | null
  train?: StrategyPoolBenchmarkPhase | null
  validation?: StrategyPoolBenchmarkPhase | null
  holdout?: StrategyPoolBenchmarkPhase | null
}

export interface StrategyPoolEntry {
  market: 'SH' | 'SZ'
  code: string
  name: string
  automatic_candidates: Record<StrategyPoolObjective, StrategyPoolCandidate | null>
  manual_overrides: Record<StrategyPoolObjective, StrategyPoolCandidate | null>
  active_candidate: StrategyPoolCandidate | null
  active_source: 'manual' | 'automatic'
  status: 'pending' | 'ready' | 'partial' | 'no_strategy' | 'failed' | string
  last_error?: string
  data_fingerprint?: string
  market_fingerprint?: string
  config_fingerprint?: string
  source_job_id?: string
  updated_at?: string | null
  benchmark?: StrategyPoolBenchmark | null
}

export interface StrategyPoolSignal extends StrategyPoolMetrics {
  market: 'SH' | 'SZ'
  code: string
  name: string
  symbol: string
  as_of_date?: string
  signal_type?: 'buy' | 'sell' | ''
  status: 'buy' | 'sell' | 'buy_sell' | 'held' | 'flat' | 'no_strategy' | 'error' | string
  objective?: StrategyPoolObjective
  candidate_id?: string
  quality?: StrategyPoolCandidate['quality']
  full_period_return?: number | null
  holdout_return?: number | null
  validation_trades?: number | null
  holdout_trades?: number | null
  error?: string | null
}

export interface StrategyPoolHolding {
  market: 'SH' | 'SZ'
  code: string
  name?: string | null
  status?: 'held' | string
  holding_return_pct?: number | null
}

export interface StrategyPoolStrategySnapshotItem {
  market: 'SH' | 'SZ'
  code: string
  candidate_id: string
}

export interface StrategyPoolMarketSnapshot {
  data_end: string | null
  bar_count: number
  last_online_sync_at: string | null
}

export interface StrategyPoolJobResult {
  mode?: StrategyPoolMode
  objective?: StrategyPoolObjective
  total_symbols?: number
  completed_symbols?: number
  skipped_symbols?: number
  failed_symbols?: number
  errors?: Array<{ symbol: string; error: string }>
  single_candidate?: {
    market: 'SH' | 'SZ'
    code: string
    name?: string
    benchmark?: StrategyPoolBenchmark | null
    annual_candidate?: StrategyPoolCandidate | null
    sharpe_candidate?: StrategyPoolCandidate | null
    split_metadata?: Record<string, unknown>
    status?: string
    error?: string
    saved_to_pool?: boolean
  } | null
  holdout_portfolios?: Record<StrategyPoolObjective, Record<string, unknown>>
  portfolio?: Record<string, unknown>
  common_calendar?: Record<string, unknown>
  as_of_date?: string | null
  signal_target?: { market: 'SH' | 'SZ'; code: string } | null
  holding_scope?: 'per_stock_strategy' | string
  holding_snapshot_complete?: boolean
  market_snapshot?: StrategyPoolMarketSnapshot
  strategy_snapshot?: StrategyPoolStrategySnapshotItem[]
  buy_signals?: StrategyPoolSignal[]
  sell_signals?: StrategyPoolSignal[]
  states?: StrategyPoolSignal[]
  model_portfolio?: {
    available?: boolean
    holding_scope?: 'per_stock_strategy' | string
    current_state?: Record<string, unknown> | null
    holdings?: StrategyPoolHolding[]
    risk_exit_signals?: Array<Record<string, unknown>>
    failed_batches?: number
    reason?: string
    data_fingerprint?: string
  }
}

export interface StrategyPoolJobState {
  job_id: string
  status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled' | string
  description?: string
  progress?: number
  phase?: string
  total_candidates?: number
  total_scanned?: number
  cancel_requested?: boolean
  error?: string | null
  metadata?: Record<string, unknown>
  result?: StrategyPoolJobResult | null
}

export interface StrategyPoolJobSummary {
  job_id: string
  status: string
  mode: StrategyPoolMode
  objective?: StrategyPoolObjective | null
  market?: string | null
  code?: string | null
  created_at?: number | string
  finished_at?: number | string
  completed_symbols?: number
  skipped_symbols?: number
  failed_symbols?: number
  total_symbols?: number
  error?: string | null
}
