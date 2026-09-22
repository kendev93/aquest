/** condition 相关的前端类型。 */

import type { BacktestExecution, BacktestPositionMode, DateRangeMode, InstrumentBoard, InstrumentType } from './common'

import type { RuleGroupPayload, ScreenCondition } from './screen'


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
