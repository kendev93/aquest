import type {
  HoldingRiskDisplay,
  StrategyPoolCandidate,
  StrategyPoolEntry,
  StrategyPoolObjective,
  StrategyPoolSignal,
} from '../types'

import { formatNumber } from './format'

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

/** 列头点击在 高→低 / 低→高 / 取消排序（回到 'status' 档）之间循环。 */
export type StrategyPoolSortDirection = 'asc' | 'desc'

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
  return resolveStatus(entry, states.find((item) => item.symbol === symbol(entry)))
}

function resolveStatus(entry: StrategyPoolEntry, row: StrategyPoolSignal | undefined): string {
  const candidate = activeReplayableCandidate(entry)
  if (!candidate) return entry.status === 'insufficient_history' ? 'insufficient_history' : 'no_strategy'
  if (!row || row.status === 'no_strategy' || row.candidate_id !== candidate.candidate_id) {
    return 'signal_stale'
  }
  if (row.status === 'held') return 'flat'
  return row.status ?? 'signal_stale'
}

/**
 * 排序用的状态查找表：先把当天状态按证券代码建一次索引，再记住每个标的算出来的状态。
 *
 * 排序里每个标的要被比较十几次，逐个查数组就是 O(n log n × m)：3350 行 × 3350 条状态
 * 实测一次 1.4 秒，而全市场页每拿一次列表都要排一遍，浏览器直接卡到弹「页面无响应」。
 * 同一份状态里重复的证券代码取第一条，和 `states.find` 的行为一致。
 */
