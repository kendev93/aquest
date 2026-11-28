<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import PageGuide from '../components/PageGuide.vue'
import IndividualResearchBacktestDetail from '../components/IndividualResearchBacktestDetail.vue'
import { fetchMetadata } from '../api/formulaScreen'
import { buildIndicatorNameMap, indicatorDisplayName } from '../utils/indicatorNames'
import { isTerminalJobStatus } from '../utils/jobStatus'
import { holdingRiskSummary } from '../utils/strategyPoolStatus'
import { formatRiskControls, formatTradingRule } from '../utils/strategyRules'
import type { HoldingRiskDisplay, IndividualResearchFullBacktest, StrategyPoolCandidate, StrategyPoolObjective } from '../types'
import AppTopbar from '../components/AppTopbar.vue'

interface Version {
  version_id: string; created_at: string; stopped_at?: string; source: string; status: string; last_error?: string;
  candidate: StrategyPoolCandidate; summary: Record<string, unknown>; is_current: boolean
}
const segments = window.location.pathname.split('/')
const market = segments[2] ?? ''; const code = segments[3] ?? ''
const query = new URLSearchParams(window.location.search)
const objective = ref<StrategyPoolObjective>(query.get('objective') === 'sharpe' ? 'sharpe' : 'annual_return')
// 收益曲线区间：默认「全部区间」；不读 URL，保持每次打开行为一致
type PoolCurveRange = '1y' | 'ytd' | '2y' | '3y' | 'all' | 'created'
const RANGE_OPTIONS: { key: PoolCurveRange; label: string }[] = [
  { key: '1y', label: '最近一年' },
  { key: 'ytd', label: '今年以来' },
  { key: '2y', label: '最近两年' },
  { key: '3y', label: '最近三年' },
  { key: 'all', label: '全部区间' },
  { key: 'created', label: '成立以来' },
]
const range = ref<PoolCurveRange>('1y')
interface CurveRangeSummary {
  key: string; label: string; start_date: string | null; end_date: string | null
  total_return: number | null; max_drawdown: number | null
}
// 六个区间的收益与回撤由后端一次算全：点按钮只换曲线，不重新请求这张表。
const ranges = ref<CurveRangeSummary[]>([])
interface CurveExitSummary { key: string; label: string; trades: number; pnl: number }
// 出场原因分布跟着所选区间走：窗口内的卖出成交按规则/止损/止盈/移动止盈/到期分类。
const exits = ref<CurveExitSummary[]>([])
interface CurveYearRow { year: number; start_date: string; end_date: string
  total_return: number | null; max_drawdown: number | null }
interface CurveAnalysis {
  exposure: number | null; max_consecutive_losses: number; max_consecutive_wins: number
  top3_profit_share: number | null; cost_total: number; cost_share: number | null
  yearly: CurveYearRow[]
}
// 行为与成本指标同样是窗口口径：在场时间、连亏连盈、盈利集中度、成本占比、年度表现。
const analysis = ref<CurveAnalysis | null>(null)
const metricsVisible = computed(() => range.value === 'all' || range.value === 'created')
// 历史交易默认最近成交在前：关注的是刚发生的买卖，不是 2015 年的第一笔
const tradeOrder = ref<'desc' | 'asc'>('desc')
const detail = ref<{ name: string; objectives: Record<StrategyPoolObjective, Version[]> } | null>(null)
const selectedId = ref('')
const versions = computed(() => detail.value?.objectives[objective.value] ?? [])
const selected = computed(() => versions.value.find((v) => v.version_id === selectedId.value) ?? null)
const automatic = computed(() => versions.value.find((v) => v.is_current && v.source === 'automatic'))
const appliedOverride = computed(() => versions.value.find((v) => v.is_current && v.source === 'manual'))
const backtest = ref<IndividualResearchFullBacktest | null>(null)
const summary = ref<Record<string, unknown>>({})
const names = ref(new Map<string, string>())
// 跟踪快照的持仓行：既有展示用的价格字段，也有引擎算出的风控线与持有到期。
type TrackedHolding = HoldingRiskDisplay & Record<string, unknown>
const holdings = computed(
  () => (Array.isArray(summary.value.holding) ? summary.value.holding as TrackedHolding[] : []),
)
const error = ref(''); const loading = ref(false); const offset = ref(0); const total = ref(0)
const initializing = ref(false); const initError = ref(''); const autoInitFor = ref('')
// 每页笔数与 /trades 请求里的 limit 用同一个常量，分页按钮的位移也按它算
const TRADE_PAGE_SIZE = 50
const tradePage = computed(() => Math.floor(offset.value / TRADE_PAGE_SIZE) + 1)
const tradePageCount = computed(() => Math.max(1, Math.ceil(total.value / TRADE_PAGE_SIZE)))
const tradeRangeText = computed(() => total.value
  ? `${offset.value + 1}–${Math.min(offset.value + TRADE_PAGE_SIZE, total.value)} 笔`
  : '暂无成交')
