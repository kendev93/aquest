/** strategy 相关的前端类型。 */

import type { BacktestExecution, InstrumentBoard, InstrumentType, StrategyDiscoveryDateMode, Universe } from './common'

import type { PortfolioBacktestPayload, PortfolioBacktestResult } from './portfolio'

import type { ScreenCondition } from './screen'


export interface StrategyDiscoveryPayload {
  universe: Universe
  universe_file?: string | null
  instrument_types?: InstrumentType[]
  boards?: InstrumentBoard[]
  required_buy_conditions?: ScreenCondition[]
  required_sell_conditions?: ScreenCondition[]
  date_mode?: StrategyDiscoveryDateMode
  start_date?: string | null
  end_date?: string | null
  max_positions: number
  initial_cash?: number
  commission?: number
  min_commission?: number
  stamp_tax?: number
  slippage?: number
  execution?: BacktestExecution
  max_trials?: number
  timeout_seconds?: number
  validation_candidates?: number
  holdout_candidates?: number
  min_trades?: number
  max_drawdown?: number | null
  max_turnover?: number | null
  max_complexity?: number
  multifidelity_enabled?: boolean
  seed?: number | null
}


export interface PortfolioRiskOptimizationPayload extends PortfolioBacktestPayload {
  max_trials: 1000 | 2000 | 3000
  seed?: number | null
}


export interface StrategyDiscoveryRefinementPayload {
  parent_history_id: string
  parent_candidate_hash: string
  refinement_radius: 'medium'
  max_trials: number
  seed?: number | null
}


export interface StrategyDiscoveryJobState {
  job_id: string
  status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'
  progress: number
  total_candidates: number
  total_scanned: number
  errors: number
  skipped: number
  error: string | null
  error_code?: string | null
  result: StrategyDiscoveryResult | null
  phase?: string | null
  metadata?: Record<string, unknown>
  cancel_requested?: boolean
}


export interface StrategyDiscoveryCandidate {
  candidate_hash: string
  complexity: number
  human_readable_rules: string
  // 后端 strategy_discovery/models.py::StrategyCandidate.to_config 只回填部分字段，
  // 且刻意不套用严格的组合回测 schema，因此这里是开放字典而非完整 payload。
  config: Record<string, unknown>
  metrics: Record<string, Record<string, number | string | boolean | null>>
}


export interface StrategyDiscoveryResult {
  history_id?: string
  risk_optimization_id?: string
  mode?: 'global' | 'refinement' | 'risk_optimization'
  request: Record<string, unknown>
  results_by_sharpe: StrategyDiscoveryCandidate[]
  results_by_return: StrategyDiscoveryCandidate[]
  search_metadata: Record<string, unknown>
  validation_metadata: Record<string, unknown>
  algorithm_metadata?: Record<string, unknown>
  stage_durations_seconds?: Record<string, number>
  data_fingerprint?: string
  configuration_fingerprint?: string
  duration_seconds?: number
  started_at?: string
  finished_at?: string
  refinement?: {
    parent_history_id: string
    parent_candidate_hash: string
    refinement_radius: 'medium'
    parent_candidate?: StrategyDiscoveryCandidate
    parent_metrics?: Record<string, unknown>
    parent_data_fingerprint?: string
    data_changed?: boolean
  }
}


export interface RiskOptimizationHistorySummary {
  job_id: string
  status: 'completed'
  started_at: string | null
  finished_at: string | null
  duration_seconds: number | null
  start_date: string | null
  end_date: string | null
  requested_trials: number | null
  evaluated_trials: number | null
  candidate_count: number
  validation_best_sharpe: number | null
  validation_best_annual_return: number | null
}


export interface RiskOptimizationHistoryPage {
  items: RiskOptimizationHistorySummary[]
  total: number
  limit: number
  offset: number
}


export type StrategyDiscoveryLeaderboard = 'sharpe' | 'return'


export interface StrategyPeriodMetrics {
  key: string
  label: string
  start_date: string
  end_date: string
  bars: number
  total_return: number | null
  annual_return: number | null
  max_drawdown: number | null
  sharpe: number | null
  total_trades: number
  win_rate: number | null
  diagnostic: string | null
  partial?: boolean
}


