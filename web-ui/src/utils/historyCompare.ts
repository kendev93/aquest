/** 对比页的纯展示逻辑。
 *
 * 从页面里抽出来有两个原因：一是这些换算（口径判断、格式化、坐标轴）值得单独
 * 测；二是页面组件一旦变大，覆盖率工具的 sourcemap 映射会失真，把已经跑过的
 * 代码算成没跑过——逻辑放在这里，覆盖率才是对得上的。
 */

import type {
  HistoryCompareItem,
  HistoryCompareMetric,
} from '../api/historyCompare'
import type { ChartPoint } from './chart'

export const SERIES_COLORS = ['#4f8cff', '#f2994a', '#27ae60', '#bb6bd9', '#eb5757']

export const EQUITY_KIND = 'equity'

export function isEquityCurve(kind: string | null | undefined): boolean {
  return kind === EQUITY_KIND
}

/** 日期轴：所有曲线共用的并集日历，长度不同的曲线才能在共同日期上对齐。 */
export function buildDateAxis(items: readonly HistoryCompareItem[]): Map<string, number> {
  const keys = new Set<string>()
  for (const item of items) {
    for (const point of item.curve?.points ?? []) keys.add(point.date)
  }
  return new Map([...keys].sort().map((date, index) => [date, index]))
}

export function buildSeries(
  items: readonly HistoryCompareItem[],
  options: { xAxis: 'date' | 'index'; isEquity: boolean; axis?: Map<string, number> },
): ChartPoint[][] {
  const axis = options.axis ?? buildDateAxis(items)
  return items.map((item) =>
    (item.curve?.points ?? []).map((point) => ({
      x: options.xAxis === 'date' ? (axis.get(point.date) ?? point.index) : point.index,
      y: options.isEquity ? (point.value - 1) * 100 : point.value,
    })),
  )
}

export function metricCell(
  item: HistoryCompareItem,
  key: string,
): HistoryCompareMetric | undefined {
  return item.metrics.find((metric) => metric.key === key)
}

export function metricTitle(item: HistoryCompareItem, key: string): string {
  const metric = metricCell(item, key)
  if (!metric) return ''
  if (metric.value === null) return metric.note ?? '这一口径该类型不产出'
  return metric.note ? `${metric.source}（${metric.note}）` : metric.source
}

export function formatMetricValue(metric: HistoryCompareMetric | undefined): string {
  if (!metric || metric.value === null) return '—'
  if (metric.unit === 'percent') return `${(metric.value * 100).toFixed(2)}%`
  if (metric.unit === 'count') return String(Math.round(metric.value))
  return metric.value.toFixed(3)
}

export function formatNativeValue(metric: HistoryCompareMetric): string {
  if (metric.unit === 'text') return metric.text ?? '—'
  return formatMetricValue(metric)
}

/** 图例末值：收益率口径带正负号（红涨绿跌），IC 口径没有盈亏方向就不带。 */
export function formatLastValue(item: HistoryCompareItem, isEquity: boolean): string {
  const value = lastValue(item)
  if (value === null) return '—'
  if (!isEquity) return value.toFixed(3)
  const percent = (value - 1) * 100
  return `${percent >= 0 ? '+' : ''}${percent.toFixed(2)}%`
}

export function lastValue(item: HistoryCompareItem): number | null {
  const points = item.curve?.points
  if (!points?.length) return null
  return points[points.length - 1].value
}

export function lastValueTone(item: HistoryCompareItem, isEquity: boolean): string {
  const value = lastValue(item)
  if (value === null || !isEquity) return ''
  return value >= 1 ? 'positive' : 'negative'
}

export function dateRangeText(item: HistoryCompareItem): string {
  const { start, end, days } = item.date_range
  const span = start && end ? `${start} → ${end}` : '区间未记录'
  return days ? `${span}（${days} 个交易日）` : span
}

export function axisNote(xAxis: 'date' | 'index'): string {
  return xAxis === 'index'
    ? '按第 N 个交易日对齐'
    : '按日期对齐（只排实际有数据的交易日）'
}

export function normalizeNote(normalize: string, kind: string | null): string {
  if (!kind) return ''
  return normalize === 'first'
    ? '归一化：除以首值（起点强制为 0%）'
    : '归一化：除以本金（起点为 0%）'
}

export function seriesLabel(item: HistoryCompareItem): string {
  const base = item.curve?.label ? `${item.label}（${item.curve.label}）` : item.label
  return `${base} · ${item.short_id}`
}

export function colorFor(items: readonly HistoryCompareItem[], historyId: string): string {
  const index = items.findIndex((item) => item.history_id === historyId)
  return SERIES_COLORS[(index < 0 ? 0 : index) % SERIES_COLORS.length]
}
