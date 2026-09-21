export type CombineMode = 'all' | 'any' | 'at_least'
export type Universe = 'all' | 'sh' | 'sz' | 'custom'
export type PortfolioUniverse = Universe | 'codes'
export type FormulaMode = 'preset' | 'custom'
export type BacktestExecution = 'next_open' | 'next_close'
export type BacktestPositionMode = 'full' | 'fixed'
export type PortfolioRankOrder = 'asc' | 'desc'
export type PortfolioRebalanceFrequency = 'daily' | 'weekly' | 'monthly'
export type PortfolioSellValueOperator = 'gte' | 'lte'
export type PortfolioCompareOperator = 'gt' | 'gte' | 'lt' | 'lte'
// 本系统只研究股票：基金（含 ETF）与指数不进候选池——ETF 跟踪同一指数会产生
// 大量共线标的，指数本身不可交易。
export type InstrumentType = 'stock'
export type InstrumentBoard = 'main' | 'star' | 'chinext'
export type DateRangeMode = 'recent5' | 'full' | 'custom'
export type StrategyDiscoveryDateMode = DateRangeMode
export interface DateRangeSelection {
  mode: DateRangeMode
  startDate: string
  endDate: string
}
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
  /** Safe to sort a cross-section of stocks by (no currency units, not constant). */
  rankable?: boolean
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

/** 筛选方案存的内容：整套可复现的输入，不含任何结果。字段用 snake_case，与接口一致。 */
export interface ScreenPresetPayload {
  mode: FormulaMode
  rule_mode: RuleMode
  selected_signals: string[]
  conditions: ScreenCondition[]
  combine_mode: CombineMode
  minimum_matches: number | null
  universe: Universe
  universe_file: string
  instrument_types: InstrumentType[]
  boards: InstrumentBoard[]
  workers: number
  period: 'daily'
  formula_text: string
  formula_parameters: Record<string, number>
}

export interface ScreenPresetSummary {
  preset_id: string
  name: string
  note: string
  created_at: string
  updated_at: string
}

export interface ScreenPreset extends ScreenPresetSummary {
  payload: ScreenPresetPayload
  /** 服务端裁剪时改动了什么。只有写操作会带。 */
  notes?: string[]
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
  /** 日线来源：在线同步 online/online_discovery，通达信本地导入 local。 */
  source?: 'online' | 'online_discovery' | 'local'
  /** 后端返回的字符串；历史记录里可能是已移除的 'tdx'。 */
  provider?: string
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
  /** 以下是通达信本地导入（source='local'）专用的键，在线同步不会返回。 */
  vipdoc_path?: string
  discovered_files?: number
  selected_files?: number
  filtered_files?: number
  imported_instruments?: number
  imported_bars?: number
  replaced_bars?: number
  skipped_files?: number
  missing_files?: number
  range_start?: string | null
  range_end?: string | null
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
  market_sync_lock?: Record<string, unknown> | null
  last_market_sync_summary?: Record<string, unknown> | null
  legacy_market_write_enabled?: boolean
}

export type LocalMarketScope = 'all' | 'SH' | 'SZ'

export interface LocalVipdocCandidate {
  path: string
  file_count: number
  markets: string[]
}
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

/** 画在 K 线上的买卖点（回测成交）。 */
export interface ChartMarker {
  date: string
  side: 'buy' | 'sell'
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

export type BacktestHistoryType =
  | 'single_stock'
  | 'portfolio'
  | 'strategy_fitness'
  | 'condition_research'
  | 'swing_research'
  | 'factor_test'

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
  universe: PortfolioUniverse
  date_mode?: DateRangeMode
  universe_file?: string | null
  codes?: string[] | null
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
  fitness_rank_weight?: number
  fitness_min_trades?: number
  fitness_max_drawdown?: number
  position_mode?: BacktestPositionMode
  fixed_size?: number | null
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
  rank_score?: number | null
  fitness_score?: number | null
  fitness_trades?: number | null
  fitness_note?: string | null
}

export interface PortfolioRankingEvent {
  date: string
  slots_available: number
  ranking_value: string
  candidates: PortfolioRankingCandidate[]
}

