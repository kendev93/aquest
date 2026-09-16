export type CombineMode = 'all' | 'any' | 'at_least'
export type Universe = 'all' | 'sh' | 'sz' | 'custom'
export type FormulaMode = 'preset' | 'custom'
export type BacktestExecution = 'next_open' | 'next_close'
export type BacktestPositionMode = 'full' | 'fixed'
export type PortfolioRankOrder = 'asc' | 'desc'
export type PortfolioRebalanceFrequency = 'daily' | 'weekly' | 'monthly'
export type PortfolioSellValueOperator = 'gte' | 'lte'
export type PortfolioCompareOperator = 'gt' | 'gte' | 'lt' | 'lte'
export type InstrumentType = 'stock' | 'fund' | 'index' | 'bond'
export type InstrumentBoard = 'main' | 'star' | 'chinext' | 'b_share' | 'fund' | 'index' | 'bond'
export type DateRangeMode = 'recent5' | 'full' | 'custom'
export type StrategyDiscoveryDateMode = DateRangeMode
export interface DateRangeSelection {
  mode: DateRangeMode
  startDate: string
  endDate: string
}
export type MarketDataProvider = 'eastmoney' | 'tdx'
export type MarketSyncRangePreset = 'all' | '5y' | '3y' | '1y' | 'ytd' | 'custom'
export type RuleMode = 'signals' | 'conditions'
export type ConditionOperator = 'gt' | 'gte' | 'lt' | 'lte' | 'eq' | 'neq' | 'cross_up' | 'cross_down'

export interface SignalDefinition {
  id: string
  display_name: string
  description: string
}

export interface ValueDefinition {
  id: string
  display_name: string
  description: string
  /** Output crosses zero on both sides, so `> 0` / `< 0` are both satisfiable. */
  bidirectional?: boolean
  /** Custom-formula output that is still a raw price/volume level, not a ratio. */
  price_scale?: boolean
}

export interface IndicatorDefinition {
  id: string
  display_name: string
  minimum_bars: number
  recommended_bars: number
  signals: SignalDefinition[]
  values?: ValueDefinition[]
}

export interface ConditionOperatorDefinition {
  value: ConditionOperator
  label: string
}

export interface FormulaScreenMetadata {
  indicators: IndicatorDefinition[]
  combine_modes: { value: CombineMode; label: string }[]
  condition_operators?: ConditionOperatorDefinition[]
  supported_markets: string[]
  supported_universe: { value: Universe; label: string }[]
  periods: { value: 'daily'; label: string }[]
  data_directory_help: string
  default_vipdoc_path?: string
}

export interface CustomParameterDefinition {
  name: string
  default: number
  minimum: number
  maximum: number
  step: number
}

export interface CustomSignalDefinition {
  id: string
  display_name: string
  description: string
}

export interface CustomFormulaMetadata {
  parameters: CustomParameterDefinition[]
  signals: CustomSignalDefinition[]
  values?: ValueDefinition[]
  minimum_bars: number
  warnings: string[]
}

export interface ScreenFormState {
  mode: FormulaMode
  ruleMode: RuleMode
  selectedSignals: string[]
  conditions: ScreenCondition[]
  combineMode: CombineMode
  minimumMatches: number | null
  universe: Universe
  universeFile: string
  instrumentTypes: InstrumentType[]
  boards: InstrumentBoard[]
  workers: number
  period: 'daily'
  formulaText: string
  formulaParameters: Record<string, number>
}

export interface ScreenCondition {
  left_value: string
  operator: ConditionOperator
  right_value: string | null
  right_constant: number | null
}

export interface RuleGroupPayload {
  signals: string[]
  conditions: ScreenCondition[]
  required_conditions?: ScreenCondition[]
  combine_mode: CombineMode
  minimum_matches: number | null
}

export interface SavedConditionSet {
  id: string
  name: string
  ruleMode: RuleMode
  selectedSignals: string[]
  conditions: ScreenCondition[]
  combineMode: CombineMode
  minimumMatches: number | null
  mode: FormulaMode
  formulaText: string
  formulaParameters: Record<string, number>
}

export interface ScanPayload {
  selected_signals: string[]
  conditions?: ScreenCondition[]
  combine_mode: CombineMode
  minimum_matches: number | null
  universe: Universe
  universe_file: string | null
  workers: number
  period: 'daily'
  formula_text: string | null
  formula_parameters: Record<string, number>
  instrument_types?: InstrumentType[]
  boards?: InstrumentBoard[]
}

