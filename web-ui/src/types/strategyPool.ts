export type StrategyPoolObjective = 'annual_return' | 'sharpe'
export type StrategyPoolMode = 'full' | 'single' | 'signals' | 'portfolio'

export interface StrategyPoolMetrics {
  total_return?: number | null
  annual_return?: number | null
  sharpe?: number | null
  win_rate?: number | null
  payoff_ratio?: number | null
  max_drawdown?: number | null
  avg_holding_days?: number | null
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

export interface StrategyPoolFocusItem {
  market: 'SH' | 'SZ'
  code: string
  name: string
}

/**
 * 关注开关需要的最小信息。买卖信号行、持仓行和标的表行都能给全，
 * 所以四处共用同一个按钮与同一个接口，不为某一处再开一套语义。
 */
export interface StrategyPoolFollowTarget {
  market: 'SH' | 'SZ'
  code: string
  name?: string | null
}

export interface StrategyPoolEntry {
  created_at?: string | null
  created_return?: number | null
  tracking_status?: string
  tracking_as_of_date?: string | null
  /**
   * 上线后窗口（从这一版策略的 launch_data_date 起算）。后端从当前生效版本（手工优先）
   * 的摘要里投影出来，`post_launch_has_data` 为假表示创建后还没有新行情——那是「还没跑」
   * 而不是「平盘」，不能和真数字一起排。
   */
  post_launch_return?: number | null
  post_launch_benchmark_return?: number | null
  post_launch_days?: number | null
  post_launch_has_data?: boolean | null
  /**
   * 该标的最新一根已完成日线的收盘价（不复权）及其日期。后端一次聚合从行情表直接投影，
   * 不读跟踪摘要，所以空仓、当天没有买卖信号、甚至还没有跟踪版本的行都有；它和风控三档
   * 线各自的日期不一定相同，不一致时表格会标出「价截至 X」。
   */
  latest_close?: number | null
  latest_close_date?: string | null
  tracking?: Record<string, unknown> | null
  market: 'SH' | 'SZ'
  code: string
  name: string
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

/**
 * 引擎按每标的生效配置算出的风控展示。三处视图共用：全市场页的当前持仓、
 * 个股详情的跟踪持仓、特别关注 / 全市场表格。价格是原始价口径，
 * `risk_level_as_of` 说明这条线用的是哪一天收盘后的数据。
 */
export interface HoldingRiskDisplay {
  stop_loss_price?: number | null
  trailing_stop_price?: number | null
  take_profit_price?: number | null
  risk_level_as_of?: string | null
  pending_exit_reason?: string | null
  pending_exit_signal_date?: string | null
  pending_exit_risk_trigger?: string | null
  max_hold_days?: number | null
  max_hold_days_left?: number | null
  max_hold_expiry_date?: string | null
}

export interface StrategyPoolHolding extends HoldingRiskDisplay {
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
  results_by_objective?: Partial<Record<StrategyPoolObjective, StrategyPoolJobResult>>
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
    failed_symbols?: number
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
  created_at?: number | string | null
  started_at?: number | string | null
  finished_at?: number | string | null
  error?: string | null
  metadata?: Record<string, unknown>
  result?: StrategyPoolJobResult | null
}

/** 「增量更新策略池」点下去之前的工作量预估，与任务预扫描同源。 */
export interface StrategyPoolResearchEstimate {
  universe_symbols: number
  pending_symbols: number
  reusable_symbols: number
  /** 历史样本不足、本轮不进队列的只数；它们按排期自动回来。 */
  deferred_symbols?: number
  /** 这些标的里最早自动重算的那一天（ISO 日期），一只都没有排期时为 null。 */
  next_eligible_date?: string | null
  rebuild: boolean
  risk_trials: number
  data_end: string | null
}

export interface StrategyPoolJobSummary {
  dual_objectives?: boolean
  rebuild?: boolean
  job_id: string
  status: string
  mode: StrategyPoolMode
  automatic?: boolean
  objective?: StrategyPoolObjective | null
  market?: string | null
  code?: string | null
  created_at?: number | string
  started_at?: number | string | null
  finished_at?: number | string | null
  duration_seconds?: number | null
  duration_includes_queue?: boolean
  completed_symbols?: number
  skipped_symbols?: number
  newly_researched_symbols?: number
  failed_symbols?: number
  total_symbols?: number
  maximum_parallelism?: number | null
  phase_label?: string | null
  error_code?: string | null
  error?: string | null
}

export interface StrategyPoolJobModeStats {
  samples: number
  avg_duration_seconds: number | null
  median_duration_seconds: number | null
  started_at_samples: number
  throughput_samples: number
  avg_seconds_per_symbol: number | null
  median_seconds_per_symbol: number | null
  avg_parallelism: number | null
}

export interface StrategyPoolJobStats {
  sample_limit: number
  modes: Partial<Record<StrategyPoolMode, StrategyPoolJobModeStats>>
}
