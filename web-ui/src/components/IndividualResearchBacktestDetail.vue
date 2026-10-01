<script setup lang="ts">
import type { IndividualResearchFullBacktest } from '../types'

const props = defineProps<{ backtest: IndividualResearchFullBacktest | null }>()

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

function chartPoints(): string {
  const points = (props.backtest?.equity_curve ?? []).flatMap((item) => {
    const total = item.total ?? item.equity
    return typeof total === 'number' && Number.isFinite(total) ? [{ total }] : []
  })
  if (points.length < 2) return ''
  const sample = points.length > 120
    ? points.filter((_, index) => index % Math.ceil(points.length / 120) === 0)
    : points
  const values = sample.map((point) => point.total)
  const minimum = Math.min(...values)
  const span = Math.max(...values) - minimum || 1
  return sample.map((point, index) => {
    const x = index / Math.max(sample.length - 1, 1) * 800
    const y = 220 - (point.total - minimum) / span * 190
    return `${x.toFixed(1)},${y.toFixed(1)}`
  }).join(' ')
}

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
      <div class="individual-full-metrics">
        <span>策略收益 {{ formatPercent(props.backtest.performance.total_return) }}</span>
        <span>持有基准 {{ props.backtest.benchmark ? formatSignedPercent(props.backtest.benchmark_return) : '不可用' }}</span>
        <span>超额收益 {{ formatSignedPercent(props.backtest.excess_return) }}</span>
        <span>年化 {{ formatPercent(props.backtest.performance.annual_return) }}</span>
        <span>夏普 {{ formatNumber(props.backtest.performance.sharpe, 3) }}</span>
        <span>回撤 {{ formatPercent(props.backtest.performance.max_drawdown) }}</span>
        <span>胜率 {{ formatPercent(props.backtest.performance.win_rate) }}</span>
        <span>盈亏比 {{ formatNumber(props.backtest.performance.payoff_ratio, 2) }}</span>
      </div>
      <small class="individual-benchmark-note">基准按区间首日收盘买入并持有至末日收盘，不计交易费用；超额收益是策略收益减基准涨幅。</small>
      <svg v-if="chartPoints()" viewBox="0 0 800 240" role="img" aria-label="完整区间权益曲线">
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
