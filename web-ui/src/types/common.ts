/** common 相关的前端类型。 */

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

export type MarketChartPeriod = 'daily' | 'monthly' | 'yearly'
