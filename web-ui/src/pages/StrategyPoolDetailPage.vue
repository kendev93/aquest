<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import PageGuide from '../components/PageGuide.vue'
import IndividualResearchBacktestDetail from '../components/IndividualResearchBacktestDetail.vue'
import { fetchMetadata } from '../api/formulaScreen'
import { buildIndicatorNameMap, indicatorDisplayName } from '../utils/indicatorNames'
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
// 查看区间固定默认“全历史”；不读 URL，保持每次打开行为一致
const scope = ref<'created' | 'full'>('full')
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
function percent(value: unknown) { return typeof value === 'number' && Number.isFinite(value) ? `${(value * 100).toFixed(2)}%` : '—' }
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
  return `${endpoint}/versions/${selectedId.value}/trades?scope=${scope.value}&order=${tradeOrder.value}&offset=${offset.value}&limit=${TRADE_PAGE_SIZE}`
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
      get<{ backtest: IndividualResearchFullBacktest | null; summary?: Record<string, unknown> }>(`${endpoint}/versions/${selectedId.value}/curve?scope=${scope.value}`),
      get<{ items: Record<string, unknown>[]; total: number }>(tradesUrl()),
    ])
    if (current !== requestId) return
    backtest.value = curve.backtest ? { ...curve.backtest, trades: trades.items } : null
    summary.value = curve.summary ?? {}; total.value = trades.total
  } catch (e) { if (current === requestId) error.value = e instanceof Error ? e.message : '详情加载失败' }
  finally { if (current === requestId) loading.value = false }
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

const SCOPE_LABELS: Record<'created' | 'full', string> = { created: '创建以来（含承接持仓）', full: '全历史' }
const scopeLabel = computed(() => SCOPE_LABELS[scope.value])
</script>

<template>
    <AppTopbar />
  <main class="content-shell pool-detail" data-testid="pool-detail-page">
    <a data-testid="pool-detail-back" :href="returnHref">← 返回全市场标的状态</a>
    <header class="detail-head">
      <p class="eyebrow">STRATEGY POOL · DETAIL</p>
      <h1>{{ market }} {{ code }} · {{ detail?.name }} 策略详情</h1>
      <p class="detail-note">创建前为历史回测，创建后为模拟跟踪；只用已完成的行情，打开详情不会启动扫描。</p>
    </header>
    <PageGuide page-key="strategy-pool" />
    <div class="tabs" role="group" aria-label="选择策略目标"><button :aria-pressed="objective === 'annual_return'" data-testid="detail-annual" @click="selectGoal('annual_return')">年化最优</button><button :aria-pressed="objective === 'sharpe'" data-testid="detail-sharpe" @click="selectGoal('sharpe')">夏普最优</button></div>
    <p v-if="appliedOverride" class="detail-note">当前实际生效的是手工覆盖策略，列表收益和信号来自该版本。可切换到自动最优版本查看参考表现。</p>
    <div v-if="versions.length" class="detail-controls">
      <label>跟踪版本 <select v-model="selectedId" @change="offset = 0; loadVersion()"><option v-for="version in versions" :key="version.version_id" :value="version.version_id">{{ version.source === 'manual' ? '手工覆盖' : '自动最优' }} · {{ date(version.created_at) }} · {{ version.is_current ? '当前版本' : '已归档' }}</option></select></label>
      <label>查看区间 <select v-model="scope" data-testid="pool-detail-scope" @change="offset = 0; loadVersion()"><option value="created">创建以来，含承接持仓</option><option value="full">全历史</option></select></label>
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
      <!-- 「尚未初始化」只在真的读完且没有数据时说，以前加载途中也会闪这句 -->
      <p v-if="!loading && !backtest" class="empty-copy" data-testid="pool-detail-not-initialized">账户尚未初始化，等待后台跟踪任务完成。</p>
      <IndividualResearchBacktestDetail v-if="backtest" :backtest="backtest" :launch-date="selected.created_at" :baseline-date="String(summary.baseline_date ?? '')" return-curve>
        <!-- 分页紧贴交易表：以前它在这块面板外面，看 50 笔要先在表里滚一遍、再往下找按钮 -->
        <template #trades-toolbar>
          <nav class="trades-pager" data-testid="pool-trade-pager">
            <button :disabled="offset === 0 || loading" data-testid="pool-trade-prev" @click="stepTrades(-TRADE_PAGE_SIZE)">上一页</button>
            <span>第 {{ tradePage }} / {{ tradePageCount }} 页 · 显示 {{ tradeRangeText }} / 共 {{ total }} 笔</span>
            <button :disabled="offset + TRADE_PAGE_SIZE >= total || loading" data-testid="pool-trade-next" @click="stepTrades(TRADE_PAGE_SIZE)">下一页</button>
          </nav>
        </template>
      </IndividualResearchBacktestDetail>
    </section>
  </main>
</template>
<style scoped>
.pool-detail { display:grid; gap:18px; padding:24px; max-width:1400px; margin:auto; color:var(--text); }
/* 这页以前挂着 panel-card 却没定义它，两个面板其实就是裸内容堆在背景上 */
.panel-card { padding:22px; border:1px solid #25364f; background:rgba(12,20,36,.78); }
.detail-head { display:grid; gap:8px; }
.detail-note, .empty-copy { margin:0; color:#8798b1; font-size:.9rem; line-height:1.6; }
.detail-controls { display:flex; flex-wrap:wrap; align-items:end; gap:14px 24px; padding:16px; border:1px solid #25364f; background:rgba(12,20,36,.6); }
.detail-controls label { display:grid; gap:6px; color:var(--muted); font-size:.88rem; }
.detail-controls select { width:auto; min-width:240px; }
.section-heading h2 { margin:6px 0 0; }
.tabs, .metrics, nav { display:flex; gap:24px; flex-wrap:wrap; }
.rules { display:grid; grid-template-columns:repeat(3,1fr); gap:24px; }
.rules p { white-space:pre-wrap; }
button, select { padding:10px; }
.trades-pager { align-items:center; gap:12px; }
.trades-pager span { color:var(--muted); }
/* 组件里 340px 的窗口是给不分页的个股研究用的；详情页每页只有 50 笔且有分页，
   再压高度就是让用户在表格里二次滚动 */
.pool-detail :deep(.individual-trades) { max-height:620px; }
[aria-pressed="true"] { color:var(--accent); border-color:var(--accent); }
@media (max-width:800px) { .rules { grid-template-columns:1fr; } }
</style>
