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
}

export interface MarketSyncResult {
  source?: 'local' | 'online' | 'combined'
  status?: 'skipped'
  reason?: string
  vipdoc_path?: string
  discovered_files?: number
  imported_files?: number
  updated_files?: number
  unchanged_files?: number
  skipped_files?: number
  missing_files?: number
  imported_instruments?: number
  replaced_instruments?: number
  imported_bars?: number
  provisional_bars?: number
  filtered_files?: number
  total_candidates?: number
  processed?: number
  written_bars?: number
  errors?: number
  failure_reasons?: Record<string, number>
  local_import?: MarketSyncResult
  online_sync?: MarketSyncResult
}

export interface MarketSyncJobState {
  job_id: string
  status: 'queued' | 'running' | 'completed' | 'failed'
  progress: number
  total_candidates: number
  total_scanned: number
  errors: number
  error: string | null
  result: MarketSyncResult | null
}

export interface DataStoreStatus {
  database_path: string
  schema_version: number
  instrument_count: number
  bar_count: number
  data_start: string | null
  data_end: string | null
  last_local_import_at: string | null
  last_online_sync_at: string | null
  startup_import_job_id?: string | null
  auto_sync_job_id?: string | null
  auto_sync_enabled?: boolean
  auto_sync_interval_seconds?: number | null
}

export type LocalMarketScope = 'all' | 'SH' | 'SZ'
export type MarketChartPeriod = 'daily' | 'monthly' | 'yearly'

export interface LocalInstrument {
  market: 'SH' | 'SZ'
  code: string
  instrument_type: InstrumentType
  board?: InstrumentBoard
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

export interface BacktestPayload {
  market: 'SH' | 'SZ'
  code: string
  buy_signal: string
  sell_signal: string
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
  bars: number
  start_date: string
  end_date: string
  buy_signal: string
  sell_signal: string
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

export interface PortfolioBacktestPayload {
  universe: Universe
  universe_file?: string | null
  instrument_types?: InstrumentType[]
  boards?: InstrumentBoard[]
  selected_signals: string[]
  combine_mode: CombineMode
  minimum_matches: number | null
  ranking_value: string
  rank_order?: PortfolioRankOrder
  max_positions: number
  rebalance_frequency?: PortfolioRebalanceFrequency
  formula_text?: string | null
  formula_parameters?: Record<string, number>
  sell_signal?: string | null
  stop_loss_pct?: number | null
  take_profit_pct?: number | null
  sell_value?: string | null
  sell_value_operator?: PortfolioSellValueOperator | null
  sell_value_threshold?: number | null
  compare_left_value?: string | null
  compare_operator?: PortfolioCompareOperator | null
  compare_right_value?: string | null
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
  direction: 'BUY' | 'SELL'
  size: number
  price: number
  commission: number
  stamp_tax: number
  slippage: number
  pnl: number
  cost_basis: number
  reason: string
  rejected: boolean
}

export interface PortfolioHolding {
  market: 'SH' | 'SZ'
  code: string
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