export interface PortfolioBenchmark {
  /** 基准口径的名称，界面上直接显示。 */
  label: string
  /** 真正进入基准的标的数（首日就有价格的才算）。 */
  instrument_count: number
  /** 与 equity_curve 同序的交易日，YYYYMMDD。 */
  dates: number[]
  totals: number[]
  performance: Record<string, number | null>
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
  fitness_rank_weight?: number
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
  // 后端 portfolio_backtest/service.py 会在结果上附带行情指纹，用于复核时判断数据是否变化。
  data_fingerprint: string
  /** 指定标的中行情库里找不到的代码（只有 universe === 'codes' 时才可能有）。 */
  missing_codes?: string[]
  /** 指定标的中被板块/类型范围过滤掉的代码。 */
  filtered_codes?: string[]
  /** 基准对照（标的池等权买入持有）。窗口太短或池内无可用标的时为 null。 */
  benchmark?: PortfolioBenchmark | null
}

/** 策略库里只保留可比的数字指标，曲线与成交明细不进策略表。 */
export interface SavedStrategyPerformance {
  total_return?: number | null
  annual_return?: number | null
  max_drawdown?: number | null
  sharpe?: number | null
  volatility?: number | null
  calmar?: number | null
  total_trades?: number | null
  win_rate?: number | null
}

/** 保存策略时顺手记下的结果快照，只够列表页展示。 */
export interface SavedStrategySnapshot {
  start_date?: string
  end_date?: string
  bars?: number
  max_positions?: number
  ranking_value?: string
  rank_order?: string
  universe?: string
  total_candidates?: number
  performance?: SavedStrategyPerformance
  benchmark_performance?: SavedStrategyPerformance
}

/** 策略库列表项：刻意不带 request——列表一次读全部策略，带着它太重。 */
export interface SavedStrategySummary {
  strategy_id: string
  name: string
  note: string
  source_history_id: string | null
  created_at: string
  updated_at: string
  summary: SavedStrategySnapshot
}

/** 策略详情，带可重放的原始配置（日期区间在保存时已固化成绝对日期）。 */
export interface SavedStrategyDetail extends SavedStrategySummary {
  request: PortfolioBacktestPayload
}

/** 追踪任务的状态。重跑是分钟级的全历史，所以永远走后台任务。 */
export interface StrategyTrackingJobState {
  job_id: string
  status: 'queued' | 'running' | 'completed' | 'failed'
  progress: number
  total_candidates: number
  total_scanned: number
  errors: number
  error: string | null
  result: StrategyTrackingSnapshot | null
}

/** 追踪里的一个交易日。上线日那一行是接力点，研究段和观察段共用它。 */
export interface StrategyTrackingPoint {
  trade_date: number
  equity: number
  cash: number
  positions_count: number
  benchmark_value: number | null
  drawdown: number | null
}

/** 一段窗口（研究段 = 上线前，观察段 = 上线后）的绩效。 */
export interface StrategyTrackingSegment {
  start_date: string
  end_date: string
  bars: number
  performance: SavedStrategyPerformance
}

/**
 * 一次追踪的全貌。
 * `decay` 是观察段 / 研究段，`excess_annual_return` 是观察段相对同期基准的年化差——
 * 这两个数是这个功能唯一想给出的答案，其余都只是支撑它们的证据。
 */
