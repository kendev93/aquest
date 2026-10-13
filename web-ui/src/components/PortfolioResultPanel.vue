<script setup lang="ts">
/**
 * 组合回测的结果展示主体：摘要、口径、复现比对、指标条、基准、资金曲线、
 * 持仓/成交/排名/净值表、失败摘要。
 *
 * 从 PortfolioBacktestPage 拆出来。纯展示：result / availableValues /
 * replayComparison 由 props 下行，没有任何回写；「盈亏分析」的开关和
 * 「导出 JSON」留在页面（按钮在 section 头部，导出还要带页面的口径字段）。
 * 主体中间的处理进度条读的是页面的 job 进度，所以以 jobProgress 传入。
 */

import { computed } from 'vue'

import PortfolioPnlAnalysis from './PortfolioPnlAnalysis.vue'
import TermHint from './TermHint.vue'
import { formatMoney, formatNumber, formatPercent, formatTradeRatio } from '../utils/format'
import type { TradeRatioKey } from '../utils/format'
import { equityChartIndices, equityChartPoints, equityChartRange } from '../utils/portfolioChart'
import type {
  PortfolioBacktestResult,
  PortfolioReplayComparison,
  ValueDefinition,
} from '../types'

const props = defineProps<{
  result: PortfolioBacktestResult | null
  availableValues: ValueDefinition[]
  replayComparison: PortfolioReplayComparison | null
  /** 导出与「回测口径」展示用的价格口径（页面负责计算与下载）。 */
  priceBasis: string
  limitations: string[]
  showPnlAnalysis: boolean
  /** 最近一次回测任务的处理进度（0-1）；结果出来后通常已是 1。 */
  jobProgress: number | null
}>()

const latestState = computed(() => props.result?.states.at(-1) ?? null)
const latestRanking = computed(() => props.result?.ranking_events.at(-1) ?? null)
const sampledEquity = computed(() => {
  const curve = props.result?.equity_curve ?? []
  return curve.length > 60 ? curve.slice(-60) : curve
})
const recentTrades = computed(() => (props.result?.trades ?? []).slice(-100).reverse())
// 指定标的里没进回测的代码必须让用户看见：否则填了 10 只、实际只跑 8 只，
// 报告照样完整，只有「已处理 N / M」里的 M 会小一点，没人会去数。
const droppedCodes = computed(() => [
  ...(props.result?.missing_codes ?? []).map(code => ({ code, reason: '行情库里没有' })),
  ...(props.result?.filtered_codes ?? []).map(code => ({ code, reason: '被板块或类型范围排除' })),
  ...(props.result?.excluded_st_codes ?? []).map(code => ({ code, reason: 'ST/退市已排除' })),
])
// 基准对照：同一批股票各买一点然后不动。没有它，年化 20% 在牛市里可能只是没跑赢。
const benchmark = computed(() => props.result?.benchmark ?? null)

function valueName(id: string): string {
  return props.availableValues.find((value) => value.id === id)?.display_name ?? id
}

function performanceValue(key: string): number | null {
  return props.result?.performance[key] ?? null
}

function benchmarkValue(key: string): number | null {
  return benchmark.value?.performance[key] ?? null
}

function formatPortfolioRatio(key: TradeRatioKey): string {
  return formatTradeRatio(key, {
    value: performanceValue(key),
    totalTrades: performanceValue('total_trades'),
    loseTrades: performanceValue('lose_trades'),
  })
}

function formatTurnoverMultiple(key: 'buy_turnover' | 'sell_turnover' | 'turnover'): string {
  const value = performanceValue(key)
  return value === null ? '—' : `${value.toFixed(2)}×`
}

const excessAnnualReturn = computed(() => {
  const strategy = performanceValue('annual_return')
  const base = benchmark.value?.performance.annual_return ?? null
  return strategy === null || base === null ? null : strategy - base
})