const returnHref = computed(() => `/strategy-pool${query.get('list') ? `?${query.get('list')}` : ''}`)
const endpoint = `/api/v1/strategy-pool/entries/${encodeURIComponent(market)}/${encodeURIComponent(code)}`
let requestId = 0
async function get<T>(url: string): Promise<T> {
  const response = await fetch(url)
  const payload = await response.json()
  if (!response.ok) throw new Error(payload.detail ?? '详情加载失败')
  return payload.data as T
}
async function post<T>(url: string, body: unknown): Promise<T> {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  const payload = await response.json()
  if (!response.ok) throw new Error(payload.detail ?? payload.error?.message ?? '任务提交失败')
  return payload.data as T
}
function percent(value: unknown) { return typeof value === 'number' && Number.isFinite(value) ? `${(value * 100).toFixed(2)}%` : '—' }
function signedPercent(value: unknown) { return typeof value === 'number' && Number.isFinite(value) ? `${value >= 0 ? '+' : ''}${(value * 100).toFixed(2)}%` : '—' }
function number(value: unknown, digits = 2) { return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(digits) : '—' }
function days(value: unknown) { return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(1) : '—' }
function money(value: unknown) { return typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString('zh-CN', { maximumFractionDigits: 0 }) : '—' }
const performance = computed(() => (backtest.value?.performance ?? {}) as Record<string, unknown>)
function date(value: string | undefined) { return value ? new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' }) : '—' }
function indicator(id: string) { return indicatorDisplayName(names.value, id) }
async function selectGoal(goal: StrategyPoolObjective) {
  objective.value = goal; offset.value = 0
  const requested = new URLSearchParams(window.location.search).get('version')
  selectedId.value = (requested && versions.value.some(v => v.version_id === requested) ? requested : '') || (appliedOverride.value?.version_id ?? automatic.value?.version_id ?? versions.value[0]?.version_id ?? '')
  await loadVersion()
}
// 曲线只跟「版本 + 区间」有关，成交明细是按 offset 切的切片。以前翻页也走整块
// 重取，每点一次下一页都要重拉曲线并重建图，看起来就是页面在闪。
function tradesUrl(): string {
  return `${endpoint}/versions/${selectedId.value}/trades?range=${range.value}&order=${tradeOrder.value}&offset=${offset.value}&limit=${TRADE_PAGE_SIZE}`
}
async function loadVersion() {
  const params = new URLSearchParams(window.location.search)
  params.delete('scope') // 清理旧版本残留的 scope 参数
  params.set('objective',objective.value); params.set('version',selectedId.value)
  window.history.replaceState({},'',window.location.pathname+'?'+params.toString())
  const current = ++requestId
  backtest.value = null; summary.value = {}; error.value = ''
  if (!selectedId.value) return
  loading.value = true
  try {
    const [curve, trades] = await Promise.all([
      get<{ backtest: IndividualResearchFullBacktest | null; summary?: Record<string, unknown>; ranges?: CurveRangeSummary[]; exits?: CurveExitSummary[]; analysis?: CurveAnalysis | null }>(`${endpoint}/versions/${selectedId.value}/curve?range=${range.value}`),
      get<{ items: Record<string, unknown>[]; total: number }>(tradesUrl()),
    ])
    if (current !== requestId) return
    backtest.value = curve.backtest ? { ...curve.backtest, trades: trades.items } : null
    summary.value = curve.summary ?? {}; total.value = trades.total
    ranges.value = curve.ranges ?? []
    exits.value = curve.exits ?? []
    analysis.value = curve.analysis ?? null
  } catch (e) { if (current === requestId) error.value = e instanceof Error ? e.message : '详情加载失败' }
  finally { if (current === requestId) loading.value = false }
  // 研究只生成策略版本；当前版本还没有模拟账户快照时，打开详情就自动为这一只
  // 补一次初始化（通常几秒），同一个版本每次打开只自动试一次，失败可手动重试。
  if (
    current === requestId
    && !backtest.value
    && selected.value?.is_current
    && autoInitFor.value !== selected.value.version_id
  ) {
    autoInitFor.value = selected.value.version_id
    void initializeAccount()
  }
}

/** 只回放这一只股票，给刚研究完、还没有模拟账户快照的版本补一份。 */
async function initializeAccount(): Promise<void> {
  if (initializing.value || !selected.value?.is_current) return
  initializing.value = true
  initError.value = ''
  try {
    const created = await post<{ job_id: string; status: string }>(
      '/api/v1/strategy-pool/jobs',
      { mode: 'signals', objective: objective.value, market, code },
    )
    if (!created.job_id) throw new Error('初始化任务没有创建成功')
    let finished = false
    for (let attempt = 0; attempt < 120; attempt += 1) {
      const state = await get<{ status: string; error?: string }>(
        `/api/v1/strategy-pool/jobs/${created.job_id}`,
      )
      if (isTerminalJobStatus(state.status)) {
        if (state.status !== 'completed') throw new Error(state.error || '初始化任务失败')
        finished = true
        break
      }
      await new Promise((resolve) => window.setTimeout(resolve, 1_500))
    }
    if (!finished) throw new Error('初始化任务超时，请到策略池任务记录里查看进度')
    await loadVersion()
  } catch (e) {
    initError.value = e instanceof Error ? e.message : '初始化失败'
  } finally {
    initializing.value = false
  }
}

async function refreshTrades(): Promise<void> {
  if (!selectedId.value) return
  const current = ++requestId
  error.value = ''
  loading.value = true
  try {
    const data = await get<{ items: Record<string, unknown>[]; total: number }>(tradesUrl())
    // 期间换了版本或区间就丢弃这次结果；曲线还没就绪时没有可打的补丁。
    if (current !== requestId || !backtest.value) return
    backtest.value = { ...backtest.value, trades: data.items }
    total.value = data.total
  } catch (e) {
    if (current === requestId) error.value = e instanceof Error ? e.message : '成交明细加载失败'
  } finally { if (current === requestId) loading.value = false }
}

function stepTrades(delta: number): void {
  offset.value = Math.max(0, offset.value + delta)
  void refreshTrades()
}

function selectRange(next: PoolCurveRange): void {
  if (next === range.value) return
  range.value = next; offset.value = 0
  void loadVersion()
}
onMounted(async () => {
  try {
    const [data, metadata] = await Promise.all([get<NonNullable<typeof detail.value>>(`${endpoint}/detail`), fetchMetadata().catch(() => null)])
    detail.value = data; names.value = buildIndicatorNameMap(metadata)
    await selectGoal(objective.value)
  } catch (e) { error.value = e instanceof Error ? e.message : '详情加载失败' }
})

// 版本状态是研究侧词表（pending/ready/failed），与标的状态不是一套，别混用 statusLabel
const VERSION_STATUS_LABELS: Record<string, string> = { pending: '待初始化', ready: '已就绪', failed: '更新失败' }
function versionStatus(value: string): string { return VERSION_STATUS_LABELS[value] ?? value }

const scopeLabel = computed(
  () => RANGE_OPTIONS.find((option) => option.key === range.value)?.label ?? '',
)
</script>

<template>
    <AppTopbar />
  <main class="content-shell pool-detail" data-testid="pool-detail-page">
    <a class="text-button detail-back" data-testid="pool-detail-back" :href="returnHref">← 返回全市场标的状态</a>
    <header class="detail-head">
      <p class="eyebrow">STRATEGY POOL · DETAIL</p>
      <h1>{{ market }} {{ code }} · {{ detail?.name }} 策略详情</h1>
      <p class="detail-note">创建前为历史回测，创建后为模拟跟踪；只用已完成的行情，打开详情不会启动扫描。</p>
    </header>
    <PageGuide page-key="strategy-pool" />
    <div class="segmented-tabs" role="group" aria-label="选择策略目标"><button :aria-pressed="objective === 'annual_return'" data-testid="detail-annual" @click="selectGoal('annual_return')">年化最优</button><button :aria-pressed="objective === 'sharpe'" data-testid="detail-sharpe" @click="selectGoal('sharpe')">夏普最优</button></div>
    <p v-if="appliedOverride" class="detail-note">当前实际生效的是手工覆盖策略，列表收益和信号来自该版本。可切换到自动最优版本查看参考表现。</p>
    <div v-if="versions.length" class="detail-controls">
      <label>跟踪版本 <select v-model="selectedId" @change="offset = 0; loadVersion()"><option v-for="version in versions" :key="version.version_id" :value="version.version_id">{{ version.source === 'manual' ? '手工覆盖' : '自动最优' }} · {{ date(version.created_at) }} · {{ version.is_current ? '当前版本' : '已归档' }}</option></select></label>
      <label>历史交易排序 <select v-model="tradeOrder" data-testid="pool-trade-order" @change="offset = 0; refreshTrades()"><option value="desc">最近成交在前</option><option value="asc">最早成交在前</option></select></label>
    </div>
    <p v-if="!versions.length" class="empty-copy">该目标暂无可用策略。请在策略池研究完成后查看。</p>
    <p v-if="error" class="notice error" role="alert">{{ error }}</p>
    <section v-if="selected" class="panel-card">
      <div class="section-heading"><div><p class="eyebrow">RULES</p><h2>交易规则</h2></div><span>状态 {{ versionStatus(selected.status) }} · 行情截至 {{ summary.as_of_date ?? selected.summary.as_of_date ?? '待初始化' }}</span></div>
      <div class="rules"><div><h3>买入规则</h3><p>{{ formatTradingRule(selected.candidate.buy_rule, indicator) }}</p></div><div><h3>卖出规则</h3><p>{{ formatTradingRule(selected.candidate.sell_rule, indicator) }}</p></div><div><h3>退出风控</h3><p>{{ formatRiskControls(selected.candidate.config) }}</p></div></div>
      <p>创建时间 {{ date(selected.created_at) }}<span v-if="selected.stopped_at"> · 停用时间 {{ date(selected.stopped_at) }}</span></p>
      <p v-if="selected.last_error" role="alert">更新失败：{{ selected.last_error }}。保留最后成功结果。</p>
      <div class="metrics"><span>{{ selected.is_current ? '创建至今收益' : '截至停用收益' }} <b>{{ percent(summary.created_return) }}</b></span><span>上线后收益 <b>{{ percent(summary.post_launch_return) }}</b></span></div>
      <p v-if="summary.inherited_position">含跨创建日持仓，收益统计起点为原买入日 {{ summary.baseline_date }}；不包含更早已完成交易的收益。</p>
      <p v-if="summary.as_of_date && summary.has_post_launch_data === false">尚无创建后的新行情。</p>
      <div v-if="holdings.length"><p v-for="(holding, index) in holdings" :key="index" data-testid="pool-detail-holding">持仓 {{ holding.size }} 股 · 买入日期 {{ holding.entry_date }} · 买入价 {{ holding.entry_price }} · 当前价 {{ holding.close ?? '—' }} · 浮动收益 {{ percent(holding.unrealized_pnl_pct) }} · {{ holdingRiskSummary(holding) }}</p></div>
      <p v-else-if="summary.as_of_date">当前空仓</p>
      <p v-if="summary.recomputed_at">行情校正后于 {{ summary.recomputed_at }} 重算，创建时间保持不变。</p>
    </section>
    <section v-if="selected" class="panel-card" :aria-busy="loading">
      <div class="section-heading"><div><p class="eyebrow">CURVE &amp; TRADES</p><h2>收益曲线与历史交易</h2></div><span data-testid="pool-curve-state">{{ loading ? '读取中…' : `${scopeLabel} · 成交明细每页 ${TRADE_PAGE_SIZE} 笔` }}</span></div>
      <div class="curve-range">
        <div class="segmented-tabs" role="group" aria-label="收益曲线区间"><button v-for="option in RANGE_OPTIONS" :key="option.key" :aria-pressed="range === option.key" :data-testid="`pool-range-btn-${option.key}`" @click="selectRange(option.key)">{{ option.label }}</button></div>
      </div>
      <!-- 「尚未初始化」只在真的读完且没有数据时说，以前加载途中也会闪这句 -->
      <p v-if="!loading && !backtest" class="empty-copy" data-testid="pool-detail-not-initialized">账户尚未初始化：研究只生成策略版本，模拟账户由信号跟踪任务创建。可以点下方按钮只初始化这一只，或回全市场策略池点「刷新最新交易日信号」刷新全市场。</p>
      <div v-if="!loading && !backtest && selected?.is_current" class="detail-initialize">
        <button class="button-secondary" data-testid="pool-init-account" type="button" :disabled="initializing" @click="initializeAccount">{{ initializing ? '正在初始化…' : '初始化该股模拟账户' }}</button>
        <small>{{ initializing ? '正在为该股创建模拟账户，通常几秒到一分钟…' : '打开详情时会自动初始化这一只；若失败可点按钮重试，或回策略池刷新全市场信号。' }}</small>
      </div>
      <p v-if="initError" class="notice error" role="alert">{{ initError }}</p>
      <IndividualResearchBacktestDetail v-if="backtest" :backtest="backtest" :launch-date="selected.created_at" :baseline-date="String(summary.baseline_date ?? '')" hide-metrics return-curve>
        <template #after-chart>
        <div class="detail-metrics">
          <div v-if="metricsVisible && backtest" class="metrics-grid" data-testid="pool-metrics">
            <div class="metric-group"><h3>收益</h3>
              <p><span>策略收益</span><b>{{ percent(performance.total_return) }}</b></p>
              <p><span>持有基准</span><b>{{ signedPercent(backtest.benchmark_return) }}</b></p>
              <p><span>超额收益</span><b>{{ signedPercent(backtest.excess_return) }}</b></p>
              <p><span>年化</span><b>{{ percent(performance.annual_return) }}</b></p>
              <p><span>夏普</span><b>{{ number(performance.sharpe, 3) }}</b></p>
              <p><span>Calmar</span><b>{{ number(performance.calmar) }}</b></p>
            </div>
            <div class="metric-group"><h3>风险</h3>
              <p><span>最大回撤</span><b>{{ percent(performance.max_drawdown) }}</b></p>
              <p><span>回撤持续</span><b>{{ number(performance.max_dd_duration, 0) }} 个交易日</b></p>
              <p><span>年化波动</span><b>{{ percent(performance.volatility) }}</b></p>
              <p><span>Sortino</span><b>{{ number(performance.sortino) }}</b></p>
            </div>
            <div class="metric-group"><h3>交易</h3>
              <p><span>成交笔数</span><b>{{ number(performance.total_trades, 0) }}</b></p>
              <p><span>胜率</span><b>{{ percent(performance.win_rate) }}</b></p>
              <p><span>盈亏比</span><b>{{ number(performance.payoff_ratio) }}</b></p>
              <p><span>盈亏因子</span><b>{{ number(performance.profit_factor) }}</b></p>
              <p><span>平均持仓</span><b>{{ days(performance.avg_holding_days) }} 天（自然日）</b></p>
            </div>
          </div>
          <div class="metric-tables">
            <table v-if="ranges.length" class="range-summary" data-testid="pool-range-summary">
              <thead><tr><th>区间</th><th>收益率</th><th>最大回撤</th></tr></thead>
              <tbody>
                <tr v-for="item in ranges" :key="item.key" :class="{ active: item.key === range }" :data-testid="`pool-range-${item.key}`" @click="selectRange(item.key as PoolCurveRange)">
                  <td>{{ item.label }}</td><td>{{ percent(item.total_return) }}</td><td>{{ percent(item.max_drawdown) }}</td>
                </tr>
              </tbody>
            </table>
            <table v-if="exits.length" class="exit-summary" data-testid="pool-exit-summary">
              <thead><tr><th>出场原因</th><th>笔数</th><th>累计盈亏</th></tr></thead>
              <tbody><tr v-for="item in exits" :key="item.key" :data-testid="`pool-exit-${item.key}`"><td>{{ item.label }}</td><td>{{ item.trades }}</td><td>{{ money(item.pnl) }}</td></tr></tbody>
            </table>
            <dl v-if="analysis" class="behavior-stats" data-testid="pool-behavior-stats">
              <div><dt>在场时间</dt><dd>{{ percent(analysis.exposure) }}</dd></div>
              <div><dt>最长连亏 / 连盈</dt><dd>{{ analysis.max_consecutive_losses }} / {{ analysis.max_consecutive_wins }} 笔</dd></div>
              <div><dt>前 3 笔盈利占比</dt><dd>{{ percent(analysis.top3_profit_share) }}</dd></div>
              <div><dt>交易成本占比</dt><dd>{{ percent(analysis.cost_share) }}</dd></div>
            </dl>
          </div>
        </div>
        <table v-if="analysis?.yearly.length" class="yearly-summary" data-testid="pool-yearly-summary">
          <thead><tr><th>年份</th><th>收益</th><th>年内最大回撤</th><th>区间</th></tr></thead>
          <tbody><tr v-for="row in analysis.yearly" :key="row.year"><td>{{ row.year }}</td><td>{{ percent(row.total_return) }}</td><td>{{ percent(row.max_drawdown) }}</td><td>{{ row.start_date }} → {{ row.end_date }}</td></tr></tbody>
        </table>
        </template>
        <!-- 分页紧贴交易表：以前它在这块面板外面，看 50 笔要先在表里滚一遍、再往下找按钮 -->
        <template #trades-toolbar>
          <nav class="trades-pager" data-testid="pool-trade-pager">
            <button class="button-secondary" :disabled="offset === 0 || loading" data-testid="pool-trade-prev" @click="stepTrades(-TRADE_PAGE_SIZE)">上一页</button>
            <span>第 {{ tradePage }} / {{ tradePageCount }} 页 · 显示 {{ tradeRangeText }} / 共 {{ total }} 笔</span>
            <button class="button-secondary" :disabled="offset + TRADE_PAGE_SIZE >= total || loading" data-testid="pool-trade-next" @click="stepTrades(TRADE_PAGE_SIZE)">下一页</button>
          </nav>
        </template>
      </IndividualResearchBacktestDetail>
    </section>
  </main>
</template>
<style scoped>
.pool-detail { display:grid; grid-template-columns:minmax(0, 1fr); gap:18px; padding:24px; max-width:1400px; margin:auto; color:var(--text); }
/* 这页以前挂着 panel-card 却没定义它，两个面板其实就是裸内容堆在背景上 */
.panel-card { padding:22px; border:1px solid #25364f; background:rgba(12,20,36,.78); }
.detail-head { display:grid; gap:8px; }
.detail-note, .empty-copy { margin:0; color:#8798b1; font-size:.9rem; line-height:1.6; }
.detail-initialize { display:flex; flex-wrap:wrap; align-items:center; gap:10px 14px; margin-top:10px; color:var(--muted); font-size:12px; }
.detail-controls { display:flex; flex-wrap:wrap; align-items:end; gap:14px 24px; padding:16px; border:1px solid #25364f; background:rgba(12,20,36,.6); }
.detail-controls label { display:grid; gap:6px; color:var(--muted); font-size:.88rem; }
.detail-controls select { width:auto; min-width:240px; }
.section-heading h2 { margin:6px 0 0; }
.metrics, nav { display:flex; gap:24px; flex-wrap:wrap; }
.rules { display:grid; grid-template-columns:repeat(3,1fr); gap:24px; }
.rules p { white-space:pre-wrap; }
.detail-back { display:inline-flex; align-items:center; justify-self:start; font-size:11px; }
.curve-range { margin-bottom:12px; }
.range-summary { width:100%; border-collapse:collapse; font-size:11px; }
.range-summary th, .range-summary td { padding:5px 10px; border-bottom:1px solid var(--border); text-align:right; white-space:nowrap; }
.range-summary th:first-child, .range-summary td:first-child { white-space:normal; }
.range-summary th:first-child, .range-summary td:first-child { text-align:left; color:var(--muted); }
.range-summary th { color:var(--subtle); font-weight:700; }
.range-summary tbody tr { cursor:pointer; }
.range-summary tbody tr:hover { background:var(--surface-hover); }
.range-summary tbody tr.active { background:var(--accent-soft); color:var(--accent); }
.detail-metrics { display:grid; gap:16px; margin-bottom:14px; }
.metrics-grid { display:grid; grid-template-columns:repeat(3, minmax(0, 240px)); gap:12px 28px; }
.metric-group { min-width:0; }
/* 区间表、出场原因、行为指标统一排在指标组下面；宽屏并排，窄屏自动堆叠。 */
.metric-tables { display:grid; grid-template-columns:repeat(auto-fit, minmax(280px, 1fr)); align-items:start; gap:16px 24px; }
/* 表格用固定布局随列宽收缩：nowrap 的单元格不会再把整块顶出面板。 */
.metric-tables > * { min-width:0; }
.range-summary, .exit-summary, .yearly-summary { table-layout:fixed; }
.metric-group h3 { margin:0 0 6px; color:var(--subtle); font-size:11px; letter-spacing:.12em; }
.metric-group p { display:flex; justify-content:space-between; gap:12px; margin:3px 0; color:var(--muted); font-size:11px; }
.metric-group b { color:var(--text); font-weight:700; }
.exit-summary, .yearly-summary { width:100%; max-width:100%; border-collapse:collapse; font-size:11px; }
.yearly-summary { margin-bottom:14px; min-width:0; }
.yearly-summary th, .yearly-summary td { padding:5px 10px; border-bottom:1px solid var(--border); text-align:right; white-space:nowrap; }
.yearly-summary th:first-child, .yearly-summary td:first-child { text-align:left; color:var(--muted); }
.yearly-summary th { color:var(--subtle); font-weight:700; }
.yearly-summary td:last-child, .yearly-summary th:last-child { text-align:left; color:var(--subtle); white-space:normal; }
.behavior-stats { display:grid; gap:4px; margin:0; }
.behavior-stats div { display:flex; justify-content:space-between; gap:12px; color:var(--muted); font-size:11px; }
.behavior-stats dt { font-weight:400; }
.behavior-stats dd { margin:0; color:var(--text); font-weight:700; }
.exit-summary th, .exit-summary td { padding:5px 10px; border-bottom:1px solid var(--border); text-align:right; white-space:nowrap; }
.exit-summary th:first-child, .exit-summary td:first-child { text-align:left; color:var(--muted); }
.exit-summary th { color:var(--subtle); font-weight:700; }
.trades-pager { align-items:center; gap:12px; }
.trades-pager .button-secondary { min-height:31px; padding:0 10px; font-size:11px; }
.trades-pager span { color:var(--muted); }
/* 组件里 340px 的窗口是给不分页的个股研究用的；详情页每页只有 50 笔且有分页，
   再压高度就是让用户在表格里二次滚动 */
.pool-detail :deep(.individual-trades) { max-height:620px; }
@media (max-width:800px) { .rules { grid-template-columns:1fr; } }
@media (max-width:720px) { .metrics-grid { grid-template-columns:minmax(0, 1fr); } }
</style>
