/** 股票组：一组被留下来观察的股票，以及它们入组之后的表现。 */

export interface StockGroupMember {
  market: string
  code: string
  name?: string | null
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
