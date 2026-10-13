/** portfolio 相关的前端类型。 */

import type { BacktestExecution, BacktestPositionMode, CombineMode, DateRangeMode, InstrumentBoard, InstrumentType, PortfolioCompareOperator, PortfolioRankOrder, PortfolioRebalanceFrequency, PortfolioSellValueOperator, PortfolioUniverse, Universe } from './common'

import type { RuleGroupPayload } from './screen'


export interface PortfolioBacktestPayload {
  universe: PortfolioUniverse
  single_security_mode?: boolean
  date_mode?: DateRangeMode
  universe_file?: string | null
  codes?: string[] | null
  instrument_types?: InstrumentType[]
  boards?: InstrumentBoard[]
  selected_signals: string[]
  combine_mode: CombineMode
  minimum_matches: number | null
  buy_rule?: RuleGroupPayload | null
  ranking_value: string | null
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
  max_hold_days?: number | null
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
  /** 排除 ST / *ST / 退市标的（按当前股票名称判定）。 */
  exclude_st?: boolean
}


export interface PortfolioBacktestJobState {
  job_id: string
  status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'
  progress: number
  total_candidates: number
  total_scanned: number
  errors: number
  error: string | null
  result: PortfolioBacktestResult | null
  cancel_requested?: boolean
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
  universe: PortfolioUniverse
  single_security_mode?: boolean
  total_candidates: number
  processed: number
  skipped: number
  errors: number
  bars: number
  start_date: string
  end_date: string
  max_positions: number
  ranking_value: string | null
  rank_order: PortfolioRankOrder | null
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
  /** 开启排除后被当作 ST / *ST / 退市剔除的代码。 */
  excluded_st_codes?: string[]
  /** 开启排除后被剔除的标的数量（全市场回测时不逐个展示代码也能看出过滤生效）。 */
  excluded_st_count?: number
  /** 基准对照（标的池等权买入持有）。窗口太短或池内无可用标的时为 null。 */
  benchmark?: PortfolioBenchmark | null
  /** 价格口径。当前组合回测使用后复权日线（hfq）；旧历史记录可能仍是 raw。 */
  price_basis?: string
  /** 回测结果的固定模型边界；老历史记录可能没有该字段。 */
  limitations?: string[]
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


/** 追踪里的一个交易日。上线日那一行记录新观察账户的初始资金和空仓状态。 */
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


/** 一笔成交。pnl 只在卖出时有意义（买入侧是 0）。 */
export interface TrackingTradeRow {
  trade_date: number
  signal_date: number | null
  phase: 'pre' | 'post'
  direction: 'BUY' | 'SELL' | string
  market: string
  code: string
  name: string
  size: number
  price: number
  pnl: number
  reason: string
  commission: number | null
  stamp_tax: number | null
  slippage: number | null
  cost_basis: number | null
}

export interface TrackingTradePage {
  rows: TrackingTradeRow[]
  total: number
  limit: number
  offset: number
}

export interface TrackingSignalReading {
  id: string
  matched: boolean
}

export interface TrackingConditionReading {
  left_value: string
  right_value: string | null
  operator: string
  current_left_value: number | null
  current_right_value: number | null
  threshold: number | null
  previous_left_value?: number | null
  previous_right_value?: number | null
  required: boolean
  matched: boolean
}

export interface TrackingRuleReading {
  matched: boolean
  signals: TrackingSignalReading[]
  conditions: TrackingConditionReading[]
}


/** 一段的成交明细。rows 是倒序（最近在前），total 是真实笔数。 */
export interface TrackingTradeSection {
  rows: TrackingTradeRow[]
  total: number
  truncated: boolean
}


/** 分上线前后的成交明细。列表接口不带它（见路由注释），展开时才单独取。 */
export interface TrackingTrades {
  before: TrackingTradeSection
  after: TrackingTradeSection
  limit: number
}


/** 下一交易日计划里的一笔。买入侧只有估算值——明日开盘价今天不存在。 */
export interface TrackingPlanOrder {
  market?: string
  code: string
  name?: string
  /** 卖出：持仓实际股数。 */
  size?: number
  /** 买入：按今日收盘价估算的股数。 */
  estimated_size?: number
  reference_price?: number
  estimated_proceeds?: number
  estimated_cost?: number
  reason?: string
  signal_date?: string
}


export interface TrackingHolding {
  market: string
  code: string
  name: string
  size: number
  reference_price: number
  market_value: number
  entry_price: number
  entry_date: string
  unrealized_pnl_pct: number
}

export interface TrackingCurrentHolding {
  market: string
  code: string
  name: string
  size: number
  entry_price: number
  entry_date: string
  close: number
  latest_price: number
  market_value: number
  unrealized_pnl: number
  unrealized_pnl_pct: number
  position_weight: number
  indicator_as_of?: string | null
  sell_signal: TrackingRuleReading
}

export interface TrackingCurrentState {
  as_of: string
  available: boolean
  equity: number
  total_asset: number
  cash: number
  position_value: number
  positions_count: number
  holdings: TrackingCurrentHolding[]
}

export interface TrackingCandidate {
  rank: number
  market: string
  code: string
  name: string
  score: number
  rank_score: number
  ranking_value: string
  already_held: boolean
  buy_signal: { matched: boolean; signals: TrackingSignalReading[] }
  buy_conditions: TrackingConditionReading[]
  fitness_score?: number
  fitness_trades?: number
  fitness_note?: string
}

export interface TrackingTodayCandidates {
  as_of: string | null
  ranking_value: string
  total: number
  rows: TrackingCandidate[]
}


/**
 * 下一交易日计划：截至最近一个交易日收盘时排定的待执行单。
 * 数量一律按今日收盘价估算，notes 里必须写清这一点。
 */
export interface TrackingNextPlan {
  as_of: string
  cash: number
  estimated_cash_after_sells: number
  /** 月 / 周频策略在不是调仓日时为 false，此时 buys 是空的。 */
  rebalance_due: boolean
  holdings: TrackingHolding[]
  sells: TrackingPlanOrder[]
  buys: TrackingPlanOrder[]
  notes: string[]
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
  /**
   * 最小跟踪记录长度：按观察段目前的表现，还要多少个交易日才能说它显著为正。
   * null 表示算不出来（观察段太短、零波动、或平均收益不为正），不是"不需要"。
   */
  min_track_record_length: number | null
  /** 列表接口为 null（几百笔 × 策略数会撑大响应），展开时走详情接口拿。 */
  trades: TrackingTrades | null
  /** 下一交易日计划。下线后是 null——过期的单子挂在页面上比没有更糟。 */
  next_plan: TrackingNextPlan | null
  /** 最近一次成功重算保存的账户快照；旧版本可能没有。 */
  current_state: TrackingCurrentState | null
  /** 每个最新行情日的信息型候选，和下一交易日计划分开。 */
  today_candidates: TrackingTodayCandidates | null
}

/** 组合回测页表单的完整状态（reactive 对象的形状）。 */
export interface PortfolioFormState {
  mode: 'preset' | 'custom'
  buyRule: RuleGroupPayload
  universe: PortfolioUniverse
  universeFile: string
  codesText: string
  instrumentTypes: InstrumentType[]
  boards: InstrumentBoard[]
  rankingValue: string
  rankOrder: PortfolioRankOrder
  maxPositions: number
  rebalanceFrequency: PortfolioRebalanceFrequency
  execution: BacktestExecution
  formulaText: string
  formulaParameters: Record<string, number>
  sellRule: RuleGroupPayload
  stopLossEnabled: boolean
  stopLossMode: 'pct' | 'atr'
  stopLossPct: number
  stopLossAtrMultiplier: number
  stopLossCooldownDays: number
  takeProfitEnabled: boolean
  takeProfitMode: 'pct' | 'atr'
  takeProfitPct: number
  takeProfitAtrMultiplier: number
  takeProfitCooldownDays: number
  trailingStopEnabled: boolean
  trailingStopMode: 'pct' | 'atr'
  trailingStopPct: number
  trailingStopAtrMultiplier: number
  trailingStopCooldownDays: number
  maxHoldDaysEnabled: boolean
  maxHoldDays: number
  atrPeriod: number
  dateMode: DateRangeMode
  startDate: string
  endDate: string
  initialCash: number
  commission: number
  minCommission: number
  stampTax: number
  slippage: number
  fitnessRankWeight: number
  fitnessMinTrades: number
  fitnessMaxDrawdown: number
  positionMode: BacktestPositionMode
  fixedSize: number
  excludeSt: boolean
}

/** 应用了策略发现候选后留在页面上的复现标注；与验证段区间一致时才带指标。 */
export interface AppliedDiscoveryCandidate {
  candidate_hash: string
  data_fingerprint: string | null
  validation_metrics: Record<string, number | string | boolean | null> | null
}

/** 重跑回测后，验证「榜单上的候选是否真的复现」的三项比对结论。 */
export interface PortfolioReplayComparison {
  candidateHash: string
  tradeRecords: '一致' | '不一致' | '未提供'
  performance: '一致' | '不一致' | '未提供'
  dataFingerprint: '一致' | '不一致' | '未提供'
}
