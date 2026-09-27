/** swing 相关的前端类型。 */

import type { RankOrder } from './condition'

import type { RuleGroupPayload, ScreenCondition } from './screen'


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
  ic_mean: number | null
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