export interface JobState {
  job_id: string
  status: 'queued' | 'running' | 'completed' | 'failed'
  progress: number
  total_candidates: number
  total_scanned: number
  total_signals: number
  errors: number
  skipped: number
  error: string | null
  worker_count?: number | null
}

export interface ActiveJobState {
  job_id: string
  description: string
  status: 'queued' | 'running'
  progress: number
  total_candidates: number
  total_scanned: number
  total_signals?: number
  errors: number
  skipped?: number
  error: string | null
  result?: unknown | null
  worker_count?: number | null
  phase?: string | null
  metadata?: Record<string, unknown>
  cancel_requested?: boolean
  operation?: 'discovery' | 'refinement' | 'replay'
}

export interface MarketSyncResult {
  source?: 'online' | 'online_discovery'
  provider?: MarketDataProvider | 'mixed'
  status?: 'skipped'
  reason?: string
  updated_files?: number
  unchanged_files?: number
  total_candidates?: number
  processed?: number
  listed_instruments?: number
  filtered_instruments?: number
  new_instruments?: number
  existing_instruments?: number
  target_source?: string
  written_bars?: number
  errors?: number
  failure_reasons?: Record<string, number>
  history_sources?: Record<string, number>
  execution_mode?: string
  worker_count?: number
  staged_rows?: number
  merged_rows?: number
  stage_files?: string[]
  download_seconds?: number
  merge_seconds?: number
  cleanup_seconds?: number
  rate_limit_events?: number
  rate_limit_backoffs?: number
  operation?: string
  success?: boolean
  range_preset?: string
  requested_start?: string | null
  requested_end?: string | null
  actual_start?: string | null
  actual_end?: string | null
  total_targets?: number
  planned_jobs?: number
  completed?: number
  skipped?: number
  failed?: number
  rows?: number
  catalog_counts?: Record<string, number>
}

export interface MarketSyncJobState {
  job_id: string
  description?: string
  status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'
  progress: number
  total_candidates: number
  total_scanned: number
  errors: number
  error: string | null
  worker_count?: number | null
  phase?: string | null
  metadata?: Record<string, unknown>
  cancel_requested?: boolean
  result: MarketSyncResult | null
}

export interface MarketDataProviderCapability {
  provider: MarketDataProvider
  available: boolean
  read_only?: boolean
  version?: string | null
  error?: string | null
  instrument_types?: InstrumentType[]
  boards?: InstrumentBoard[]
  adjustments?: string[]
  history?: string
  history_sources?: string[]
  fallback_policy?: string
}

export interface MarketDataCapabilities {
  default_provider: MarketDataProvider
  read_only?: boolean
  supports_full_sync?: boolean
  database_path?: string
  instrument_count?: number
  sync_execution?: {
    mode: string
    worker_count: number
    staging_directory?: string
  }
  last_online_provider?: string | null
  last_online_provider_version?: string | null
  instrument_list_fingerprint?: string | null
  providers: Partial<Record<MarketDataProvider, MarketDataProviderCapability>>
}

export interface DataStoreStatus {
  database_path: string
  schema_version: number
  instrument_count: number
  bar_count: number
  data_start: string | null
  data_end: string | null
  last_online_sync_at: string | null
  last_online_provider?: string | null
  last_online_provider_version?: string | null
  auto_sync_job_id?: string | null
  auto_sync_enabled?: boolean
  auto_sync_interval_seconds?: number | null
  market_data_source?: 'indicator_lab' | string
  market_data_read_only?: boolean
  market_data_write_target?: 'indicator_lab' | string
  full_market_sync_supported?: boolean
  market_sync_lock?: Record<string, unknown> | null
  last_market_sync_summary?: Record<string, unknown> | null
  legacy_market_write_enabled?: boolean
}

export type LocalMarketScope = 'all' | 'SH' | 'SZ'
export type MarketChartPeriod = 'daily' | 'monthly' | 'yearly'

export interface LocalInstrument {
  market: 'SH' | 'SZ'
  code: string
  name?: string | null
  instrument_type: InstrumentType
  board?: InstrumentBoard
  provider?: string | null
  bars: number
  data_start: string | null
  data_end: string | null
  last_close: number | null
  error: string | null
}

export interface LocalInstrumentMeta {
  total: number
  page: number
  page_size: number
  pages: number
}

