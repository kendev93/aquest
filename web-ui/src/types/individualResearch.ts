import type { BacktestExecution, BacktestPositionMode, DateRangeMode } from './common'
import type { BacktestHistoryRecord, BacktestHistorySummary } from './history'
import type { PortfolioBacktestPayload } from './portfolio'

export interface IndividualResearchPayload {
  market: 'SH' | 'SZ'
  code: string
  date_mode: DateRangeMode
  start_date?: string | null
  end_date?: string | null
  reversal_multiple: number
  atr_window: number
  min_bars: number
  horizon: number
  value_ids: string[] | null
  signal_ids: string[] | null
  candidate_limit: number
  risk_trials: 1000 | 2000 | 3000
  initial_cash: number
  commission: number
  min_commission: number
  stamp_tax: number
  slippage: number
  execution: BacktestExecution
  position_mode: BacktestPositionMode
  fixed_size: number | null
}

export interface IndividualResearchMetrics {
  total_return?: number | null
  annual_return?: number | null
  max_drawdown?: number | null
  sharpe?: number | null
  win_rate?: number | null
  payoff_ratio?: number | null
  profit_factor?: number | null
  total_trades?: number | null
  closed_trades?: number | null
  data_fingerprint?: string | null
  error?: string
}

export interface IndividualResearchFullBacktest {
  start_date: string
  end_date: string
  bars: number
  closed_trades?: number
  performance: Record<string, number | null>
  benchmark?: {
    label: string
    instrument_count: number
    total_return: number | null
    annual_return: number | null
    max_drawdown: number | null
    // 买入持有净值序列（BenchmarkCurve）：int YYYYMMDD 日期与绝对净值，随跟踪快照 report 返回
    dates?: unknown[]
    totals?: unknown[]
  } | null
  benchmark_return?: number | null
  excess_return?: number | null
  equity_curve: Record<string, unknown>[]
  trades: Record<string, unknown>[]
  data_fingerprint?: string | null
  error?: string
}

export interface IndividualResearchCandidate {
  candidate_hash: string
  candidate_id: string
  condition_candidate_id: string | null
  human_readable_rules?: string | null
  condition_rules?: { buy: Record<string, unknown>; sell: Record<string, unknown> }
  config: PortfolioBacktestPayload
  metrics: {
    train?: IndividualResearchMetrics | null
    validation?: IndividualResearchMetrics | null
    holdout?: IndividualResearchMetrics | null
  }
  full_backtest_id?: string | null
  quality?: {
    badges: string[]
    sample_adequate: boolean
    high_drawdown_risk: boolean
    eligible_for_display: boolean
  }
  data_fingerprint?: string | null
}

export interface IndividualResearchSplitWindow {
  start: string | null
  end: string | null
  days: number
  purged_days: number
}

export interface IndividualResearchResult {
  history_id?: string
  market: 'SH' | 'SZ'
  code: string
  name: string
  start_date: string
  end_date: string
  date_range: { start: string; end: string; days: number }
  single_security_mode: true
  segmentation: Record<string, number | string | null>
  split_metadata: {
    train: IndividualResearchSplitWindow
    validation: IndividualResearchSplitWindow
    holdout: IndividualResearchSplitWindow
    [key: string]: unknown
  }
  condition_candidates: {
    buy: Record<string, unknown>[]
    sell: Record<string, unknown>[]
  }
  rule_candidates: {
    results_by_sharpe: Record<string, unknown>[]
    results_by_return: Record<string, unknown>[]
    evaluated_pairs: number
  }
  results_by_sharpe: IndividualResearchCandidate[]
  results_by_return: IndividualResearchCandidate[]
  full_backtests?: Record<string, IndividualResearchFullBacktest>
  candidate_count: number
  risk_rule_sets?: string[]
  risk_search_runs?: { rule_id: string; search_metadata: { evaluated_trials?: number; max_trials?: number; stop_reason?: string; fidelity_evaluations?: Record<string, number> }; stage_durations_seconds?: Record<string, number> }[]
  risk_trials_per_rule_set: number
  searched_value_ids: string[]
  searched_signal_ids: string[]
  data_fingerprint: string
  configuration_fingerprint: string
  limitations?: string[]
  reason?: string | null
}

export interface IndividualResearchJobState {
  job_id: string
  status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'
  progress: number
  total_candidates: number
  total_scanned: number
  errors: number
  skipped: number
  error: string | null
  result: IndividualResearchResult | null
  description: string
  phase: string | null
  metadata: Record<string, unknown>
  cancel_requested: boolean
}

export type IndividualResearchHistorySummary = BacktestHistorySummary
export type IndividualResearchHistoryRecord = BacktestHistoryRecord<IndividualResearchResult>