// 净值曲线的抽稀/范围/点串计算在 utils/portfolioChart（纯函数），这里只喂当前数据。
const chartIndices = computed(() => equityChartIndices(props.result?.equity_curve.length ?? 0))
const chartRange = computed(() => {
  const base = benchmark.value?.totals
  return equityChartRange(
    chartIndices.value.map((index) => props.result?.equity_curve[index]?.total ?? 0),
    chartIndices.value.map((index) => (base && index < base.length ? base[index] : undefined)),
  )
})
const chartPoints = computed(() => equityChartPoints(
  chartIndices.value.map((index) => props.result?.equity_curve[index]?.total ?? 0),
  chartIndices.value.length,
  chartRange.value,
))
const benchmarkPoints = computed(() => {
  const base = benchmark.value?.totals
  if (!base) return ''
  return equityChartPoints(
    chartIndices.value.map((index) => (index < base.length ? base[index] : undefined)),
    chartIndices.value.length,
    chartRange.value,
  )
})
const chartSummary = computed(() => {
  const curve = props.result?.equity_curve ?? []
  if (!curve.length) return '暂无净值数据'
  const first = curve[0].total
  const last = curve[curve.length - 1].total
  return `组合净值从 ${formatMoney(first)} 变为 ${formatMoney(last)}，共 ${curve.length} 个交易日`
})
</script>