export interface MarketCandle {
  date: string
  open: number | null
  high: number | null
  low: number | null
  close: number | null
  volume: number | null
  amount: number | null
  ma: Record<string, number | null>
  rsi14: number | null
  macd: number | null
  macd_signal: number | null
  macd_histogram: number | null
}

export interface MarketFormulaPanelValue {
  id: string
  display_name: string
  values: Array<number | null>
  latest: number | null
}

export interface MarketFormulaPanel {
  id: string
  display_name: string
  dates: string[]
  values: MarketFormulaPanelValue[]
}

export interface LocalMarketChart {
  market: 'SH' | 'SZ'
  code: string
  period: MarketChartPeriod
  total_daily_bars: number
  bars: number
  available_data_start: string
  available_data_end: string
  data_start: string
  data_end: string
  candles: MarketCandle[]
  formula_panels?: MarketFormulaPanel[]
}

export interface ScreenResult {
  market: string
  code: string
  name?: string | null
  instrument_type?: InstrumentType
  board?: InstrumentBoard
  signal_date: number
  last_close: number
  matched_signals: string[]
  match_count: number
  indicator_values: Record<string, number | null>
}

export interface ResultsMeta {
  total_candidates: number
  total_scanned: number
  total_signals: number
  errors: number
  skipped: number
  failure_reasons: Record<string, number>
  skip_reasons: Record<string, number>
}

export interface ScreenHistorySummary {
  history_id: string
  created_at: string
  total_candidates: number
  total_scanned: number
  total_signals: number
  errors: number
  skipped: number
  result_count: number
}

export interface ScreenHistoryRecord {
  history_id: string
  created_at: string
  meta: ResultsMeta
  results: ScreenResult[]
}

export interface BacktestPayload {
  market: 'SH' | 'SZ'
  code: string
  date_mode?: DateRangeMode
  buy_signal?: string | null
  sell_signal?: string | null
  buy_rule?: RuleGroupPayload | null
  sell_rule?: RuleGroupPayload | null
  formula_text?: string | null
  formula_parameters?: Record<string, number>
  start_date?: string | null
  end_date?: string | null
  initial_cash?: number
  commission?: number
  min_commission?: number
  stamp_tax?: number
  slippage?: number
  execution?: BacktestExecution
  position_mode?: BacktestPositionMode
  fixed_size?: number | null
}

export interface BacktestEquityPoint {
  date: string
  cash: number | null
  position_value: number | null
  total: number | null
  drawdown: number | null
  drawdown_pct: number | null
}

export interface BacktestTrade {
  date: string
  name?: string | null
  direction: 'BUY' | 'SELL'
  size: number
  price: number
  commission: number
  slippage: number
  pnl: number
  cost_basis?: number
  rejected: boolean
}

export interface BacktestPosition {
  date: string
  size: number
  avg_price: number
  market_value: number
  unrealized_pnl: number
}

export interface BacktestResult {
  market: 'SH' | 'SZ'
  code: string
  name?: string | null
  bars: number
  start_date: string
  end_date: string
  buy_signal: string | null
  sell_signal: string | null
  performance: Record<string, number | null>
  equity_curve: BacktestEquityPoint[]
  trades: BacktestTrade[]
  positions: BacktestPosition[]
  configuration: Record<string, number | string | boolean | null>
  diagnostic: string | null
}

export interface BacktestJobState {
  job_id: string
  status: 'queued' | 'running' | 'completed' | 'failed'
  progress: number
  total_candidates: number
  total_scanned: number
  errors: number
  error: string | null
  result: BacktestResult | null
}

export type BacktestHistoryType = 'single_stock' | 'portfolio' | 'strategy_fitness' | 'condition_research'

export interface BacktestHistorySummary {
  history_id: string
  backtest_type: BacktestHistoryType
  created_at: string
  started_at: string
  finished_at: string
  duration_seconds: number
  summary: Record<string, unknown>
}

export interface BacktestHistoryRecord<T = Record<string, unknown>> extends BacktestHistorySummary {
  request: Record<string, unknown>
  result: T
}

