/** factor 相关的前端类型。 */

import type { InstrumentBoard, InstrumentType, Universe } from './common'


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


export interface AttributionFactor {
  value_id: string
  display_name: string
  win_mean: number | null
  loss_mean: number | null
  /** 胜组均值 − 败组均值。**差值不是倍数**：指标值可以取负，比值会读反方向。 */
  difference: number | null
  /** 差值除以合并标准差，只用来把不同量纲的指标排个先后。 */
  standardized: number | null
  win_count: number
  loss_count: number
  /** 入场信号日取不到值的成交笔数：丢弃，绝不当 0 填进来。 */
  missing: number
  p_value: number | null
  adjusted_p_value: number | null
  /** null = 没判（样本太小或没有噪声门槛），和 false（判了但不显著）不是一回事。 */
  significant: boolean | null
  note: string | null
}


export interface AttributionSamples {
  trades: number
  wins: number
  losses: number
  /** 期末还持有的买入：没有已实现收益，不算胜单。 */
  open_positions: number
  orphan_sells: number
  rejected: number
  unparsable: number
  signal_days: number
  stocks: number
  holding_days: number | null
  win_holding_days: number | null
  loss_holding_days: number | null
  missing_stocks: string[]
}


export interface FactorAttributionReport {
  source: {
    history_id: string | null
    backtest_type: string | null
    finished_at: string | null
    date_range: { start: string | null; end: string | null }
  }
  samples: AttributionSamples
  value_count: number
  significant_count: number
  permutations: number
  block_size_days: number
  block_size_trades: number | null
  alpha: number
  factors: AttributionFactor[]
  notes: string[]
  /** 口径边界，页面原文照抄展示。 */
  boundaries: string[]
  reason: string | null
}


export interface FactorAttributionJobState {
  job_id: string
  status: 'queued' | 'running' | 'completed' | 'failed'
  progress: number
  error: string | null
  result: FactorAttributionReport | null
}


export interface FactorAttributionPayload {
  source_history_id?: string
  value_ids?: string[] | null
  include_custom?: boolean
  min_group_trades?: number
  permutations?: number
  block_size?: number
  alpha?: number
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
