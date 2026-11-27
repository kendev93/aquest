<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { IndividualResearchFullBacktest } from '../types'
import { formatNumber, formatPercent, formatSignedPercent } from '../utils/format'

const props = defineProps<{ backtest: IndividualResearchFullBacktest | null; launchDate?: string; baselineDate?: string; returnCurve?: boolean; hideMetrics?: boolean; hideTrades?: boolean }>()

// 方向与曲线图例同词同色：图例写「买入/卖出」，表格里露 BUY/SELL 会让用户以为
// 是两套东西。signal_date 单独成列，因为信号在收盘后判定、下一交易日才成交，
// 成交日常比信号日晚一天，只看日期列会误判策略的响应速度。
const TRADE_DIRECTIONS: Record<string, string> = { BUY: '买入', SELL: '卖出' }

function tradeDirection(value: unknown): string {
  return typeof value === 'string' ? TRADE_DIRECTIONS[value] ?? value : '—'
}

function tradeDirectionClass(value: unknown): string {
  return value === 'BUY' ? 'trade-buy' : value === 'SELL' ? 'trade-sell' : ''
}

function tradeValue(trade: Record<string, unknown>, key: string): string {
  const value = trade[key]
  if (typeof value === 'number') return formatNumber(value, key === 'size' ? 0 : 2)
  return typeof value === 'string' && value ? value : '—'
}

function markerText(date: string, directions: string[], trades: Record<string, unknown>[]): string {
  if (!trades.length) return `${date} · ${directions.map(tradeDirection).join(' / ')}`
  return trades.map((trade) => [
    `日期：${tradeValue(trade, 'date')}`,
    ...(typeof trade.symbol === 'string' && trade.symbol ? [`标的：${trade.symbol}`] : []),
    `方向：${tradeDirection(trade.direction)}`,
    `价格：${tradeValue(trade, 'price')}`,
    `数量：${tradeValue(trade, 'size')}`,
    `盈亏：${tradeValue(trade, 'pnl')}`,
    `原因：${tradeValue(trade, 'reason')}`,
    `信号日：${tradeValue(trade, 'signal_date')}`,
  ].join('\n')).join('\n\n')
}

// 固定轴标签留白，图例和上线日期放在绘图区外。
const plot = { left: 86, right: 774, top: 16, bottom: 204 }

