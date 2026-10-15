<script setup lang="ts">
import { computed } from 'vue'
import type { IndividualResearchFullBacktest } from '../types'
import { formatNumber, formatPercent, formatSignedPercent } from '../utils/format'

const props = defineProps<{ backtest: IndividualResearchFullBacktest | null; launchDate?: string; baselineDate?: string; returnCurve?: boolean; hideMetrics?: boolean }>()

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

const chart = computed(() => {
  const points = (props.backtest?.equity_curve ?? []).flatMap((item) => {
    const record = item as Record<string, unknown>
    const value = props.returnCurve ? record.return_pct : record.total ?? record.equity
    const date = String(record.date ?? '')
    return typeof value === 'number' && Number.isFinite(value) ? [{ value, date, directions: Array.isArray(record.trade_directions) ? record.trade_directions as string[] : [] }] : []
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
  const x = (time: number) => 20 + (time - minimumTime) / (maximumTime - minimumTime || 1) * 760
  const values = sample.map(point => point.value)
  // Y 域合并策略与基准；high/low 标注只看策略曲线
  const minimum = Math.min(...values, ...benchmarkSample.map(p => p.value))
  const maximum = Math.max(...values, ...benchmarkSample.map(p => p.value))
  const span = maximum - minimum || 1
  const y = (value: number) => 210 - (value - minimum) / span * 180
  // 年份刻度：次年起每年 1 月 1 日一个刻度
  const yearTicks: Array<{ x: number; label: string }> = []
  for (let year = new Date(minimumTime).getFullYear() + 1; year <= new Date(maximumTime).getFullYear(); year++) {
    const time = Date.parse(`${year}-01-01`)
    if (Number.isFinite(time) && time > minimumTime && time < maximumTime) yearTicks.push({ x: x(time), label: String(year) })
  }
  return { creation, launchX: x(launch), high: props.returnCurve ? formatPercent(Math.max(...values)) : formatNumber(Math.max(...values)), low: props.returnCurve ? formatPercent(Math.min(...values)) : formatNumber(Math.min(...values)),
    benchmark: benchmarkSample.length > 1
      ? { points: benchmarkSample.map(p => `${x(p.time).toFixed(1)},${y(p.value).toFixed(1)}`).join(' ') }
      : null,
    yearTicks,
    markers: sample.filter(p => p.directions.length && props.returnCurve).map(p => ({date:p.date, directions:p.directions.join('/'),x:x(Date.parse(p.date)),y:y(p.value)})),
    first:sample[0]?.date ?? '', last:sample.at(-1)?.date ?? '',
    points:sample.map((point, i) => {
      const horizontal = props.launchDate && Number.isFinite(times[i]) ? x(times[i]!) : 20 + i / Math.max(1,sample.length-1) * 760
      return `${horizontal.toFixed(1)},${y(point.value).toFixed(1)}`
    }).join(' ') }
})
function chartPoints() { return chart.value.points }

function closedTrades(): number {
  return props.backtest?.closed_trades
    ?? (props.backtest?.trades ?? []).filter(
      (trade) => trade.direction === 'SELL' && trade.rejected !== true,
    ).length
}
</script>

<template>
  <div v-if="props.backtest" class="individual-full-detail">
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
      <svg v-if="chartPoints()" viewBox="0 0 800 240" role="img" :aria-label="props.returnCurve ? '策略收益曲线，叠加买入持有基准，标记买卖点与上线日期' : '完整区间权益曲线'">
        <rect v-if="props.launchDate" :x="chart.launchX" y="10" :width="Math.max(0, 780-chart.launchX)" height="210" fill="var(--accent)" opacity="0.08" />
        <line v-if="props.launchDate" :x1="chart.launchX" :x2="chart.launchX" y1="10" y2="220" stroke="var(--accent)" stroke-dasharray="5 4" />
        <text v-if="props.launchDate" :x="Math.max(20,Math.min(chart.launchX,440))" y="16" fill="var(--text)">上线 {{ chart.creation }}</text>
        <text x="20" y="28" fill="var(--muted)">{{ chart.high }}</text><text x="20" y="218" fill="var(--muted)">{{ chart.low }}</text>
        <g v-if="chart.benchmark">
          <polyline :points="chart.benchmark.points" fill="none" stroke="#f59e0b" stroke-width="1.6" stroke-dasharray="4 3" />
        </g>
        <g v-for="tick in chart.yearTicks" :key="tick.label">
          <line :x1="tick.x" :x2="tick.x" y1="220" y2="226" stroke="var(--border)" />
          <text :x="tick.x" y="235" fill="var(--muted)" font-size="9" text-anchor="middle">{{ tick.label }}</text>
        </g>
        <circle v-for="marker in chart.markers" :key="marker.date" :cx="marker.x" :cy="marker.y" r="4" :fill="marker.directions.includes('SELL') ? '#f87171' : '#34d399'" stroke="var(--surface)" stroke-width="1.5"><title>{{ marker.date }} {{ marker.directions }}</title></circle>
        <text x="20" y="235" fill="var(--muted)">{{ chart.first }}</text><text x="680" y="235" fill="var(--muted)">{{ chart.last }}</text>
        <polyline :points="chartPoints()" fill="none" stroke="var(--accent)" stroke-width="3" />
        <g font-size="10">
          <circle cx="470" cy="14" r="4" fill="var(--accent)" /><text x="478" y="18" fill="var(--muted)">策略</text>
          <g v-if="chart.benchmark"><line x1="514" x2="534" y1="14" y2="14" stroke="#f59e0b" stroke-width="1.6" stroke-dasharray="4 3" /><text x="538" y="18" fill="var(--muted)">基准·买入持有</text></g>
          <circle cx="470" cy="30" r="4" fill="#34d399" stroke="var(--surface)" stroke-width="1.5" /><text x="478" y="34" fill="var(--muted)">买入</text>
          <circle cx="514" cy="30" r="4" fill="#f87171" stroke="var(--surface)" stroke-width="1.5" /><text x="522" y="34" fill="var(--muted)">卖出</text>
        </g>
      </svg>
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
  </div>
</template>

<style scoped>
.individual-full-detail { display: grid; gap: 10px; padding: 10px; }
.individual-full-detail > p { margin: 0; color: var(--muted); }
.individual-full-detail svg { width: min(100%, 800px); height: auto; min-height: 180px; border: 1px solid var(--border); background: var(--surface); }
.individual-full-metrics { display: flex; flex-wrap: wrap; gap: 8px 16px; color: var(--muted); }
.individual-benchmark-note { color: var(--subtle); }
.individual-trades { max-height: 340px; }
.individual-trades table { min-width: 800px; }
.individual-trades .trade-buy { color: #34d399; }
.individual-trades .trade-sell { color: #f87171; }
</style>
