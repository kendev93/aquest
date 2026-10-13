import type {
  StrategyPoolCandidate,
  StrategyPoolEntry,
  StrategyPoolObjective,
  StrategyPoolSignal,
} from '../types'

/** 「全市场标的状态」表格的排序列；'status' 表示按状态优先级排。 */
export type StrategyPoolSortColumn =
  | 'status'
  | 'strategy_return'
  | 'annual_return'
  | 'sharpe'
  | 'win_rate'
  | 'payoff_ratio'
  | 'max_drawdown'
  | 'avg_holding_days'
  | 'benchmark_return'
  | 'created_return'

export type StrategyPoolMetricColumn = Exclude<StrategyPoolSortColumn, 'status'>

export interface PoolJobStateContext {
  queuedSymbol: string
  busy: boolean
  busyIntent: string
}

export function symbol(item: Pick<StrategyPoolEntry, 'market' | 'code'>): string {
  return `${item.market}${item.code}`
}

export function activeReplayableCandidate(entry: StrategyPoolEntry): StrategyPoolCandidate | null {
  const candidate = entry.active_candidate
  return candidate
    && candidate.replayable !== false
    && candidate.buy_rule
    && candidate.sell_rule
    ? candidate
    : null
}

export function entryStatus(entry: StrategyPoolEntry, states: StrategyPoolSignal[]): string {
  const row = states.find((item) => item.symbol === symbol(entry))
  const candidate = activeReplayableCandidate(entry)
  if (!candidate) return entry.status === 'insufficient_history' ? 'insufficient_history' : 'no_strategy'
  if (!row || row.status === 'no_strategy' || row.candidate_id !== candidate.candidate_id) {
    return 'signal_stale'
  }
  if (row.status === 'held') return 'flat'
  return row.status ?? 'signal_stale'
}

export function entryIsHeld(entry: StrategyPoolEntry, heldKeys: Set<string>): boolean {
  return heldKeys.has(symbol(entry))
}

// 默认排序档位：触发买入 → 触发卖出 → 持仓中 → 今日无买卖信号 → 待刷新 / 无策略 / 其他。
// 持仓中不带买卖信号时状态为 flat，需要靠 held 与「今日无买卖信号」区分开。
export function statusOrder(value: string, held = false): number {
  if (value === 'buy' || value === 'buy_sell') return 0
  if (value === 'sell') return 1
  // 有买卖信号的持仓标的归入买卖档；其余持仓标的统一排在「持仓中」。
  if (held && value !== 'no_strategy' && value !== 'insufficient_history') return 2
  if (value === 'flat') return 3
  return { signal_stale: 4, no_strategy: 5 }[value] ?? 6
}

export function statusLabel(value: string): string {
  return {
    buy: '触发买入',
    buy_sell: '买卖均触发',
    sell: '触发卖出',
    held: '持仓中',
    flat: '今日无买卖信号',
    signal_stale: '策略可用 · 信号待刷新',
    no_strategy: '无可用策略',
    insufficient_history: '历史样本不足',
    error: '扫描失败',
  }[value] ?? value
}

export function metricValue(
  entry: StrategyPoolEntry,
  key: StrategyPoolMetricColumn,
  objective: StrategyPoolObjective | null,
): number {
  if (key === 'created_return') return entry.created_return ?? Number.NEGATIVE_INFINITY
  if (key === 'benchmark_return') {
    return benchmarkReturn(entry) ?? Number.NEGATIVE_INFINITY
  }
  const candidate = entry.active_candidate
    ?? (objective ? entry.automatic_candidates[objective] : null)
  // 表格展示统一用全区间实际回放口径；验证段指标只在策略选择时使用。
  const metricKey = key === 'strategy_return' ? 'total_return' : key
  const value = candidate?.metrics?.full_period?.[metricKey]
  return typeof value === 'number' && Number.isFinite(value) ? value : Number.NEGATIVE_INFINITY
}

export function benchmarkReturn(entry: StrategyPoolEntry): number | null {
  const value = entry.benchmark?.full_period?.total_return
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

export function tradeCount(
  candidate: StrategyPoolCandidate | null | undefined,
  phase: 'validation' | 'holdout',
): number | null {
  return candidate?.metrics?.[phase]?.closed_trades
    ?? candidate?.metrics?.[phase]?.total_trades
    ?? null
}

export function formatHoldingDays(value: unknown): string {
  return typeof value === 'number' && Number.isFinite(value) ? `${value.toFixed(1)} 天` : '—'
}

export function objectiveLabel(value: StrategyPoolObjective | null): string {
  return value === 'sharpe' ? '夏普' : '年化收益'
}

/**
 * 表格默认排序：状态优先级优先，同档再按所选目标的指标高低、证券代码。
 * 「特别关注」面板与主表的 status 档共用这条比较链。
 */
export function sortPoolEntriesByStatus(
  rows: StrategyPoolEntry[],
  states: StrategyPoolSignal[],
  heldKeys: Set<string>,
  objective: StrategyPoolObjective | null,
): StrategyPoolEntry[] {
  return [...rows].sort((a, b) => {
    const order = statusOrder(entryStatus(a, states), entryIsHeld(a, heldKeys))
      - statusOrder(entryStatus(b, states), entryIsHeld(b, heldKeys))
    return order
      || metricValue(b, objective ?? 'annual_return', objective)
        - metricValue(a, objective ?? 'annual_return', objective)
      || symbol(a).localeCompare(symbol(b))
  })
}

export function rerunEntryDisabled(context: PoolJobStateContext): boolean {
  return Boolean(context.queuedSymbol) || (context.busy && context.busyIntent !== 'signals')
}

export function rerunEntryTitle(entry: StrategyPoolEntry, context: PoolJobStateContext): string {
  if (context.queuedSymbol) {
    return context.queuedSymbol === symbol(entry) ? '该标的已排队重跑' : '已有逐股重跑在排队中'
  }
  if (context.busy && context.busyIntent === 'signals') return '点击后排在当天信号扫描之后重跑'
  if (context.busy) return '当前全市场研究或组合回测结束后可重跑'
  return `重跑 ${entry.market} ${entry.code}`
}
