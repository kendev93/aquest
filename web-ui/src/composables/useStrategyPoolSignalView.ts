import { computed, ref, watch, type Ref } from 'vue'

import type {
  StrategyPoolEntry,
  StrategyPoolFollowTarget,
  StrategyPoolHolding,
  StrategyPoolMetrics,
  StrategyPoolSignal,
} from '../types'
import {
  formatHoldingDays,
  symbol,
} from '../utils/strategyPoolStatus'
import { formatNumber, formatPercent } from '../utils/format'

export const SIGNAL_PAGE_SIZE = 10

export type SellPanelItem =
  | { kind: 'rule'; key: string; signal: StrategyPoolSignal }
  // 风控退出行是引擎直接落库的宽松对象，这里顺手把它归一成关注开关需要的市场+代码+名称，
  // 免得模板里再对 unknown 做一遍判断。
  | {
      kind: 'risk'
      key: string
      signal: Record<string, unknown>
      target: StrategyPoolFollowTarget
    }

// 三个信号面板（触发买入 / 触发卖出 / 持仓中）共用的显示排序（2026-10-13 用户需求）：
// 后端返回顺序对跟踪增量结果是按证券代码的，这里允许按行内展示的全区间指标重排。
// 'default' 表示保持返回顺序，只有这一档把特别关注置顶。
export const SIGNAL_SORT_OPTIONS = [
  { value: 'default', label: '默认顺序（关注置顶）' },
  { value: 'win_rate', label: '全区间胜率 高→低' },
  { value: 'payoff_ratio', label: '全区间盈亏比 高→低' },
  { value: 'annual_return', label: '全区间年化收益 高→低' },
] as const
type SignalSortValue = (typeof SIGNAL_SORT_OPTIONS)[number]['value']

interface StrategyPoolSignalViewOptions {
  buySignals: Ref<StrategyPoolSignal[]>
  sellPanelItems: Ref<SellPanelItem[]>
  currentHoldings: Ref<StrategyPoolHolding[]>
  entries: Ref<StrategyPoolEntry[]>
  followKeys: Ref<Set<string>>
}

export function useStrategyPoolSignalView(options: StrategyPoolSignalViewOptions) {
  const signalSort = ref<SignalSortValue>('default')
  const buyPage = ref(1)
  const sellPage = ref(1)
  const holdingsPage = ref(1)

  // 三个信号列表行内的策略质量指标（2026-10-09 用户需求）：全区间胜率 / 盈亏比 /
  // 平均持仓天数，与「全市场标的状态」表格口径一致。从条目表按代码查找——跟踪
  // 路径的信号行与持仓行本身不带指标，这样两条来源的行都能显示。
  const entryQualityMap = computed(() => {
    const map = new Map<string, StrategyPoolMetrics>()
    for (const entry of options.entries.value) {
      const candidate = entry.active_candidate
      const full = candidate?.metrics?.full_period
      if (full) map.set(symbol(entry), full)
    }
    return map
  })

  function qualityText(key: string): string {
    const full = entryQualityMap.value.get(key)
    if (!full) return ''
    const parts: string[] = []
    // 全区间累计收益不可跨标的比较（上市早的累计年份更多），排序按年化；
    // 这里把年化一并写出来，排序结果才在行内看得见。
    if (typeof full.annual_return === 'number' && Number.isFinite(full.annual_return)) {
      parts.push(`全区间年化 ${formatPercent(full.annual_return)}`)
    }
    if (typeof full.win_rate === 'number' && Number.isFinite(full.win_rate)) {
      parts.push(`全区间胜率 ${formatPercent(full.win_rate)}`)
    }
    if (typeof full.payoff_ratio === 'number' && Number.isFinite(full.payoff_ratio)) {
      parts.push(`全区间盈亏比 ${formatNumber(full.payoff_ratio)}`)
    }
    if (typeof full.avg_holding_days === 'number' && Number.isFinite(full.avg_holding_days)) {
      parts.push(`平均持仓 ${formatHoldingDays(full.avg_holding_days)}`)
    }
    return parts.join(' · ')
  }

  function sellItemSymbol(item: SellPanelItem): string {
    return item.kind === 'rule'
      ? item.signal.symbol
      : `${String(item.signal.market ?? '')}${String(item.signal.code ?? '')}`
  }

  // 排序取全区间指标：与行内「全区间胜率 / 盈亏比」文案同一口径，且跟踪增量返回的
  // 信号行本身不带指标，只有条目表补得上。缺全区间指标的行排在末尾。
  function sortSignalRows<T>(rows: T[], keyOf: (row: T) => string): T[] {
    // 关注只在默认排序（按状态优先级）时置顶：选了指标以后还钉在榜首，关注行就会挡住
    // 真正的排序结果，看着像是排序没生效。
    if (signalSort.value === 'default') {
      const pinned: T[] = []
      const rest: T[] = []
      for (const row of rows) {
        (options.followKeys.value.has(keyOf(row)) ? pinned : rest).push(row)
      }
      return [...pinned, ...rest]
    }
    const metric = signalSort.value
    const score = (row: T): number => {
      const value = entryQualityMap.value.get(keyOf(row))?.[metric]
      return typeof value === 'number' && Number.isFinite(value) ? value : Number.NEGATIVE_INFINITY
    }
    return [...rows].sort((left, right) => (
      score(right) - score(left) || keyOf(left).localeCompare(keyOf(right))
    ))
  }

  function pageCount(count: number): number {
    return Math.max(1, Math.ceil(count / SIGNAL_PAGE_SIZE))
  }

  function pageItems<T>(items: T[], page: number): T[] {
    const start = (page - 1) * SIGNAL_PAGE_SIZE
    return items.slice(start, start + SIGNAL_PAGE_SIZE)
  }

  const sortedBuySignals = computed(() => sortSignalRows(options.buySignals.value, (item) => item.symbol))
  const sortedSellPanelItems = computed(() => sortSignalRows(options.sellPanelItems.value, sellItemSymbol))
  const sortedCurrentHoldings = computed(() =>
    sortSignalRows(options.currentHoldings.value, (holding) => symbol(holding)),
  )
  const visibleBuySignals = computed(() => pageItems(sortedBuySignals.value, buyPage.value))
  const visibleSellSignals = computed(() => pageItems(sortedSellPanelItems.value, sellPage.value))
  const visibleCurrentHoldings = computed(() =>
    pageItems(sortedCurrentHoldings.value, holdingsPage.value),
  )
  const buyPageCount = computed(() => pageCount(options.buySignals.value.length))
  const sellPageCount = computed(() => pageCount(options.sellPanelItems.value.length))
  const holdingsPageCount = computed(() => pageCount(sortedCurrentHoldings.value.length))

  watch([options.buySignals, options.sellPanelItems, options.currentHoldings], () => {
    buyPage.value = 1
    sellPage.value = 1
    holdingsPage.value = 1
  })
  watch(signalSort, () => {
    buyPage.value = 1
    sellPage.value = 1
    // 持仓面板也共用这个排序：不重置的话第 2 页之后会停在旧页码，甚至显示空表。
    holdingsPage.value = 1
  })

  return {
    SIGNAL_PAGE_SIZE,
    SIGNAL_SORT_OPTIONS,
    signalSort,
    buyPage,
    sellPage,
    holdingsPage,
    visibleBuySignals,
    visibleSellSignals,
    visibleCurrentHoldings,
    buyPageCount,
    sellPageCount,
    holdingsPageCount,
    qualityText,
  }
}