export interface PortfolioBacktestPayload {
  universe: Universe
  date_mode?: DateRangeMode
  universe_file?: string | null
  instrument_types?: InstrumentType[]
  boards?: InstrumentBoard[]
  selected_signals: string[]
  combine_mode: CombineMode
  minimum_matches: number | null
  buy_rule?: RuleGroupPayload | null
  ranking_value: string
  rank_order?: PortfolioRankOrder
  max_positions: number
  rebalance_frequency?: PortfolioRebalanceFrequency
  formula_text?: string | null
  formula_parameters?: Record<string, number>
  sell_signal?: string | null
  stop_loss_pct?: number | null
  take_profit_pct?: number | null
  take_profit_mode?: 'pct' | 'atr'
  take_profit_atr_multiplier?: number | null
  take_profit_cooldown_days?: number
  stop_loss_mode?: 'pct' | 'atr'
  stop_loss_atr_multiplier?: number | null
  stop_loss_cooldown_days?: number
  trailing_stop_pct?: number | null
  trailing_stop_mode?: 'pct' | 'atr'
  trailing_stop_atr_multiplier?: number | null
  trailing_stop_cooldown_days?: number
  atr_period?: number
  sell_value?: string | null
  sell_value_operator?: PortfolioSellValueOperator | null
  sell_value_threshold?: number | null
  compare_left_value?: string | null
  compare_operator?: PortfolioCompareOperator | null
  compare_right_value?: string | null
  sell_rule?: RuleGroupPayload | null
  start_date?: string | null
  end_date?: string | null
  initial_cash?: number
  commission?: number
  min_commission?: number
  stamp_tax?: number
  slippage?: number
  execution?: BacktestExecution
  fitness_filter_enabled?: boolean
  fitness_min_score?: number
  fitness_min_trades?: number
  fitness_max_drawdown?: number
}

export interface PortfolioBacktestJobState {
  job_id: string
  status: 'queued' | 'running' | 'completed' | 'failed'
  progress: number
  total_candidates: number
  total_scanned: number
  errors: number
  error: string | null
  result: PortfolioBacktestResult | null
}

export interface PortfolioEquityPoint {
  date: string
  cash: number
  position_value: number
  total: number
  positions_count: number
  drawdown: number
  drawdown_pct: number
}

export interface PortfolioTrade {
  date: string
  signal_date: string
  market: 'SH' | 'SZ'
  code: string
  name?: string | null
  direction: 'BUY' | 'SELL'
  size: number
  price: number
  commission: number
  stamp_tax: number
  slippage: number
  pnl: number
  cost_basis: number
  reason: string
  instrument_type?: InstrumentType
  board?: InstrumentBoard
  risk_trigger?: 'take_profit' | 'stop_loss' | 'trailing_stop' | null
  cooldown_days?: number
  rejected: boolean
}

export interface PortfolioHolding {
  market: 'SH' | 'SZ'
  code: string
  name?: string | null
  size: number
  entry_price: number
  close: number
  unrealized_pnl: number
}

export interface PortfolioState {
  date: string
  cash: number
  position_value: number
  total: number
  positions_count: number
  holdings: PortfolioHolding[]
}

export interface PortfolioRankingCandidate {
  rank: number
  market: 'SH' | 'SZ'
  code: string
  name?: string | null
  score: number
  selected: boolean
  fitness_score?: number | null
  fitness_trades?: number | null
  fitness_passed?: boolean | null
  excluded_reason?: string | null
}

export interface PortfolioRankingEvent {
  date: string
  slots_available: number
  ranking_value: string
  candidates: PortfolioRankingCandidate[]
}

export interface PortfolioBacktestResult {
  universe: Universe
  total_candidates: number
  processed: number
  skipped: number
  errors: number
  bars: number
  start_date: string
  end_date: string
  max_positions: number
  ranking_value: string
  rank_order: PortfolioRankOrder
  fitness_filter_enabled?: boolean
  fitness_min_score?: number
  fitness_min_trades?: number
  fitness_max_drawdown?: number
  performance: Record<string, number | null>
  equity_curve: PortfolioEquityPoint[]
  trades: PortfolioTrade[]
  states: PortfolioState[]
  ranking_events: PortfolioRankingEvent[]
  failure_reasons: Record<string, number>
  diagnostic: string | null
  risk_controls?: Record<string, number | string | null>
}

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
}

export interface StrategyDiscoveryCandidate {
  candidate_hash: string
  complexity: number
  human_readable_rules: string
  config: PortfolioBacktestPayload
  metrics: Record<string, Record<string, number | string | boolean | null>>
}

