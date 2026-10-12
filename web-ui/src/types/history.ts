/** history 相关的前端类型。 */


export type BacktestHistoryType =
  | 'single_stock'
  | 'portfolio'
  | 'strategy_fitness'
  | 'condition_research'
  | 'swing_research'
  | 'individual_research'
  | 'factor_test'
  | 'factor_attribution'


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