const chart = computed(() => {
  const points = (props.backtest?.equity_curve ?? []).flatMap((item) => {
    const record = item as Record<string, unknown>
    const value = props.returnCurve ? record.return_pct : record.total ?? record.equity
    const date = String(record.date ?? '')
    const trades = Array.isArray(record.trade_events) ? record.trade_events as Record<string, unknown>[] : []
    return typeof value === 'number' && Number.isFinite(value) ? [{ value, date, trades, directions: Array.isArray(record.trade_directions) ? record.trade_directions as string[] : [] }] : []
  })
  const sample = !props.returnCurve && points.length > 120
    ? points.filter((_, i) => i % Math.ceil(points.length / 120) === 0 || i === points.length - 1) : points
  // 基准（买入持有）净值序列：BenchmarkCurve 的 dates(int YYYYMMDD)/totals，
  // 与策略资金曲线同交易日。先裁剪到策略显示窗口，再以窗口首日归一化，
  // 保证“全历史”和“创建以来”两种区间下基准都与策略同起点可比
  const benchmarkRaw = props.backtest?.benchmark ?? null
  const benchDates = Array.isArray(benchmarkRaw?.dates) ? benchmarkRaw.dates : []
  const benchTotals = Array.isArray(benchmarkRaw?.totals) ? benchmarkRaw.totals : []
  const parseCurveDate = (value: unknown): number => {
    if (typeof value === 'number') {
      const text = String(value)
      return text.length === 8 ? Date.parse(`${text.slice(0, 4)}-${text.slice(4, 6)}-${text.slice(6, 8)}`) : value
    }
    return typeof value === 'string' ? Date.parse(value) : Number.NaN
  }
  const creation = props.launchDate ? new Date(props.launchDate).toLocaleDateString('en-CA', { timeZone: 'Asia/Shanghai' }) : ''
  const times = sample.map(p => Date.parse(p.date))
  const launch = Date.parse(creation)
  const validTimes = times.filter(Number.isFinite)
  const minimumTime = Math.min(...validTimes, ...(Number.isFinite(launch) ? [launch] : []))
  const maximumTime = Math.max(...validTimes, ...(Number.isFinite(launch) ? [launch] : []))
  let benchmark = benchDates
    .map((raw, i) => ({ time: parseCurveDate(raw), value: Number(benchTotals[i]) }))
    .filter(p => Number.isFinite(p.time) && p.time >= minimumTime && p.time <= maximumTime
      && Number.isFinite(p.value) && p.value > 0)
  if (benchmark.length > 1 && benchmark[0]!.value > 0) {
    const base = benchmark[0]!.value
    benchmark = benchmark.map(p => ({ time: p.time, value: props.returnCurve ? p.value / base - 1 : p.value }))
  } else benchmark = []
  const benchmarkSample = benchmark.length > 240
    ? benchmark.filter((_, i) => i % Math.ceil(benchmark.length / 240) === 0 || i === benchmark.length - 1)
    : benchmark
  const x = (time: number) => plot.left + (time - minimumTime) / (maximumTime - minimumTime || 1) * (plot.right - plot.left)
  const values = sample.map(point => point.value)
  const minimum = Math.min(...values, ...benchmarkSample.map(p => p.value))
  const maximum = Math.max(...values, ...benchmarkSample.map(p => p.value))
  const padding = minimum === maximum ? (props.returnCurve ? 0.01 : 1) : (maximum - minimum) * 0.05
  const bottomValue = minimum - padding
  const span = maximum + padding - bottomValue
  const y = (value: number) => plot.bottom - (value - bottomValue) / span * (plot.bottom - plot.top)
  const yTicks = Array.from({ length: 5 }, (_, i) => {
    const value = bottomValue + span * i / 4
    return { y: y(value), label: props.returnCurve ? formatPercent(value) : formatNumber(value) }
  })
  const xTicks = Number.isFinite(minimumTime) && Number.isFinite(maximumTime)
    ? Array.from({ length: minimumTime === maximumTime ? 1 : 5 }, (_, i) => {
      const time = minimumTime + (maximumTime - minimumTime) * i / 4
      return { x: x(time), label: new Date(time).toISOString().slice(0, 10) }
    })
    : []
  return { creation, launchX: Number.isFinite(launch) ? x(launch) : null,
    benchmark: benchmarkSample.length > 1
      ? { points: benchmarkSample.map(p => `${x(p.time).toFixed(1)},${y(p.value).toFixed(1)}`).join(' ') }
      : null,
    xTicks, yTicks,
    markers: sample.filter(p => p.directions.length && props.returnCurve).map(p => ({date:p.date, directions:p.directions.join('/'), text: markerText(p.date, p.directions, p.trades),x:x(Date.parse(p.date)),y:y(p.value)})),
    points:sample.map((point, i) => {
      const horizontal = Number.isFinite(times[i]) ? x(times[i]!) : plot.left + i / Math.max(1, sample.length - 1) * (plot.right - plot.left)
      return `${horizontal.toFixed(1)},${y(point.value).toFixed(1)}`
    }).join(' ') }
})
function chartPoints() { return chart.value.points }

const curvePlot = ref<HTMLElement | null>(null)
const tradeTooltip = ref<{ text: string; x: number; y: number } | null>(null)

// 用实际像素定位，曲线缩放后提示仍跟着买卖点。
function showTradeTooltip(event: Event, text: string): void {
  const target = event.currentTarget
  const plotBounds = curvePlot.value?.getBoundingClientRect()
  if (!(target instanceof SVGElement) || !plotBounds) return
  const marker = target.getBoundingClientRect()
  tradeTooltip.value = {
    text,
    x: marker.left + marker.width / 2 - plotBounds.left,
    y: marker.bottom - plotBounds.top + 6,
  }
}

watch(chart, () => { tradeTooltip.value = null })

function closedTrades(): number {
  return props.backtest?.closed_trades
    ?? (props.backtest?.trades ?? []).filter(
      (trade) => trade.direction === 'SELL' && trade.rejected !== true,
    ).length
}
</script>