export interface StrategyDiscoveryResult {
  history_id?: string
  mode?: 'global' | 'refinement'
  request: Record<string, unknown>
  results_by_sharpe: StrategyDiscoveryCandidate[]
  results_by_return: StrategyDiscoveryCandidate[]
  search_metadata: Record<string, unknown>
  validation_metadata: Record<string, unknown>
  algorithm_metadata?: Record<string, unknown>
  stage_durations_seconds?: Record<string, number>
  data_fingerprint?: string
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
  config: PortfolioBacktestPayload
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

export interface StrategyFitnessPayload extends Omit<PortfolioBacktestPayload, 'max_positions' | 'rebalance_frequency'> {
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

export type ConditionResearchSide = 'buy' | 'sell' | 'both'

export interface ConditionResearchPayload {
  universe: 'all' | 'sh' | 'sz'
  vipdoc_path?: string
  boards?: InstrumentBoard[]
  buy_rule: RuleGroupPayload
  sell_rule: RuleGroupPayload
  formula_text?: string | null
  formula_parameters?: Record<string, number>
  date_mode?: DateRangeMode
  start_date?: string | null
  end_date?: string | null
  search_sides?: ConditionResearchSide
  initial_cash?: number
  commission?: number
  min_commission?: number
  stamp_tax?: number
  slippage?: number
  execution?: BacktestExecution
  position_mode?: BacktestPositionMode
  fixed_size?: number | null
  min_trades?: number
  min_instruments?: number
  candidate_limit?: number
  holdout_candidates?: number
}

export interface ConditionResearchJobState {
  job_id: string
  status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'
  progress: number
  total_candidates: number
  total_scanned: number
  errors: number
  skipped: number
  error: string | null
  result: ConditionResearchReport | null
  phase?: string | null
  metadata?: Record<string, unknown>
}

export interface ConditionResearchMiningMetrics {
  support_count: number
  support_ratio: number
  instrument_count: number
  outcome_rate: number
  baseline_rate: number
  lift: number
}

export interface ConditionResearchStockResult {
  market: string
  code: string
  name: string | null
  closed_trades: number
  win_rate: number | null
  payoff_ratio: number | null
  profit_factor: number | null
  mean_trade_return: number | null
  median_trade_return: number | null
  total_return: number | null
  annual_return: number | null
  max_drawdown: number | null
  sharpe: number | null
  baseline_win_rate?: number | null
  improved_win_rate?: boolean | null
}

export interface ConditionResearchSummary {
  pooled_trades: {
    closed_trades: number
    wins: number
    losses: number
    win_rate: number | null
    payoff_ratio: number | null
    profit_factor: number | null
    mean_trade_return: number | null
    median_trade_return: number | null
    stocks_with_trades: number
  }
  per_stock: {
    stock_count: number
    median_win_rate: number | null
    median_payoff_ratio: number | null
    median_total_return: number | null
    median_annual_return: number | null
    median_max_drawdown: number | null
    median_sharpe: number | null
    compared_stock_count: number
    improved_stock_count: number
    improved_stock_ratio: number | null
  }
  stocks?: ConditionResearchStockResult[]
}

export interface ConditionResearchCandidate {
  candidate_id: string
  side: 'buy' | 'sell'
  condition: ScreenCondition
  mining: ConditionResearchMiningMetrics
  validation: ConditionResearchSummary
  passed_constraints: boolean
  constraint_failures: string[]
  full_period: ConditionResearchSummary | null
  holdout: ConditionResearchSummary | null
}

export interface ConditionResearchReport {
  universe: 'all' | 'sh' | 'sz'
  start_date: string
  end_date: string
  data_fingerprint?: string | null
  search_sides: ConditionResearchSide
  feature_count: number
  min_trades?: number
  min_instruments?: number
  /**
   * 每侧参与挖掘的样本总数。买样本按入场日计（unit=entry），
   * 卖样本按持仓期提前离场决策点计（unit=decision_point），两者口径不同。
   */
  sample_totals?: Partial<Record<ConditionResearchSide, { count: number; unit: string }>>
  /** 数据覆盖与幸存者偏差披露；契约要求不得静默当作完整历史全市场。 */
  disclosure?: {
    universe_scope: string
    universe: string
    survivorship_bias: boolean
    requested_instruments: number
    processed_instruments: number
    coverage_ratio: number
    notes: string[]
  }
  total_candidates: number
  processed: number
  skipped: number
  errors: number
  failure_reasons: Record<string, number>
  splits: Record<'train' | 'validation' | 'holdout', { start_date: string; end_date: string }>
  baseline: {
    train: ConditionResearchSummary
    validation?: ConditionResearchSummary
    full_period?: ConditionResearchSummary
    holdout?: ConditionResearchSummary
  }
  candidates: ConditionResearchCandidate[]
  diagnostic?: string
  algorithm: Record<string, string | number>
}
