<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import PageGuide from '../components/PageGuide.vue'
import IndividualResearchBacktestDetail from '../components/IndividualResearchBacktestDetail.vue'
import { fetchMetadata } from '../api/formulaScreen'
import { ApiError } from '../api/client'
import { buildIndicatorNameMap, indicatorDisplayName } from '../utils/indicatorNames'
import { formatRiskControls, formatTradingRule } from '../utils/strategyRules'
import type { IndividualResearchFullBacktest, StrategyPoolCandidate, StrategyPoolObjective } from '../types'
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
const detail = ref<{ name: string; objectives: Record<StrategyPoolObjective, Version[]> } | null>(null)
const selectedId = ref('')
const versions = computed(() => detail.value?.objectives[objective.value] ?? [])
const selected = computed(() => versions.value.find((v) => v.version_id === selectedId.value) ?? null)
const automatic = computed(() => versions.value.find((v) => v.is_current && v.source === 'automatic'))
const appliedOverride = computed(() => versions.value.find((v) => v.is_current && v.source === 'manual'))
const backtest = ref<IndividualResearchFullBacktest | null>(null)
const summary = ref<Record<string, unknown>>({})
const names = ref(new Map<string, string>())
const holdings = computed(() => Array.isArray(summary.value.holding) ? summary.value.holding as Array<Record<string, unknown>> : [])
const error = ref(''); const loading = ref(false); const offset = ref(0); const total = ref(0)
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
    const base = `${endpoint}/versions/${selectedId.value}`
    const [curve, trades] = await Promise.all([
      get<{ backtest: IndividualResearchFullBacktest | null; summary?: Record<string, unknown> }>(`${base}/curve?scope=${scope.value}`),
      get<{ items: Record<string, unknown>[]; total: number }>(`${base}/trades?scope=${scope.value}&offset=${offset.value}&limit=50`),
    ])
    if (current !== requestId) return
    backtest.value = curve.backtest ? { ...curve.backtest, trades: trades.items } : null
    summary.value = curve.summary ?? {}; total.value = trades.total
  } catch (e) { if (current === requestId) error.value = e instanceof Error ? e.message : '详情加载失败' }
  finally { if (current === requestId) loading.value = false }
}
onMounted(async () => {
  try {
    const [data, metadata] = await Promise.all([get<NonNullable<typeof detail.value>>(`${endpoint}/detail`), fetchMetadata().catch(() => null)])
    detail.value = data; names.value = buildIndicatorNameMap(metadata)
    await selectGoal(objective.value)
  } catch (e) { error.value = e instanceof Error ? e.message : '详情加载失败' }
})
</script>

<template>
    <AppTopbar />
  <main class="content-shell pool-detail" data-testid="pool-detail-page">
    <a data-testid="pool-detail-back" :href="returnHref">← 返回全市场标的状态</a>
    <h1>{{ market }} {{ code }} · {{ detail?.name }} 策略详情</h1>
    <PageGuide page-key="strategy-pool" />
    <p>创建前为历史回测，创建后为模拟跟踪。仅使用已完成行情，不在打开详情时启动扫描。</p>
    <div class="tabs"><button :aria-pressed="objective === 'annual_return'" data-testid="detail-annual" @click="selectGoal('annual_return')">年化最优</button><button :aria-pressed="objective === 'sharpe'" data-testid="detail-sharpe" @click="selectGoal('sharpe')">夏普最优</button></div>
    <p v-if="appliedOverride">当前实际生效的是手工覆盖策略，列表收益和信号来自该版本。可切换到自动最优版本查看参考表现。</p>
    <label>跟踪版本 <select v-model="selectedId" @change="offset = 0; loadVersion()"><option v-for="version in versions" :key="version.version_id" :value="version.version_id">{{ version.source === 'manual' ? '手工覆盖' : '自动最优' }} · {{ date(version.created_at) }} · {{ version.is_current ? '当前版本' : '已归档' }}</option></select></label>
    <p v-if="!versions.length">该目标暂无可用策略。请在策略池研究完成后查看。</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <section v-if="selected" class="panel-card">
      <h2>交易规则</h2>
      <div class="rules"><div><h3>买入规则</h3><p>{{ formatTradingRule(selected.candidate.buy_rule, indicator) }}</p></div><div><h3>卖出规则</h3><p>{{ formatTradingRule(selected.candidate.sell_rule, indicator) }}</p></div><div><h3>退出风控</h3><p>{{ formatRiskControls(selected.candidate.config) }}</p></div></div>
      <p>创建时间 {{ date(selected.created_at) }}<span v-if="selected.stopped_at"> · 停用时间 {{ date(selected.stopped_at) }}</span></p>
      <p>状态 {{ selected.status }} · 行情截至 {{ summary.as_of_date ?? selected.summary.as_of_date ?? '待初始化' }}</p>
      <p v-if="selected.last_error" role="alert">更新失败：{{ selected.last_error }}。保留最后成功结果。</p>
      <div class="metrics"><span>{{ selected.is_current ? '创建至今收益' : '截至停用收益' }} <b>{{ percent(summary.created_return) }}</b></span><span>上线后收益 <b>{{ percent(summary.post_launch_return) }}</b></span></div>
      <p v-if="summary.inherited_position">含跨创建日持仓，收益统计起点为原买入日 {{ summary.baseline_date }}；不包含更早已完成交易的收益。</p>
      <p v-if="summary.as_of_date && summary.has_post_launch_data === false">尚无创建后的新行情。</p>
      <div v-if="holdings.length"><p v-for="(holding, index) in holdings" :key="index">持仓 {{ holding.size }} 股 · 买入日期 {{ holding.entry_date }} · 买入价 {{ holding.entry_price }} · 当前价 {{ holding.close ?? '—' }} · 浮动收益 {{ percent(holding.unrealized_pnl_pct) }}</p></div>
      <p v-else-if="summary.as_of_date">当前空仓</p>
      <p v-if="summary.recomputed_at">行情校正后于 {{ summary.recomputed_at }} 重算，创建时间保持不变。</p>
    </section>
    <section v-if="selected" class="panel-card">
      <label>查看区间 <select v-model="scope" @change="offset = 0; loadVersion()"><option value="created">创建以来，含承接持仓</option><option value="full">全历史</option></select></label>
      <p v-if="loading">读取跟踪缓存…</p><p v-else-if="!backtest">账户尚未初始化，等待后台跟踪任务完成。</p>
      <IndividualResearchBacktestDetail v-else :backtest="backtest" :launch-date="selected.created_at" :baseline-date="String(summary.baseline_date ?? '')" return-curve hide-metrics />
      <nav v-if="total"><button :disabled="offset === 0 || loading" @click="offset = Math.max(0, offset - 50); loadVersion()">上一页</button><span>{{ offset + 1 }}–{{ Math.min(offset + 50, total) }} / {{ total }} 笔</span><button :disabled="offset + 50 >= total || loading" @click="offset += 50; loadVersion()">下一页</button></nav>
    </section>
  </main>
</template>
<style scoped>
.pool-detail { display:grid; gap:18px; padding:24px; max-width:1400px; margin:auto; color:var(--text); }
.tabs, .metrics, nav { display:flex; gap:24px; flex-wrap:wrap; }
.rules { display:grid; grid-template-columns:repeat(3,1fr); gap:24px; }
.rules p { white-space:pre-wrap; }
button, select { padding:10px; }
[aria-pressed="true"] { color:var(--accent); border-color:var(--accent); }
@media (max-width:800px) { .rules { grid-template-columns:1fr; } }
</style>