<template>
  <div v-if="props.backtest" class="individual-full-detail" :class="{ 'return-curve-detail': props.returnCurve }">
    <p>{{ props.backtest.start_date }} → {{ props.backtest.end_date }} · {{ props.backtest.bars }} 个交易日 · {{ closedTrades() }} 笔交易</p>
    <p v-if="props.backtest.error" class="field-error">{{ props.backtest.error }}</p>
    <template v-else>
      <div v-if="!props.hideMetrics" class="individual-full-metrics">
        <span>策略收益 {{ formatPercent(props.backtest.performance.total_return) }}</span>
        <span>持有基准 {{ props.backtest.benchmark ? formatSignedPercent(props.backtest.benchmark_return) : '不可用' }}</span>
        <span>超额收益 {{ formatSignedPercent(props.backtest.excess_return) }}</span>
        <span>年化 {{ formatPercent(props.backtest.performance.annual_return) }}</span>
        <span>夏普 {{ formatNumber(props.backtest.performance.sharpe, 3) }}</span>
        <span>回撤 {{ formatPercent(props.backtest.performance.max_drawdown) }}</span>
        <span>胜率 {{ formatPercent(props.backtest.performance.win_rate) }}</span>
        <span>盈亏比 {{ formatNumber(props.backtest.performance.payoff_ratio, 2) }}</span>
      </div>
      <small v-if="!props.hideMetrics" class="individual-benchmark-note">基准按区间首日收盘买入并持有至末日收盘，不计交易费用；超额收益是策略收益减基准涨幅。</small>
      <div v-if="chartPoints()" class="curve-header">
        <div class="curve-legend" aria-label="曲线图例">
          <span><i class="strategy-key" />策略</span>
          <span v-if="chart.benchmark"><i class="benchmark-key" />基准·买入持有</span>
          <template v-if="props.returnCurve">
            <span><i class="buy-key" />买入</span>
            <span><i class="sell-key" />卖出</span>
          </template>
        </div>
        <small v-if="chart.launchX !== null">上线 {{ chart.creation }}</small>
      </div>
      <div v-if="chartPoints()" ref="curvePlot" class="curve-plot">
        <svg viewBox="0 0 800 240" role="img" :aria-label="props.returnCurve ? '策略收益曲线，叠加买入持有基准，标记买卖点与上线日期' : '完整区间权益曲线'">
          <rect v-if="chart.launchX !== null" :x="chart.launchX" :y="plot.top" :width="plot.right - chart.launchX" :height="plot.bottom - plot.top" fill="var(--accent)" opacity="0.08" />
          <g v-for="tick in chart.yTicks" :key="tick.y">
            <line :x1="plot.left" :x2="plot.right" :y1="tick.y" :y2="tick.y" class="curve-grid" />
            <text :x="plot.left - 8" :y="tick.y" dominant-baseline="middle" text-anchor="end" class="curve-tick">{{ tick.label }}</text>
          </g>
          <path :d="`M ${plot.left} ${plot.top} V ${plot.bottom} H ${plot.right}`" class="curve-axis" />
          <g v-for="tick in chart.xTicks" :key="tick.x">
            <line :x1="tick.x" :x2="tick.x" :y1="plot.bottom" :y2="plot.bottom + 5" class="curve-axis" />
            <text :x="tick.x" :y="plot.bottom + 21" :text-anchor="tick.x === plot.right ? 'end' : tick.x === plot.left ? 'start' : 'middle'" class="curve-tick">{{ tick.label }}</text>
          </g>
          <line v-if="chart.launchX !== null" :x1="chart.launchX" :x2="chart.launchX" :y1="plot.top" :y2="plot.bottom" stroke="var(--accent)" stroke-dasharray="5 4" />
          <polyline v-if="chart.benchmark" :points="chart.benchmark.points" fill="none" stroke="#f59e0b" stroke-width="1.6" stroke-dasharray="4 3" />
          <polyline :points="chartPoints()" fill="none" stroke="var(--accent)" stroke-width="3" />
          <g v-for="marker in chart.markers" :key="marker.date"
            tabindex="0" :aria-label="marker.text"
            @pointerenter="showTradeTooltip($event, marker.text)"
            @pointerleave="tradeTooltip = null"
            @focus="showTradeTooltip($event, marker.text)"
            @blur="tradeTooltip = null"
            @keydown.esc="tradeTooltip = null">
            <circle :cx="marker.x" :cy="marker.y" r="9" fill="transparent" />
            <circle :cx="marker.x" :cy="marker.y" r="4" :fill="marker.directions.includes('SELL') ? '#f87171' : '#34d399'" stroke="var(--surface)" stroke-width="1.5" />
          </g>
        </svg>
        <div v-if="tradeTooltip" class="curve-trade-tooltip" role="tooltip"
          :style="{ left: `clamp(0px, ${tradeTooltip.x}px, 100% - min(260px, 100%))`, top: `${tradeTooltip.y}px` }">{{ tradeTooltip.text }}</div>
      </div>
      <slot name="after-chart" />
      <template v-if="!props.hideTrades">
        <template v-if="props.backtest.trades.length">
          <slot name="trades-toolbar" />
          <div class="table-wrap individual-trades">
            <table>
              <thead><tr><th>日期</th><th>方向</th><th>价格</th><th>数量</th><th>盈亏</th><th>原因</th><th>信号日</th></tr></thead>
              <tbody>
                <tr v-for="(trade, index) in props.backtest.trades" :key="`${String(trade.date ?? '')}-${index}`">
                  <td>{{ tradeValue(trade, 'date') }}</td>
                  <td :class="tradeDirectionClass(trade.direction)">{{ tradeDirection(trade.direction) }}</td>
                  <td>{{ tradeValue(trade, 'price') }}</td><td>{{ tradeValue(trade, 'size') }}</td>
                  <td>{{ tradeValue(trade, 'pnl') }}</td><td>{{ tradeValue(trade, 'reason') }}</td>
                  <td>{{ tradeValue(trade, 'signal_date') }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </template>
        <p v-else class="inline-empty">该候选在全区间没有成交。</p>
      </template>
    </template>
  </div>
</template>

<style scoped>
/* 单列轨道必须可收缩：交易表 min-width 800，auto 轨道会把整块（含插槽里的详情分析）
   撑出页面，出现整页横向滚动。表格自己的 .table-wrap 里滚动。 */
.individual-full-detail { display: grid; grid-template-columns: minmax(0, 1fr); gap: 10px; padding: 10px; }
.individual-full-detail > p { margin: 0; color: var(--muted); }
.curve-plot { position: relative; }
.curve-trade-tooltip { position: absolute; z-index: 2; width: min(260px, 100%); box-sizing: border-box; padding: 10px 12px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface); color: var(--text); box-shadow: 0 4px 14px #0005; white-space: pre-wrap; overflow-wrap: anywhere; font-size: 13px; pointer-events: none; }
.curve-header { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px 20px; width: min(100%, 800px); margin: 8px 0; color: var(--muted); }
.curve-legend { display: flex; flex-wrap: wrap; gap: 6px 14px; font-size: 13px; }
.curve-legend span { display: inline-flex; align-items: center; gap: 6px; }
.curve-legend i { display: inline-block; width: 9px; height: 9px; border-radius: 50%; }
.curve-legend .strategy-key { background: var(--accent); }
.curve-legend .benchmark-key { width: 16px; height: 0; border-top: 2px dashed #f59e0b; border-radius: 0; }
.curve-legend .buy-key { background: #34d399; }
.curve-legend .sell-key { background: #f87171; }
.curve-grid { stroke: var(--border); }
.curve-axis { fill: none; stroke: var(--muted); }
.curve-tick { fill: var(--muted); font-size: 12px; }
.individual-full-detail svg { width: min(100%, 800px); height: auto; min-height: 180px; border: 1px solid var(--border); background: var(--surface); }
.return-curve-detail .curve-header,
.return-curve-detail svg { box-sizing: border-box; width: 100%; }
.individual-full-metrics { display: flex; flex-wrap: wrap; gap: 8px 16px; color: var(--muted); }
.individual-benchmark-note { color: var(--subtle); }
.individual-trades { max-height: 340px; }
.individual-trades table { min-width: 800px; }
.individual-trades .trade-buy { color: #34d399; }
.individual-trades .trade-sell { color: #f87171; }
</style>
