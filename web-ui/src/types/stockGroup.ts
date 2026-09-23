/** 股票组：一组被留下来观察的股票，以及它们入组之后的表现。 */

export interface StockGroupMember {
  market: string
  code: string
  name?: string | null
}

/** 存组那一刻的筛选条件快照。存快照而不是回指历史：历史只留最近 20 条。 */
export interface StockGroupCriteria {
  group_id: string
  /** 一句人话：折叠态就看这一句。 */
  summary: string
  /** 可复现的输入（公式/信号/参数），结构由产生它的那一页决定。 */
  payload: Record<string, unknown> | null
  created_at: string
  updated_at: string
}

export interface StockGroupSummary {
  group_id: string
  name: string
  note: string
  /** 来源：手动勾选或从选股结果整批存的。 */
  source: string
  source_label: string
  source_history_id: string | null
  created_at: string
  updated_at: string
  item_count: number
  /** 没有筛选条件的组（手建的）是 null，不是空对象。 */
  criteria: StockGroupCriteria | null
}

/**
 * 折叠态的「近 N 个交易日」收益。
 *
 * 和 ``StockGroupPerformance``（入组至今）**不同源**：那边每组起点不同、组之间
 * 不可比；这边所有组共用同一窗口，可以直接横向比。UI 必须写明是哪个。
 */
export interface StockGroupWindowReturn {
  /** 窗口长度（交易日）。 */
  window: number
  /** 窗口里实际有几个交易日。不足窗口长度时数字照给，complete 会是 false。 */
  trading_days: number
  group_return: number | null
  market_return: number | null
  /** 组减全市场的差值（百分点），不是倍数。 */
  excess_return: number | null
  /** 真正计入等权曲线的只数。 */
  counted: number
  /** 数据是否够填满整个窗口。 */
  complete: boolean
}

export interface StockGroupsSummary {
  /** 行情最后一天；没有数据时是 null。 */
  as_of: string | null
  windows: number[]
  /** window 数字 → 中文标签（近 1 月、近 3 月…）。 */
  labels: Record<string, string>
  groups: { group_id: string; windows: StockGroupWindowReturn[] }[]
}

export interface StockGroup extends StockGroupSummary {
  items: StockGroupMember[]
}

/** 一条等权买入持有净值曲线。dates 是 YYYYMMDD，totals 是净值。 */
export interface BasketCurve {
  label: string
  instrument_count: number
  dates: number[]
  totals: number[]
  performance: Record<string, number | null>
}

export interface MemberSpread {
  counted: number
  advanced: number
  declined: number
  median_return: number | null
  best: { market: string; code: string; name: string | null; return: number | null } | null
  worst: { market: string; code: string; name: string | null; return: number | null } | null
}

export interface StockGroupPerformance {
  group_id: string
  start_date: string | null
  end_date: string | null
  trading_days: number
  group_curve: BasketCurve | null
  market_curve: BasketCurve | null
  /** 组相对全市场的超额，是差值（百分点）不是倍数。 */
  excess_return: number | null
  spread: MemberSpread | null
  /** 入组首日没有收盘价、因此没进曲线的标的。 */
  excluded: StockGroupMember[]
  truncated: boolean
  notes: string[]
}