export interface StrategyReplayCandidate {
  rank: number
  candidate_hash: string
  human_readable_rules: string
  // 与后端 replay.py 的 `config: Mapping[str, object]` 对齐：候选配置是开放字典，
  // 复核时还会再注入 start_date/end_date，不是完整的组合回测 payload。
  config: Record<string, unknown>
  leaderboard_metrics: Record<string, unknown>
  overall_metrics: Record<string, unknown>
  periods: {
    ytd: StrategyPeriodMetrics
    one_year: StrategyPeriodMetrics
    two_year: StrategyPeriodMetrics
  }
  annual_metrics: StrategyPeriodMetrics[]
  backtest: PortfolioBacktestResult
  data_fingerprint: string
  diagnostic: string | null
}


export interface StrategyReplayResult {
  experiment_id: string
  leaderboard: StrategyDiscoveryLeaderboard
  replay_id?: string
  started_at?: string
  finished_at?: string
  duration_seconds?: number
  data_start: string
  data_end: string
  data_fingerprint: string
  source_data_fingerprint: string
  data_changed: boolean
  candidate_count: number
  candidates: StrategyReplayCandidate[]
  diagnostics: Record<string, string>
}


export interface StrategyReplayJobState {
  job_id: string
  status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'
  progress: number
  total_candidates: number
  total_scanned: number
  errors: number
  skipped: number
  error: string | null
  error_code?: string | null
  result: StrategyReplayResult | null
  phase?: string | null
  metadata?: Record<string, unknown>
}


export interface StrategyReplayHistoryRecord {
  replay_id: string
  experiment_id: string
  leaderboard: StrategyDiscoveryLeaderboard
  created_at: string
  started_at: string
  finished_at: string
  data_start: string
  data_end: string
  data_fingerprint: string
  duration_seconds: number
  result: StrategyReplayResult
}


export type FitnessLabel = 'strong' | 'watch' | 'weak' | 'insufficient'


export interface StrategyFitnessPayload extends Omit<PortfolioBacktestPayload, 'max_positions' | 'rebalance_frequency' | 'codes' | 'position_mode' | 'fixed_size'> {
  train_ratio?: number
  validation_ratio?: number
  min_trades?: number
  max_test_drawdown?: number
}


export interface FitnessPhaseMetrics {
  name: 'train' | 'validation' | 'test'
  start_date: string
  end_date: string
  bars: number
  total_trades: number
  win_rate: number | null
  total_return: number | null
  annual_return: number | null
  max_drawdown: number | null
  sharpe: number | null
  profit_factor: number | null
  expectancy: number | null
  avg_holding_days: number | null
  diagnostic: string | null
}


export interface StrategyFitnessCheck {
  id: string
  label: string
  passed: boolean
}


export interface StrategyFitnessResult {
  market: 'SH' | 'SZ'
  code: string
  name?: string | null
  bars: number
  data_start: string
  data_end: string
  suitability_score: number
  passed: boolean
  label: FitnessLabel
  passed_checks: number
  total_checks: number
  positive_periods: number
  checks: StrategyFitnessCheck[]
  train: FitnessPhaseMetrics
  validation: FitnessPhaseMetrics
  test: FitnessPhaseMetrics
  failure_reason: string | null
}


export interface StrategyFitnessReport {
  universe: Universe
  total_candidates: number
  processed: number
  skipped: number
  errors: number
  bars: number
  start_date: string
  end_date: string
  train_end_date: string
  validation_end_date: string
  ranking_value: string
  train_ratio: number
  validation_ratio: number
  min_trades: number
  max_test_drawdown: number
  results: StrategyFitnessResult[]
  failure_reasons: Record<string, number>
  diagnostic: string | null
}


export interface StrategyFitnessJobState {
  job_id: string
  status: 'queued' | 'running' | 'completed' | 'failed'
  progress: number
  total_candidates: number
  total_scanned: number
  errors: number
  error: string | null
  result: StrategyFitnessReport | null
}