function statusLookup(states: StrategyPoolSignal[]): (entry: StrategyPoolEntry) => string {
  const bySymbol = new Map<string, StrategyPoolSignal>()
  for (const item of states) {
    if (!bySymbol.has(item.symbol)) bySymbol.set(item.symbol, item)
  }
  const cache = new Map<string, string>()
  return (entry) => {
    const key = symbol(entry)
    const cached = cache.get(key)
    if (cached !== undefined) return cached
    const resolved = resolveStatus(entry, bySymbol.get(key))
    cache.set(key, resolved)
    return resolved
  }
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

export function metricRawValue(
  entry: StrategyPoolEntry,
  key: StrategyPoolMetricColumn,
): number | null {
  if (key === 'created_return') return entry.created_return ?? null
  if (key === 'benchmark_return') return benchmarkReturn(entry)
  // 列表只给 active_candidate：后端已经把「手工覆盖优先于自动最优」算成这一个字段，
  // 以前的 `?? automatic_candidates[目标]` 兜底永远走不到（active 为空时它也必为空），
  // 而那一整块占 11.5 MB 响应的 31.3%，所以接口不再回它。
  const candidate = entry.active_candidate
  // 表格展示统一用全区间实际回放口径；验证段指标只在策略选择时使用。
  const metricKey = key === 'strategy_return' ? 'total_return' : key
  const value = candidate?.metrics?.full_period?.[metricKey]
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

export function metricValue(
  entry: StrategyPoolEntry,
  key: StrategyPoolMetricColumn,
): number {
  return metricRawValue(entry, key) ?? Number.NEGATIVE_INFINITY
}

/** 全区间指标回放到的日期；旧摘要没有这一块时返回 null，页面继续显示研究口径。 */
export function fullPeriodAsOf(entry: StrategyPoolEntry): string | null {
  const value = entry.full_period_as_of_date
  return typeof value === 'string' && value ? value : null
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
  const statusOf = statusLookup(states)
  return [...rows].sort((a, b) => {
    const order = statusOrder(statusOf(a), entryIsHeld(a, heldKeys))
      - statusOrder(statusOf(b), entryIsHeld(b, heldKeys))
    return order
      || metricValue(b, objective ?? 'annual_return')
        - metricValue(a, objective ?? 'annual_return')
      || symbol(a).localeCompare(symbol(b))
  })
}

/**
 * 表格按指标列排序：方向可升可降，同值时仍按状态优先级、证券代码兜底。
 * 取不到值的标的（无候选、创建至今待初始化或更新失败）无论升降都排在末尾——
 * 否则升序会把几千只没有策略的票顶到第一页，等于没排。
 */
export function sortPoolEntriesByMetric(
  rows: StrategyPoolEntry[],
  metric: StrategyPoolMetricColumn,
  direction: StrategyPoolSortDirection,
  states: StrategyPoolSignal[],
  heldKeys: Set<string>,
  objective: StrategyPoolObjective | null,
): StrategyPoolEntry[] {
  const statusOf = statusLookup(states)
  const byStatusThenSymbol = (a: StrategyPoolEntry, b: StrategyPoolEntry): number =>
    statusOrder(statusOf(a), entryIsHeld(a, heldKeys))
      - statusOrder(statusOf(b), entryIsHeld(b, heldKeys))
      || symbol(a).localeCompare(symbol(b))
  const sign = direction === 'asc' ? 1 : -1

  return [...rows].sort((a, b) => {
    const valueA = metricRawValue(a, metric)
    const valueB = metricRawValue(b, metric)
    if (valueA === null || valueB === null) {
      if (valueA === null && valueB === null) return byStatusThenSymbol(a, b)
      return valueA === null ? 1 : -1
    }
    return sign * (valueA - valueB) || byStatusThenSymbol(a, b)
  })
}

/** 持仓生效中的风控线，拼成「止损 9.00 · 移动止盈 11.40（截至 2026-10-16 收盘）」。 */
export function holdingRiskLevelText(holding: HoldingRiskDisplay): string {
  const lines: Array<[string, number | null | undefined]> = [
    ['止损', holding.stop_loss_price],
    ['移动止盈', holding.trailing_stop_price],
    ['止盈', holding.take_profit_price],
  ]
  const active = lines
    .filter(([, value]) => typeof value === 'number' && Number.isFinite(value))
    .map(([label, value]) => `${label} ${formatNumber(value, 2)}`)
  if (!active.length) return '未设置风控线'
  return `${active.join(' · ')}${
    holding.risk_level_as_of ? `（截至 ${holding.risk_level_as_of} 收盘）` : ''
  }`
}

/**
 * 持有到期提示。到期日只有行情已经覆盖到那一天才给得出，否则只报还剩几个交易日，
 * 免得显示一个没有依据的日期。
 */
export function holdingExpiryText(holding: HoldingRiskDisplay): string {
  if (typeof holding.max_hold_days !== 'number' || !Number.isFinite(holding.max_hold_days)) return ''
  const left = typeof holding.max_hold_days_left === 'number' ? holding.max_hold_days_left : null
  if (left !== null && left <= 0) return '今日收盘满持有期限 · 下一交易日开盘卖出'
  if (holding.max_hold_expiry_date && left !== null) {
    return `${holding.max_hold_expiry_date} 到期 · 还剩 ${left} 个交易日`
  }
  return left === null ? '' : `还剩 ${left} 个交易日到期`
}

/** 风控条：生效中的三档线 + 判定日期 + 持有到期，用一行说清明天会按哪条线动。 */
export function holdingRiskSummary(holding: HoldingRiskDisplay): string {
  return [holdingRiskLevelText(holding), holdingExpiryText(holding)].filter(Boolean).join(' · ')
}

/**
 * 跟踪快照里该标的当前持仓行的风控展示；没有持仓或快照缺失时返回 null。
 *
 * 条目接口把每只标的的当前跟踪行挂在 `tracking` 上，`summary.holding` 里带着引擎
 * 算出的风控线，所以「全市场标的状态」与「特别关注」两张表不用另外请求就能显示。
 */
export function entryHoldingRisk(entry: StrategyPoolEntry): HoldingRiskDisplay | null {
  const summary = (entry.tracking as { summary?: unknown } | null | undefined)?.summary
  const holding = (summary as { holding?: unknown } | null | undefined)?.holding
  const row = Array.isArray(holding) ? holding[0] : null
  return row && typeof row === 'object' ? (row as HoldingRiskDisplay) : null
}

/**
 * 该标的的最新收盘价（最后一根已完成日线，不复权）和它自己的日期。
 *
 * 价是从行情直接取的，不是跟踪摘要里的字段：空仓、当天没有信号、甚至还没有跟踪版本的行
 * 都要有同一个数，不然只有持仓中的能看见价，整列就没法横向看。日期跟着一起给——风控三档
 * 线自己带着判定日期，两个日期不一致就说明跟踪落后于行情（停牌或落库失败），这里不粉饰。
 */
export function entryLatestClose(
  entry: StrategyPoolEntry,
): { price: number; asOf: string | null } | null {
  const price = entry.latest_close
  if (typeof price !== 'number' || !Number.isFinite(price)) return null
  return { price, asOf: entry.latest_close_date ?? null }
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
