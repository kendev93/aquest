<script setup lang="ts">
import { computed } from 'vue'
import type { IndividualResearchFullBacktest } from '../types'

const props = defineProps<{ backtest: IndividualResearchFullBacktest | null; launchDate?: string; baselineDate?: string; returnCurve?: boolean; hideMetrics?: boolean }>()

function formatPercent(value: number | null | undefined): string {
  return typeof value === 'number' && Number.isFinite(value)
    ? `${(value * 100).toFixed(2)}%`
    : '—'
}

function formatSignedPercent(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—'
  const sign = value > 0 ? '+' : ''
  return `${sign}${(value * 100).toFixed(2)}%`
}

function formatNumber(value: number | null | undefined, digits = 2): string {
  return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(digits) : '—'
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
  const creation = props.launchDate ? new Date(props.launchDate).toLocaleDateString('en-CA', { timeZone: 'Asia/Shanghai' }) : ''
  const times = sample.map(p => Date.parse(p.date))
  const launch = Date.parse(creation)
  const validTimes = times.filter(Number.isFinite)
  const minimumTime = Math.min(...validTimes, ...(Number.isFinite(launch) ? [launch] : []))
  const maximumTime = Math.max(...validTimes, ...(Number.isFinite(launch) ? [launch] : []))
  const x = (time: number) => 20 + (time - minimumTime) / (maximumTime - minimumTime || 1) * 760
  const values = sample.map(point => point.value)
  const minimum = Math.min(...values); const span = Math.max(...values) - minimum || 1
  return { creation, launchX: x(launch), high: props.returnCurve ? formatPercent(Math.max(...values)) : formatNumber(Math.max(...values)), low: props.returnCurve ? formatPercent(minimum) : formatNumber(minimum),
    markers: sample.filter(p => p.directions.length && props.returnCurve).map(p => ({date:p.date, directions:p.directions.join('/'),x:x(Date.parse(p.date)),y:210-(p.value-minimum)/span*180})),
    first:sample[0]?.date ?? '', last:sample.at(-1)?.date ?? '',
    points:sample.map((point, i) => {
      const horizontal = props.launchDate && Number.isFinite(times[i]) ? x(times[i]!) : 20 + i / Math.max(1,sample.length-1) * 760
      return `${horizontal.toFixed(1)},${(210 - (point.value-minimum)/span*180).toFixed(1)}`
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
      <svg v-if="chartPoints()" viewBox="0 0 800 240" role="img" :aria-label="props.returnCurve ? '策略收益曲线，标记上线日期' : '完整区间权益曲线'">
        <rect v-if="props.launchDate" :x="chart.launchX" y="10" :width="Math.max(0, 780-chart.launchX)" height="210" fill="var(--accent)" opacity="0.08" />
        <line v-if="props.launchDate" :x1="chart.launchX" :x2="chart.launchX" y1="10" y2="220" stroke="var(--accent)" stroke-dasharray="5 4" />
        <text v-if="props.launchDate" :x="Math.max(20,Math.min(chart.launchX,600))" y="16" fill="var(--text)">上线 {{ chart.creation }}</text>
        <text x="20" y="28" fill="var(--muted)">{{ chart.high }}</text><text x="20" y="218" fill="var(--muted)">{{ chart.low }}</text>
        <circle v-for="marker in chart.markers" :key="marker.date" :cx="marker.x" :cy="marker.y" r="4" :fill="marker.directions.includes('SELL') ? '#f87171' : '#34d399'"><title>{{ marker.date }} {{ marker.directions }}</title></circle>
        <text x="20" y="235" fill="var(--muted)">{{ chart.first }}</text><text x="680" y="235" fill="var(--muted)">{{ chart.last }}</text>
        <polyline :points="chartPoints()" fill="none" stroke="var(--accent)" stroke-width="3" />
      </svg>
      <div v-if="props.backtest.trades.length" class="table-wrap individual-trades">
        <table>
          <thead><tr><th>日期</th><th>方向</th><th>价格</th><th>数量</th><th>盈亏</th><th>原因</th><th>风控触发</th></tr></thead>
          <tbody>
            <tr v-for="(trade, index) in props.backtest.trades" :key="`${String(trade.date ?? '')}-${index}`">
              <td>{{ tradeValue(trade, 'date') }}</td><td>{{ tradeValue(trade, 'direction') }}</td>
              <td>{{ tradeValue(trade, 'price') }}</td><td>{{ tradeValue(trade, 'size') }}</td>
              <td>{{ tradeValue(trade, 'pnl') }}</td><td>{{ tradeValue(trade, 'reason') }}</td>
              <td>{{ tradeValue(trade, 'risk_trigger') }}</td>
            </tr>
          </tbody>
        </table>
      </div>
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
</style>
