<script setup lang="ts">
import { computed } from 'vue'

import type {
  StrategyPeriodMetrics,
  StrategyReplayCandidate,
  StrategyReplayResult,
} from '../types'

type ReplayPeriod = 'overall' | 'ytd' | 'one_year' | 'two_year' | 'annual'

interface Props {
  result: StrategyReplayResult
  period: ReplayPeriod
  selectedCandidateHash: string | null
}

const props = defineProps<Props>()

const emit = defineEmits<{
  'update:period': [period: ReplayPeriod]
  select: [candidateHash: string]
  apply: [candidate: StrategyReplayCandidate]
}>()

const annualYears = computed(() => {
  const years = new Set(
    props.result.candidates.flatMap((candidate) => candidate.annual_metrics.map((metric) => metric.label)),
  )
  return [...years].sort((left, right) => Number(left) - Number(right))
})

const selectedCandidate = computed(() => props.result.candidates.find(
  (candidate) => candidate.candidate_hash === props.selectedCandidateHash,
) ?? null)

function metricValue(metrics: Record<string, unknown>, key: string): number | null {
  const value = metrics[key]
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function formatPercent(value: number | null): string {
  return value === null ? '—' : `${(value * 100).toFixed(2)}%`
}

function formatNumber(value: number | null, digits = 2): string {
  return value === null ? '—' : value.toFixed(digits)
}

function periodMetrics(candidate: StrategyReplayCandidate): StrategyPeriodMetrics | null {
  if (props.period === 'ytd' || props.period === 'one_year' || props.period === 'two_year') {
    return candidate.periods[props.period]
  }
  return null
}

function annualMetric(candidate: StrategyReplayCandidate, year: string): StrategyPeriodMetrics | null {
  return candidate.annual_metrics.find((metric) => metric.label === year) ?? null
}

function annualIsCurrentPartial(year: string): boolean {
  const currentYear = props.result.data_end.slice(0, 4)
  return year === currentYear && props.result.candidates.some(
    (candidate) => annualMetric(candidate, year)?.partial === true,
  )
}

function returnDelta(candidate: StrategyReplayCandidate): number | null {
  const baseline = metricValue(candidate.leaderboard_metrics, 'annual_return')
  const overall = metricValue(candidate.overall_metrics, 'annual_return')
  return baseline === null || overall === null ? null : overall - baseline
}

function periodTradeCount(metrics: StrategyPeriodMetrics | null): string {
  if (!metrics || metrics.diagnostic) return '样本不足'
  return formatNumber(metrics.total_trades, 0)
}

function periodDiagnostic(metrics: StrategyPeriodMetrics | null): string {
  return metrics?.diagnostic ? '样本不足' : ''
}

function riskTriggerLabel(value: string | null | undefined): string {
  return {
    stop_loss: '止损',
    take_profit: '止盈',
    trailing_stop: '移动止盈',
  }[value ?? ''] ?? '—'
}

function chartPoints(candidate: StrategyReplayCandidate): string {
  const points = candidate.backtest.equity_curve
    .filter((point) => typeof point.total === 'number' && Number.isFinite(point.total))
  if (points.length < 2) return ''
  const sample = points.length > 120
    ? points.filter((_, index) => index % Math.ceil(points.length / 120) === 0)
    : points
  const values = sample.map((point) => point.total)
  const minimum = Math.min(...values)
  const maximum = Math.max(...values)
  const span = maximum - minimum || 1
  return sample.map((point, index) => {
    const x = (index / Math.max(sample.length - 1, 1)) * 800
    const y = 220 - ((point.total - minimum) / span) * 190
    return `${x.toFixed(1)},${y.toFixed(1)}`
  }).join(' ')
}

function selectCandidate(candidate: StrategyReplayCandidate): void {
  emit('select', candidate.candidate_hash)
}
</script>

<template>
  <section class="strategy-replay-results" data-testid="strategy-replay-results">
    <div class="replay-meta">
      <span class="result-badge">全量复核 {{ props.result.candidate_count }} 个策略</span>
      <span>{{ props.result.data_start }} → {{ props.result.data_end }}</span>
      <span v-if="props.result.data_changed" class="replay-warning" data-testid="strategy-replay-data-changed">行情已更新，数据指纹与实验时不同</span>
    </div>

    <div class="formula-mode-tabs replay-period-tabs" role="tablist" aria-label="全量复核时间段">
      <button type="button" data-testid="strategy-replay-period-overall" :class="{ active: props.period === 'overall' }" role="tab" :aria-selected="props.period === 'overall'" @click="emit('update:period', 'overall')">整体</button>
      <button type="button" data-testid="strategy-replay-period-ytd" :class="{ active: props.period === 'ytd' }" role="tab" :aria-selected="props.period === 'ytd'" @click="emit('update:period', 'ytd')">今年以来</button>
      <button type="button" data-testid="strategy-replay-period-one-year" :class="{ active: props.period === 'one_year' }" role="tab" :aria-selected="props.period === 'one_year'" @click="emit('update:period', 'one_year')">最近一年</button>
      <button type="button" data-testid="strategy-replay-period-two-year" :class="{ active: props.period === 'two_year' }" role="tab" :aria-selected="props.period === 'two_year'" @click="emit('update:period', 'two_year')">最近两年</button>
      <button type="button" data-testid="strategy-replay-period-annual" :class="{ active: props.period === 'annual' }" role="tab" :aria-selected="props.period === 'annual'" @click="emit('update:period', 'annual')">分年度</button>
    </div>

    <div v-if="props.period === 'overall'" class="table-wrap">
      <table class="strategy-replay-table" data-testid="strategy-replay-overall-table">
        <caption class="sr-only">前十策略全量回测整体绩效</caption>
        <thead><tr><th>#</th><th>策略</th><th>排行榜验证年化收益</th><th>排行榜验证夏普</th><th>全区间收益</th><th>年化收益</th><th>最大回撤</th><th>夏普</th><th>交易数</th><th>胜率</th><th>换手率</th><th>验证年化收益变化</th></tr></thead>
        <tbody>
          <tr v-for="candidate in props.result.candidates" :key="candidate.candidate_hash" :class="{ selected: props.selectedCandidateHash === candidate.candidate_hash }">
            <td>{{ candidate.rank }}</td>
            <td><button class="table-link replay-candidate-link" :data-testid="`strategy-replay-candidate-${candidate.candidate_hash}`" type="button" @click="selectCandidate(candidate)">{{ candidate.human_readable_rules || candidate.candidate_hash }}</button><small class="replay-hash">{{ candidate.candidate_hash }}</small><small v-if="candidate.diagnostic" class="period-diagnostic">{{ candidate.diagnostic }}</small></td>
            <td>{{ formatPercent(metricValue(candidate.leaderboard_metrics, 'annual_return')) }}</td>
            <td>{{ formatNumber(metricValue(candidate.leaderboard_metrics, 'sharpe')) }}</td>
            <td :class="{ positive: (metricValue(candidate.overall_metrics, 'total_return') ?? 0) >= 0, negative: (metricValue(candidate.overall_metrics, 'total_return') ?? 0) < 0 }">{{ formatPercent(metricValue(candidate.overall_metrics, 'total_return')) }}</td>
            <td>{{ formatPercent(metricValue(candidate.overall_metrics, 'annual_return')) }}</td>
            <td>{{ formatPercent(metricValue(candidate.overall_metrics, 'max_drawdown')) }}</td>
            <td>{{ formatNumber(metricValue(candidate.overall_metrics, 'sharpe')) }}</td>
            <td>{{ formatNumber(metricValue(candidate.overall_metrics, 'total_trades'), 0) }}</td>
            <td>{{ formatPercent(metricValue(candidate.overall_metrics, 'win_rate')) }}</td>
            <td>{{ formatPercent(metricValue(candidate.overall_metrics, 'turnover')) }}</td>
            <td :class="{ positive: (returnDelta(candidate) ?? 0) >= 0, negative: (returnDelta(candidate) ?? 0) < 0 }">{{ formatPercent(returnDelta(candidate)) }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-else-if="props.period === 'annual'" class="table-wrap">
      <table class="strategy-replay-table annual-replay-table" data-testid="strategy-replay-annual-table">
        <caption class="sr-only">前十策略分年度收益</caption>
        <thead><tr><th>#</th><th>策略</th><th v-for="year in annualYears" :key="year">{{ year }}<small v-if="annualIsCurrentPartial(year)" class="period-diagnostic">截至最新行情日</small></th></tr></thead>
        <tbody><tr v-for="candidate in props.result.candidates" :key="candidate.candidate_hash"><td>{{ candidate.rank }}</td><td><button class="table-link replay-candidate-link" :data-testid="`strategy-replay-candidate-${candidate.candidate_hash}`" type="button" @click="selectCandidate(candidate)">{{ candidate.human_readable_rules || candidate.candidate_hash }}</button></td><td v-for="year in annualYears" :key="`${candidate.candidate_hash}-${year}`" :class="{ positive: (metricValue(annualMetric(candidate, year) ?? {}, 'total_return') ?? 0) >= 0, negative: (metricValue(annualMetric(candidate, year) ?? {}, 'total_return') ?? 0) < 0 }">{{ formatPercent(metricValue(annualMetric(candidate, year) ?? {}, 'total_return')) }}<small v-if="annualMetric(candidate, year)?.diagnostic" class="period-diagnostic">样本不足</small></td></tr></tbody>
      </table>
    </div>

    <div v-else class="table-wrap">
      <table class="strategy-replay-table period-replay-table" data-testid="strategy-replay-period-table">
        <caption class="sr-only">前十策略分时间段绩效</caption>
        <thead><tr><th>#</th><th>策略</th><th>实际区间</th><th>收益</th><th>回撤</th><th>夏普</th><th>交易数</th></tr></thead>
        <tbody><tr v-for="candidate in props.result.candidates" :key="candidate.candidate_hash"><td>{{ candidate.rank }}</td><td><button class="table-link replay-candidate-link" :data-testid="`strategy-replay-candidate-${candidate.candidate_hash}`" type="button" @click="selectCandidate(candidate)">{{ candidate.human_readable_rules || candidate.candidate_hash }}</button></td><td class="muted-code">{{ periodMetrics(candidate)?.start_date || '—' }}<br>至 {{ periodMetrics(candidate)?.end_date || '—' }}<small v-if="periodDiagnostic(periodMetrics(candidate))" class="period-diagnostic">{{ periodDiagnostic(periodMetrics(candidate)) }}</small></td><td :class="{ positive: (periodMetrics(candidate)?.total_return ?? 0) >= 0, negative: (periodMetrics(candidate)?.total_return ?? 0) < 0 }">{{ formatPercent(periodMetrics(candidate)?.total_return ?? null) }}</td><td>{{ formatPercent(periodMetrics(candidate)?.max_drawdown ?? null) }}</td><td>{{ formatNumber(periodMetrics(candidate)?.sharpe ?? null) }}</td><td>{{ periodTradeCount(periodMetrics(candidate)) }}</td></tr></tbody>
      </table>
    </div>

    <div v-if="selectedCandidate" class="strategy-replay-detail" :data-testid="`strategy-replay-detail-${selectedCandidate.candidate_hash}`">
      <div class="section-heading"><div><span class="section-kicker">FULL REPLAY DETAIL</span><h3>{{ selectedCandidate.human_readable_rules || selectedCandidate.candidate_hash }}</h3></div><button class="secondary-button" type="button" @click="emit('select', '')">关闭</button></div>
      <p v-if="selectedCandidate.diagnostic" class="replay-warning">{{ selectedCandidate.diagnostic }}</p>
      <div class="metrics-strip replay-detail-metrics"><div><span>全区间收益</span><strong>{{ formatPercent(metricValue(selectedCandidate.overall_metrics, 'total_return')) }}</strong></div><div><span>最大回撤</span><strong>{{ formatPercent(metricValue(selectedCandidate.overall_metrics, 'max_drawdown')) }}</strong></div><div><span>夏普</span><strong>{{ formatNumber(metricValue(selectedCandidate.overall_metrics, 'sharpe')) }}</strong></div><div><span>成交数</span><strong>{{ formatNumber(metricValue(selectedCandidate.overall_metrics, 'total_trades'), 0) }}</strong></div><div><span>胜率</span><strong>{{ formatPercent(metricValue(selectedCandidate.overall_metrics, 'win_rate')) }}</strong></div><div><span>换手率</span><strong>{{ formatPercent(metricValue(selectedCandidate.overall_metrics, 'turnover')) }}</strong></div></div>
      <button class="secondary-button replay-apply-button" type="button" :data-testid="`strategy-replay-apply-${selectedCandidate.candidate_hash}`" :disabled="!selectedCandidate.data_fingerprint" @click="emit('apply', selectedCandidate)">{{ selectedCandidate.data_fingerprint ? '应用到普通组合回测' : '复核失败，无法应用' }}</button>
      <pre>{{ JSON.stringify(selectedCandidate.config, null, 2) }}</pre>
      <section class="chart-card"><div class="chart-heading"><div><span class="section-kicker">EQUITY CURVE</span><h3>全区间资金曲线</h3></div></div><svg class="equity-chart" viewBox="0 0 800 240" role="img" aria-label="策略全区间资金曲线"><polyline :points="chartPoints(selectedCandidate)" fill="none" stroke="var(--accent)" stroke-linecap="round" stroke-linejoin="round" stroke-width="3"></polyline></svg></section>
      <section class="data-section"><div class="section-heading"><div><span class="section-kicker">TRADES</span><h3>完整成交记录</h3></div><span>{{ selectedCandidate.backtest.trades.length }} 笔</span></div><div v-if="!selectedCandidate.backtest.trades.length" class="inline-empty">没有形成可执行成交。</div><div v-else class="table-wrap replay-trades-wrap"><table><thead><tr><th>日期</th><th>股票</th><th>方向</th><th>数量</th><th>价格</th><th>盈亏</th><th>原因</th><th>风险触发</th><th>冷却</th></tr></thead><tbody><tr v-for="(trade, index) in selectedCandidate.backtest.trades" :key="`${trade.date}-${trade.code}-${index}`"><td class="muted-code">{{ trade.date }}</td><td>{{ trade.market }} {{ trade.code }}</td><td>{{ trade.direction === 'BUY' ? '买入' : '卖出' }}</td><td>{{ trade.size }}</td><td>{{ trade.price.toFixed(2) }}</td><td :class="{ positive: trade.pnl >= 0, negative: trade.pnl < 0 }">{{ trade.pnl.toFixed(2) }}</td><td>{{ trade.reason }}</td><td>{{ riskTriggerLabel(trade.risk_trigger) }}</td><td>{{ trade.cooldown_days ?? 0 }} 日</td></tr></tbody></table></div></section>
    </div>
  </section>
</template>