<template>
  <template v-if="result">
    <div class="backtest-summary"><span class="result-badge">{{ result!.single_security_mode ? '单股策略' : result!.universe === 'all' ? '沪深全部品种' : result!.universe.toUpperCase() }}</span><span>{{ result!.start_date }} → {{ result!.end_date }}</span><span>{{ result!.bars }} 个交易日</span><span>{{ result!.processed }} / {{ result!.total_candidates }} 个标的已处理</span><span>槽位 {{ result!.max_positions }}</span><span v-if="result!.ranking_value">排序：{{ valueName(result!.ranking_value) }} · {{ result!.rank_order === 'desc' ? '高到低' : '低到高' }}</span><span v-if="result!.fitness_rank_weight" class="result-badge">适配性权重 {{ Math.round(result!.fitness_rank_weight * 100) }}%</span></div>
    <aside class="portfolio-assumptions" data-testid="portfolio-assumptions" aria-label="回测口径与限制">
      <strong>回测口径与限制</strong>
      <span>价格口径：成交与持仓用{{ priceBasis === 'raw' ? '未复权真实价（raw）' : priceBasis === 'hfq' ? '后复权日线（hfq，旧记录）' : priceBasis }}，指标按分红送转事件校正</span>
      <ul><li v-for="limitation in limitations" :key="limitation">{{ limitation }}</li></ul>
    </aside>
    <div v-if="replayComparison" class="replay-comparison" data-testid="portfolio-replay-comparison"><strong>候选复核 {{ replayComparison.candidateHash.slice(0, 12) }}</strong><span>成交记录：{{ replayComparison.tradeRecords }}</span><span>绩效摘要：{{ replayComparison.performance }}</span><span>数据指纹：{{ replayComparison.dataFingerprint }}</span></div>
    <div class="metrics-strip portfolio-metrics">
      <div><span>总收益</span><strong :class="{ positive: (performanceValue('total_return') ?? 0) >= 0, negative: (performanceValue('total_return') ?? 0) < 0 }" data-testid="portfolio-total-return">{{ formatPercent(performanceValue('total_return')) }}</strong></div>
      <div><span>年化收益</span><strong :class="{ positive: (performanceValue('annual_return') ?? 0) >= 0, negative: (performanceValue('annual_return') ?? 0) < 0 }" data-testid="portfolio-annual-return">{{ formatPercent(performanceValue('annual_return')) }}</strong></div>
      <div><span>最大回撤</span><strong class="negative">{{ formatPercent(performanceValue('max_drawdown')) }}</strong></div>
      <div><span>夏普</span><strong>{{ formatNumber(performanceValue('sharpe')) }}</strong></div>
      <div><span>成交笔数</span><strong>{{ formatNumber(performanceValue('total_trades'), 0) }}</strong></div>
      <div><span>胜率</span><strong>{{ formatPercent(performanceValue('win_rate')) }}</strong></div>
      <div><span>期末资产</span><strong>{{ formatMoney(performanceValue('end_value')) }}</strong></div>
    </div>
    <details class="portfolio-trade-breakdown" data-testid="portfolio-trade-breakdown">
      <summary>买卖与交易质量指标</summary>
      <div class="portfolio-trade-breakdown-grid">
        <div><span>盈亏比</span><strong>{{ formatPortfolioRatio('payoff_ratio') }}</strong></div>
        <div><span>盈利因子</span><strong>{{ formatPortfolioRatio('profit_factor') }}</strong></div>
        <div><span>Sortino / Calmar</span><strong>{{ formatNumber(performanceValue('sortino')) }} / {{ formatNumber(performanceValue('calmar')) }}</strong></div>
        <div><span>年化波动率</span><strong>{{ formatPercent(performanceValue('volatility')) }}</strong></div>
        <div><span>最大回撤持续</span><strong>{{ formatNumber(performanceValue('max_dd_duration'), 0) }} 个交易日</strong></div>
        <div><span>盈利 / 非盈利笔</span><strong>{{ formatNumber(performanceValue('win_trades'), 0) }} / {{ formatNumber(performanceValue('lose_trades'), 0) }}</strong></div>
        <div><span>平均盈利 / 亏损</span><strong>{{ formatPercent(performanceValue('avg_win')) }} / {{ formatPercent(performanceValue('avg_loss') === null ? null : Math.abs(performanceValue('avg_loss') ?? 0)) }}</strong></div>
        <div><span>最大单笔盈利 / 亏损</span><strong>{{ formatPercent(performanceValue('max_win')) }} / {{ formatPercent(performanceValue('max_loss') === null ? null : Math.abs(performanceValue('max_loss') ?? 0)) }}</strong></div>
        <div><span>平均持仓天数</span><strong>{{ formatNumber(performanceValue('avg_holding_days'), 1) }}</strong></div>
        <div><span>持仓天数 盈利 / 亏损</span><strong>{{ formatNumber(performanceValue('win_holding_days'), 1) }} / {{ formatNumber(performanceValue('loss_holding_days'), 1) }}</strong></div>
        <div><span>最长单笔持仓</span><strong>{{ formatNumber(performanceValue('max_holding_days'), 1) }}</strong></div>
        <div><span>买入 / 卖出成交单</span><strong>{{ formatNumber(performanceValue('executed_buy_orders'), 0) }} / {{ formatNumber(performanceValue('executed_sell_orders'), 0) }}</strong></div>
        <div><span>买入 / 卖出换手</span><strong>{{ formatTurnoverMultiple('buy_turnover') }} / {{ formatTurnoverMultiple('sell_turnover') }}</strong></div>
        <div><span>总换手</span><strong>{{ formatTurnoverMultiple('turnover') }}</strong></div>
        <div><span>买入 / 卖出成本</span><strong>{{ formatMoney(performanceValue('buy_transaction_costs')) }} / {{ formatMoney(performanceValue('sell_transaction_costs')) }}</strong></div>
        <div><span>拒绝买单 / 卖单</span><strong>{{ formatNumber(performanceValue('rejected_buy_orders'), 0) }} / {{ formatNumber(performanceValue('rejected_sell_orders'), 0) }}</strong></div>
      </div>
      <p>盈亏比按卖出记录的平均盈利幅度 ÷ 平均亏损幅度计算；盈利因子按盈利总额 ÷ 亏损总额计算。买入是入场腿，已实现胜率与盈亏比按卖出记录统计，盈亏 ≤ 0 计为非盈利。买卖换手按各方向成交额 ÷ 初始资金计算，买卖成本包含佣金、印花税和滑点。</p>
    </details>
    <div id="portfolio-pnl-analysis-panel" v-if="showPnlAnalysis">
      <PortfolioPnlAnalysis :result="result" testid="portfolio-pnl-analysis" />
    </div>
    <div class="progress-track portfolio-progress" aria-label="组合数据处理进度"><span :style="{ width: `${Math.round((jobProgress ?? 1) * 100)}%` }"></span></div>

    <section v-if="benchmark" class="benchmark-card" data-testid="portfolio-benchmark" aria-labelledby="portfolio-benchmark-title"><div class="benchmark-heading"><div><span class="section-kicker">BENCHMARK</span><h3 id="portfolio-benchmark-title">基准对照<TermHint term="benchmark" /></h3></div><span class="muted-code">{{ benchmark.label }} · {{ benchmark.instrument_count }} 只标的</span></div><div class="metrics-strip benchmark-metrics"><div><span>基准总收益</span><strong data-testid="portfolio-benchmark-total">{{ formatPercent(benchmarkValue('total_return')) }}</strong></div><div><span>基准年化</span><strong data-testid="portfolio-benchmark-annual">{{ formatPercent(benchmarkValue('annual_return')) }}</strong></div><div><span>基准最大回撤</span><strong class="negative" data-testid="portfolio-benchmark-drawdown">{{ formatPercent(benchmarkValue('max_drawdown')) }}</strong></div><div><span>基准夏普</span><strong data-testid="portfolio-benchmark-sharpe">{{ formatNumber(benchmarkValue('sharpe')) }}</strong></div><div><span>超额年化<TermHint term="excessReturn" /></span><strong :class="{ positive: (excessAnnualReturn ?? 0) > 0, negative: (excessAnnualReturn ?? 0) <= 0 }" data-testid="portfolio-excess-annual">{{ formatPercent(excessAnnualReturn) }}</strong></div></div><p class="benchmark-note">基准用同一笔本金、同一段区间，第一天按收盘价把池内标的各买一点，之后一直不动；不计手续费也不取整手，因为它是用来比的尺子，不是真去执行的策略。第一天还没有价格的标的（新股、长期停牌）不进基准。</p></section>

    <section class="chart-card" aria-labelledby="portfolio-equity-title"><div class="chart-heading"><div><span class="section-kicker">EQUITY CURVE</span><h3 id="portfolio-equity-title">组合资金曲线</h3></div><span class="chart-legend"><i aria-hidden="true"></i>总资产<template v-if="benchmark"><i class="legend-benchmark" aria-hidden="true"></i>基准</template></span></div><svg class="equity-chart" data-testid="portfolio-equity-chart" viewBox="0 0 800 240" role="img" :aria-label="chartSummary"><polyline v-if="benchmarkPoints" :points="benchmarkPoints" fill="none" stroke="var(--muted)" stroke-linecap="round" stroke-linejoin="round" stroke-width="2" stroke-dasharray="6 4" data-testid="portfolio-benchmark-chart"></polyline><polyline :points="chartPoints" fill="none" stroke="var(--accent)" stroke-linecap="round" stroke-linejoin="round" stroke-width="3"></polyline></svg><p class="chart-summary">{{ chartSummary }}</p></section>

    <section class="data-section"><div class="section-heading"><div><span class="section-kicker">CURRENT HOLDINGS</span><h3>最新持仓</h3></div><span>{{ latestState?.positions_count ?? 0 }} / {{ result!.max_positions }} 个槽位</span></div><div v-if="!latestState?.holdings.length" class="inline-empty">回测结束时没有持仓。</div><div v-else class="table-wrap"><table data-testid="portfolio-holdings"><caption class="sr-only">组合最新持仓</caption><thead><tr><th>市场</th><th>代码</th><th>中文名</th><th>数量</th><th>成本</th><th>收盘</th><th>浮动盈亏</th></tr></thead><tbody><tr v-for="holding in latestState.holdings" :key="`${holding.market}-${holding.code}`"><td><span class="market-pill" :class="holding.market.toLowerCase()">{{ holding.market }}</span></td><td><strong class="code">{{ holding.code }}</strong></td><td>{{ holding.name || '—' }}</td><td>{{ formatNumber(holding.size, 0) }}</td><td>{{ formatMoney(holding.entry_price) }}</td><td>{{ formatMoney(holding.close) }}</td><td :class="{ positive: holding.unrealized_pnl >= 0, negative: holding.unrealized_pnl < 0 }">{{ formatMoney(holding.unrealized_pnl) }}</td></tr></tbody></table></div></section>

    <section class="data-section"><div class="section-heading"><div><span class="section-kicker">TRADES</span><h3>成交记录</h3></div><span>最近 {{ recentTrades.length }} 笔</span></div><div v-if="!recentTrades.length" class="inline-empty">区间内没有形成可执行成交。</div><div v-else class="table-wrap"><table data-testid="portfolio-trades"><caption class="sr-only">组合成交记录</caption><thead><tr><th>成交日期</th><th>标的</th><th>方向</th><th>数量</th><th>价格</th><th>盈亏</th><th>原因</th></tr></thead><tbody><tr v-for="(trade, index) in recentTrades" :key="`${trade.date}-${trade.code}-${index}`"><td class="muted-code">{{ trade.date }}</td><td><span v-if="trade.name">{{ trade.name }} · </span><span class="market-pill" :class="trade.market.toLowerCase()">{{ trade.market }}</span> <strong class="code">{{ trade.code }}</strong></td><td><span class="direction" :class="trade.direction.toLowerCase()">{{ trade.direction === 'BUY' ? '买入' : '卖出' }}</span></td><td>{{ formatNumber(trade.size, 0) }}</td><td>{{ formatMoney(trade.price) }}</td><td :class="{ positive: trade.pnl >= 0, negative: trade.pnl < 0 }">{{ formatMoney(trade.pnl) }}</td><td>{{ trade.reason }}</td></tr></tbody></table></div></section>

    <section class="data-section"><div class="section-heading"><div><span class="section-kicker">LATEST RANKING</span><h3>最近候选排名</h3></div><span>{{ latestRanking?.date ?? '—' }}</span></div><div v-if="!latestRanking?.candidates.length" class="inline-empty">没有可展示的排名候选。</div><div v-else class="table-wrap"><table data-testid="portfolio-ranking"><caption class="sr-only">最近候选排名</caption><thead><tr><th>排名</th><th>标的</th><th>当前指标 / 综合分</th><th>适配性</th><th>动作</th></tr></thead><tbody><tr v-for="candidate in latestRanking.candidates" :key="`${candidate.market}-${candidate.code}`"><td class="muted-code">{{ candidate.rank }}</td><td><span v-if="candidate.name">{{ candidate.name }} · </span><span class="market-pill" :class="candidate.market.toLowerCase()">{{ candidate.market }}</span> <strong class="code">{{ candidate.code }}</strong></td><td>{{ formatNumber(candidate.score, 4) }}<small v-if="candidate.rank_score !== undefined" class="muted-code"> · {{ formatNumber(candidate.rank_score, 3) }}</small></td><td v-if="candidate.fitness_score !== undefined" :title="candidate.fitness_note ?? ''"><strong>{{ formatNumber(candidate.fitness_score, 2) }}</strong><small class="muted-code"> · {{ candidate.fitness_trades ?? 0 }} 笔</small></td><td v-else class="muted-code">未启用</td><td><span class="result-badge" v-if="candidate.selected">进入候选槽位</span><span class="muted-code" v-else>等待补位</span></td></tr></tbody></table></div></section>

    <section class="data-section"><div class="section-heading"><div><span class="section-kicker">RECENT EQUITY</span><h3>最近净值</h3></div><span>最近 {{ sampledEquity.length }} 日</span></div><div class="table-wrap"><table data-testid="portfolio-equity-table"><caption class="sr-only">组合最近资金曲线数据</caption><thead><tr><th>日期</th><th>现金</th><th>持仓市值</th><th>总资产</th><th>持仓数</th><th>回撤</th></tr></thead><tbody><tr v-for="point in sampledEquity" :key="point.date"><td class="muted-code">{{ point.date }}</td><td>{{ formatMoney(point.cash) }}</td><td>{{ formatMoney(point.position_value) }}</td><td><strong>{{ formatMoney(point.total) }}</strong></td><td>{{ point.positions_count }}</td><td>{{ formatPercent(point.drawdown_pct) }}</td></tr></tbody></table></div></section>
    <div v-if="result!.failure_reasons && Object.keys(result!.failure_reasons).length" class="failure-summary"><strong>失败摘要</strong><span v-for="(count, reason) in result!.failure_reasons" :key="reason">{{ reason }} · {{ count }}</span></div>
    <div v-if="droppedCodes.length" class="failure-summary" data-testid="portfolio-dropped-codes"><strong>未参与回测的指定标的</strong><span v-for="item in droppedCodes" :key="item.code">{{ item.code }} · {{ item.reason }}</span></div>
    <p v-if="result!.diagnostic" class="diagnostic" data-testid="portfolio-diagnostic">{{ result!.diagnostic }}</p>
  </template>
</template>