export interface StrategyTrackingSnapshot {
  strategy_id: string
  name?: string
  note?: string
  tracking_status: string
  launched_at: number | null
  last_tracked_date: number | null
  /** 失败原因。有值时 last_tracked_date 一定停在旧日期。 */
  last_error: string
  observations: StrategyTrackingPoint[]
  research: StrategyTrackingSegment | null
  observation: StrategyTrackingSegment | null
  decay: Record<string, number | null>
  benchmark_performance: SavedStrategyPerformance | null
  excess_annual_return: number | null
  /** 观察段太短，还没到可以谈"衰减"的程度。界面必须据此标注。 */
  observation_is_short: boolean
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
  // 后端 strategy_discovery/models.py::StrategyCandidate.to_config 只回填部分字段，
  // 且刻意不套用严格的组合回测 schema，因此这里是开放字典而非完整 payload。
  config: Record<string, unknown>
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

/** 一个分位档的未来收益表现。第 1 档是信息系数指出该买的那一组。 */
export interface FactorLayer {
  index: number
  /** 该档每天的平均股票数。三只股票的档位不是证据，这个数字必须看得见。 */
  size: number | null
  mean_return: number | null
  beat_market_rate: number | null
}

/** 首档减末档的每日价差。曲线是累加而非复利：持仓期重叠，复合会凭空放大。 */
export interface FactorLongShort {
  mean_spread: number | null
  std: number | null
  t_stat: number | null
  win_rate: number | null
  final: number | null
  max_drawdown: number | null
  days: number
  cumulative: number[]
}

export interface FactorEvidence {
  value_id: string
  display_name: string
  /** high = 买指标值最高的一组；low = 买最低的一组。由信息系数的符号决定。 */
  order: 'high' | 'low'
  ic_mean: number
  icir: number | null
  ic_days: number
  ic_positive_rate: number | null
  ic_series: (number | null)[]
  ic_cumulative: number[]
  ic_first_half: number | null
  ic_second_half: number | null
  layers: FactorLayer[]
  layer_inversions: number
  long_short: FactorLongShort
  ic_threshold: number | null
  pvalue: number | null
  qvalue: number | null
  significant: boolean
  note: string
}

export interface FactorTestReport {
  universe: Universe
  stock_count: number
  scanned_stocks: number
  value_count: number
  horizon: number
  min_cross_section: number
  permutations: number
  block_size: number
  alpha: number
  quantile_count: number
  date_range: { start: string | null; end: string | null; days: number }
  dates: string[]
  factors: FactorEvidence[]
  significant_count: number
  reason: string | null
  /** 口径边界，页面原文照抄展示。 */
  boundaries: string[]
}

export interface FactorTestJobState {
  job_id: string
  status: 'queued' | 'running' | 'completed' | 'failed'
  progress: number
  error: string | null
  result: FactorTestReport | null
}

export interface FactorTestPayload {
  universe?: 'all' | 'sh' | 'sz'
  vipdoc_path?: string
  instrument_types?: InstrumentType[]
  boards?: InstrumentBoard[]
  start_date?: string | null
  end_date?: string | null
  /** 前瞻天数：标签就是「未来 N 个交易日的收益」。 */
  horizon?: number
  value_ids?: string[] | null
  include_custom?: boolean
  min_cross_section?: number
  permutations?: number
  block_size?: number
  alpha?: number
  quantile_count?: number
  limit_stocks?: number | null
}

export type ConditionResearchSide = 'buy' | 'sell' | 'both'

export interface ConditionResearchPayload {
  universe: 'all' | 'sh' | 'sz'
  vipdoc_path?: string
  instrument_types?: InstrumentType[]
  boards?: InstrumentBoard[]
  // null = 不设基础买入条件（持续满仓），由研究直接搜索买入时机。
  buy_rule: RuleGroupPayload | null
  // null = 不设基础卖出条件（持有到期末），由研究直接搜索卖出时机。
  sell_rule: RuleGroupPayload | null
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
  /** 同义条件合并门槛：命中向量相关性达到它就只留排序最好的一条。1 = 不合并。 */
  duplicate_correlation?: number
  holdout_candidates?: number
  /** 滚动复验窗口数：1 = 不拆，只复验一次。 */
  walk_forward_windows?: number
  /** 置换检验次数：0 = 不检验。 */
  significance_permutations?: number
  /** 门槛分位数，默认 0.95。 */
  significance_quantile?: number
  // 排序规则：研究不建组合，但结论要落到组合回测的「候选池 + 有限槽位」模型上。
  ranking_value?: string | null
  rank_order?: RankOrder
  ranking_top_n?: number
}

export type RankOrder = 'asc' | 'desc'

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
  /** 前 N 名口径的胜率：只统计每个信号日排序前 N 名的命中样本。null = 没解析出排序指标。 */
  top_n_outcome_rate?: number | null
  /** 前 N 名口径的样本数。 */
  top_n_support_count?: number
  /** 命中数超过槽位数、排序真正起作用的交易日数。0 = 前 N 等于全池。 */
  top_n_filtered_days?: number
  /** 噪声门槛：把标签随机打乱重挖，纯运气能把 lift 推到的分位值。null = 这次没做置换检验。 */
  lift_threshold?: number | null
  /** 随机重挖中达到该候选 lift 的比例，越小越可信。 */
  permutation_pvalue?: number | null
  /** lift 是否超过噪声门槛。null = 没做检验，不判断。 */
  significant?: boolean | null
  /**
   * 被这条候选合并掉的同义条件：同一个发现的不同写法（多为阈值差一点）。
   * 列出来而不是静默删掉，用户才能判断合并门槛合不合适。
   */
  duplicates?: ConditionResearchDuplicate[]
}

