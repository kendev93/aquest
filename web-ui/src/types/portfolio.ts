/** portfolio 相关的前端类型。 */

import type { BacktestExecution, BacktestPositionMode, CombineMode, DateRangeMode, InstrumentBoard, InstrumentType, PortfolioCompareOperator, PortfolioRankOrder, PortfolioRebalanceFrequency, PortfolioSellValueOperator, PortfolioUniverse, Universe } from './common'

import type { RuleGroupPayload } from './screen'


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
  /**
   * 最小跟踪记录长度：按观察段目前的表现，还要多少个交易日才能说它显著为正。
   * null 表示算不出来（观察段太短、零波动、或平均收益不为正），不是"不需要"。
   */
  min_track_record_length: number | null
}
