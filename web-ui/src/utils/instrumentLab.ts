/** 标的检验台的展示逻辑。
 *
 * 这里全是「怎么把读数说清楚」的函数：命中/没命中、显著/没显著、差值是多少个百
 * 分点。抽出来的原因和 historyCompare 一样——页面里塞满三元表达式之后，没人能
 * 看出「不显著」这条口径有没有真的生效。
 */

import type {
  InstrumentLabCheckItem,
  InstrumentLabLibraryItem,
  InstrumentLabSeries,
} from '../api/instrumentLab'
import type { ChartPoint } from './chart'

export const PLACEHOLDER = '—'

export function formatPercent(value: number | null, digits = 2): string {
  if (value === null || !Number.isFinite(value)) return PLACEHOLDER
  const percent = value * 100
  return `${percent >= 0 ? '+' : ''}${percent.toFixed(digits)}%`
}

/** 收益差用「百分点」说，不用比值：收益能取负，两个负数相除没有意义。 */
export function formatDifferencePoints(value: number | null, digits = 2): string {
  if (value === null || !Number.isFinite(value)) return PLACEHOLDER
  const points = value * 100
  return `${points >= 0 ? '+' : ''}${points.toFixed(digits)} 个百分点`
}

export function formatPValue(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return PLACEHOLDER
  if (value < 0.001) return '< 0.001'
  return value.toFixed(3)
}

export function percentileText(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return PLACEHOLDER
  return `自身历史 P${Math.round(value)}`
}

export function percentileTone(value: number | null): 'high' | 'low' | 'mid' {
  if (value === null || !Number.isFinite(value)) return 'mid'
  if (value >= 80) return 'high'
  if (value <= 20) return 'low'
  return 'mid'
}

export function differenceTone(value: number | null): 'up' | 'down' | 'flat' {
  if (value === null || !Number.isFinite(value) || Math.abs(value) < 1e-9) return 'flat'
  return value > 0 ? 'up' : 'down'
}

/** 显著性只说三件事：显著、和随机挑几天没区别、没做判定。绝不使用「有效」。 */
export function significanceLabel(item: InstrumentLabCheckItem): string {
  if (item.significant === true) return '显著'
  if (item.significant === false) return '和随机挑几天没有区别'
  if (item.hit_count === 0) return '没有命中'
  return '未做判定'
}

export function significanceTone(item: InstrumentLabCheckItem): 'up' | 'muted' | 'warn' {
  if (item.significant === true) return 'up'
  if (item.significant === false) return 'muted'
  return 'warn'
}

export function sideLabel(side: string | null): string {
  if (side === 'buy') return '买入侧'
  if (side === 'sell') return '卖出侧'
  return PLACEHOLDER
}

export function libraryOptionLabel(item: InstrumentLabLibraryItem): string {
  const parts = [item.label]
  if (item.lift !== null) parts.push(`提升 ${item.lift.toFixed(2)} 倍`)
  if (item.support_count !== null) parts.push(`命中 ${item.support_count} 次`)
  return parts.join(' · ')
}

export function seriesPoints(series: InstrumentLabSeries[]): ChartPoint[][] {
  return series.map((item) =>
    item.values.map((value, index) => ({ x: index, y: value ?? Number.NaN })),
  )
}

export interface TradeMarker {
  date: string
  side: 'buy' | 'sell'
}

/** 成交记录 → K 线标记。只保留买卖方向，价格由 K 线自己给出。 */
export function buildTradeMarkers(trades: readonly Record<string, unknown>[]): TradeMarker[] {
  const markers: TradeMarker[] = []
  for (const trade of trades) {
    const date = trade.datetime ?? trade.date
    const direction = trade.direction
    if (typeof date !== 'string' || !date) continue
    const day = date.slice(0, 10)
    if (direction === 'buy' || direction === 'sell') {
      markers.push({ date: day, side: direction })
    }
  }
  return markers
}

export function summaryText(item: InstrumentLabCheckItem): string {
  if (item.hit_count === 0) return '该标的上从未命中这个条件'
  const parts = [`命中 ${item.hit_count} / ${item.sample_days} 个交易日`]
  if (item.difference !== null) {
    parts.push(`命中后收益比全样本高 ${formatDifferencePoints(item.difference)}`)
  }
  if (item.p_value !== null) {
    parts.push(
      item.adjusted_p_value !== null && item.adjusted_p_value !== item.p_value
        ? `置换 p=${formatPValue(item.p_value)}（校正后 ${formatPValue(item.adjusted_p_value)}）`
        : `置换 p=${formatPValue(item.p_value)}`,
    )
  }
  return parts.join('，')
}

export function selectedValueLabel(series: InstrumentLabSeries): string {
  const last =
    series.last_value === null || !Number.isFinite(series.last_value)
      ? PLACEHOLDER
      : series.last_value.toFixed(3)
  return `${series.label} ${last} · ${percentileText(series.percentile)}`
}