/** 一条被判定为「同义」而合并掉的候选条件。 */
export interface ConditionResearchDuplicate {
  condition: ScreenCondition
  /** 与保留条件的命中向量相关性（φ 系数）。 */
  correlation: number
  lift: number
  support_count: number
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

/** 滚动复验里单个窗口的基线成绩。 */
export interface ConditionResearchBaselineWindow {
  index: number
  start_date: string
  end_date: string
  calendar_days: number
  trades: number
  win_rate: number | null
}

/** 滚动复验里单个窗口的成绩：同一批候选在同一个切片上的表现。 */
export interface ConditionResearchWindow {
  index: number
  start_date: string
  end_date: string
  calendar_days: number
  trades: number
  win_rate: number | null
  baseline_win_rate: number | null
  improved_stock_ratio: number | null
  improved_win_rate: boolean | null
  /** 窗口内成交数不足：不判优劣，直接从一致性统计里剔除。 */
  insufficient_sample: boolean
}

/**
 * 一致性汇总。刻意不给「稳健/不稳健」的结论——只给「几个窗口里几个优于基线」
 * 加每窗原始数值，判定阈值交给用户。
 */
export interface ConditionResearchConsistency {
  requested: number
  used: number
  evaluated: number
  skipped: number
  better_than_baseline: number
  median_win_rate: number | null
  worst_win_rate: number | null
  median_improved_stock_ratio: number | null
  note: string | null
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
  windows?: ConditionResearchWindow[]
  consistency?: ConditionResearchConsistency | null
  /** 阈值邻域曲线。没有可挪的标量参数时（比如交叉条件）为 null。 */
  sensitivity?: ConditionResearchSensitivity | null
}

/** 邻域里的一个切点：把阈值往松/往紧挪一档之后重新计分。 */
export interface ConditionResearchSensitivityPoint {
  /** 分位偏移：负=更宽松，0=基点，正=更严格。 */
  offset: number
  /** 偏移后的分位。 */
  quantile: number
  /** 偏移后的阈值（两两比较的条件是「差值」的阈值）。 */
  threshold: number
  support_count: number
  outcome_rate: number | null
  baseline_rate: number
  lift: number | null
  /** 优势保持率：(lift-1)/(基点 lift-1)。基点没有优势时为 null。 */
  retention: number | null
  /** 命中样本不足：这一格不计入统计。 */
  insufficient: boolean
  /** 阈值退化到条件恒真，同样不计入统计。 */
  degenerate: boolean
}

export interface ConditionResearchSensitivity {
  points: ConditionResearchSensitivityPoint[]
  /** 仍然有提升的偏移数 / 计入统计的偏移数。 */
  stable_offsets: number
  evaluated_offsets: number
  skipped: number
  median_retention: number | null
  worst_retention: number | null
  note: string | null
}

export interface ConditionResearchRankingEvidence {
  value_id: string
  display_name: string
  order: 'asc' | 'desc'
  ic_mean: number
  icir: number | null
  segments: number
  same_sign_segments: number
  quantile_returns: (number | null)[]
  top_n_lift: number | null
  sample_days: number
  confidence: 'high' | 'medium' | 'low' | 'none'
  note: string
}

export interface ConditionResearchRanking {
  top_n: number
  min_cross_section: number
  sample_days: number
  evaluated: ConditionResearchRankingEvidence[]
  recommended: ConditionResearchRankingEvidence | null
  reason: string | null
  available: boolean
  selection: { value: string | null; order: 'asc' | 'desc' }
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
  /** 同义条件合并门槛（命中向量相关性）。 */
  duplicate_correlation?: number
  /** 本次一共合并掉多少条同义条件。 */
  merged_duplicate_count?: number
  /**
   * 每侧参与挖掘的样本总数。买样本按入场日计（unit=entry），
   * 卖样本按持仓期提前离场决策点计（unit=decision_point），两者口径不同。
   * daily_hits 是「日均命中股票数」，直接和组合槽位数比大小就能判断条件是否还有意义。
   */
  sample_totals?: Partial<
    Record<
      ConditionResearchSide,
      { count: number; unit: string; hit_days?: number; daily_hits?: number | null }
    >
  >
  /** 排序指标推荐：只读结论，不改变研究自身的评估口径。 */
  ranking?: ConditionResearchRanking
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
    /** 滚动复验时逐窗的基线成绩，候选的胜率要拿它当对照。 */
    windows?: ConditionResearchBaselineWindow[]
  }
  /** 滚动复验是否真的跑了（留出段太短会自动降级到 1 窗 = 未启用）。 */
  rolling?: {
    enabled: boolean
    requested: number
    used: number
    note: string | null
  }
  candidates: ConditionResearchCandidate[]
  diagnostic?: string
  algorithm: Record<string, string | number>
}

/** 波段标签：leg_start 是事后归因，onset_ahead 当天可观测（可交易）。 */
export type SwingLabel =
  | 'up_leg_start'
  | 'down_leg_start'
  | 'up_onset_ahead'
  | 'down_onset_ahead'

export interface SwingBucket {
  bucket: string
  count: number
}

export interface SwingDistribution {
  count: number
  duration: Record<string, number | null>
  amplitude: Record<string, number | null>
  duration_histogram: SwingBucket[]
  amplitude_histogram: SwingBucket[]
}

export interface SwingScenario {
  reversal_multiple: number
  min_bars: number
  stock_count: number
  segment_count: number
  long_segment_count: number
  segments_per_stock_per_year: number | null
  up: SwingDistribution
  down: SwingDistribution
}

export interface SwingOverviewResult {
  stock_count: number
  scanned_stocks: number
  min_bars: number
  atr_window: number
  date_range: { start: string | null; end: string | null; years: number | null }
  scenarios: SwingScenario[]
}

export interface SwingLabelOption {
  id: SwingLabel
  display_name: string
}

export interface SwingScore {
  value_id: string
  display_name: string
  label: SwingLabel
  /** high = 指标值大时伴随该标签；low 反之。 */
  order: 'high' | 'low'
  ic_mean: number
  icir: number | null
  ic_days: number
  /** 置换检验给出的噪声门槛：信息系数要超过它才算数。 */
  ic_threshold: number | null
  top_lift: number | null
  threshold_hint: number | null
  pvalue: number | null
  qvalue: number | null
  significant: boolean
  note: string | null
}

export interface SwingConditionTemplate {
  value_id: string
  display_name: string
  label: SwingLabel
  label_display: string
  side: 'buy' | 'sell'
  tradeable: boolean
  operator: 'gte' | 'lte'
  threshold: number
  ic_mean: number
  icir: number | null
  significant: boolean
  /** 未登记为可筛选指标的输出只给结论，不给可粘贴的规则。 */
  condition_supported: boolean
  condition: ScreenCondition
  rule: RuleGroupPayload | null
  note: string
}

export interface SwingConditionPlan {
  templates: SwingConditionTemplate[]
  buy_rule: RuleGroupPayload | null
  sell_rule: RuleGroupPayload | null
  ranking: {
    value: string
    order: RankOrder
    display_name: string
    ic_mean: number
    note: string
  } | null
  tradeable_count: number
  reason: string | null
}

export interface SwingStudyResult {
  stock_count: number
  scanned_stocks: number
  reversal_multiple: number
  min_bars: number
  atr_window: number
  horizon: number
  permutations: number
  block_size: number
  alpha: number
  value_count: number
  labels: SwingLabelOption[]
  date_range: { start: string | null; end: string | null; days: number }
  scores: SwingScore[]
  significant_count: number
  reason: string | null
  condition_plan: SwingConditionPlan | null
}
