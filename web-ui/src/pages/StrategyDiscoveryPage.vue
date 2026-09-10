<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { FormulaScreenApiError } from '../api/formulaScreen'
import {
  createStrategyDiscovery,
  cancelStrategyDiscovery,
  deleteStrategyDiscoveryHistory,
  fetchStrategyDiscoveryCapabilities,
  fetchStrategyDiscoveryHistory,
  getStrategyDiscovery,
  getStrategyDiscoveryHistory,
  getStrategyDiscoveryResults,
} from '../api/strategyDiscovery'
import RuleGroupEditor from '../components/RuleGroupEditor.vue'
import type {
  ConditionOperator,
  RuleGroupPayload,
  ScreenCondition,
  StrategyDiscoveryCandidate,
  StrategyDiscoveryJobState,
  StrategyDiscoveryPayload,
  StrategyDiscoveryResult,
  InstrumentBoard,
  InstrumentType,
  Universe,
  ValueDefinition,
} from '../types'

const APPLY_STORAGE_KEY = 'indicator-lab.strategy-discovery.apply'
const APPLY_METADATA_KEY = 'indicator-lab.strategy-discovery.apply-meta'

const INSTRUMENT_TYPE_OPTIONS: { key: InstrumentType; label: string }[] = [
  { key: 'stock', label: '股票/B股' },
  { key: 'fund', label: '基金/ETF' },
  { key: 'index', label: '指数' },
  { key: 'bond', label: '债券' },
]

const BOARD_OPTIONS: { key: InstrumentBoard; label: string }[] = [
  { key: 'main', label: '主板' },
  { key: 'star', label: '科创板' },
  { key: 'chinext', label: '创业板' },
  { key: 'b_share', label: 'B股' },
  { key: 'fund', label: '基金/ETF' },
  { key: 'index', label: '指数' },
  { key: 'bond', label: '债券' },
]

function emptyRule(): RuleGroupPayload {
  return { signals: [], conditions: [], combine_mode: 'all', minimum_matches: null }
}

const form = reactive({
  universe: 'all' as Universe,
  universeFile: '',
  instrumentTypes: [] as InstrumentType[],
  boards: [] as InstrumentBoard[],
  requiredBuyRule: emptyRule(),
  requiredSellRule: emptyRule(),
  maxPositions: 5,
  searchLevel: 'low',
})
const capabilities = ref<Record<string, unknown> | null>(null)
const history = ref<Array<Record<string, unknown>>>([])
const job = ref<StrategyDiscoveryJobState | null>(null)
const result = ref<StrategyDiscoveryResult | null>(null)
const activeBoard = ref<'sharpe' | 'return'>('sharpe')
const selectedCandidate = ref<StrategyDiscoveryCandidate | null>(null)
const loading = ref(false)
const message = ref('')
const errors = ref<Record<string, string>>({})

const boards = computed(() => activeBoard.value === 'sharpe'
  ? result.value?.results_by_sharpe ?? []
  : result.value?.results_by_return ?? [])
const availableValues = computed<ValueDefinition[]>(() => {
  const raw = capabilities.value?.indicators
  if (!Array.isArray(raw)) return []
  return raw.flatMap((item) => {
    if (!item || typeof item !== 'object') return []
    const value = item as Record<string, unknown>
    if (
      value.rankable === false
      || typeof value.id !== 'string'
      || typeof value.display_name !== 'string'
    ) return []
    return [{
      id: value.id,
      display_name: value.display_name,
      description: typeof value.description === 'string' ? value.description : '',
    }]
  })
})
const requiredBuySlots = computed(() => Math.max(0, 3 - form.requiredBuyRule.conditions.length))
const requiredSellSlots = computed(() => Math.max(0, 3 - form.requiredSellRule.conditions.length))
const canSubmit = computed(() => (
  !loading.value
  && Number.isInteger(form.maxPositions)
  && form.maxPositions >= 1
  && form.maxPositions <= 100
  && capabilities.value?.enabled !== false
))

function validate(): Record<string, string> {
  const next: Record<string, string> = {}
  if (!Number.isInteger(form.maxPositions) || form.maxPositions < 1 || form.maxPositions > 100) {
    next.maxPositions = '持仓数量必须是 1 到 100 的整数。'
  }
  if (form.universe === 'custom' && !form.universeFile.trim()) next.universeFile = '请输入自定义品种列表文件。'
  return next
}

function buildPayload(): StrategyDiscoveryPayload {
  const trialCount = form.searchLevel === 'high' ? 3000 : form.searchLevel === 'medium' ? 2000 : 1000
  return {
    universe: form.universe,
    universe_file: form.universe === 'custom' ? form.universeFile.trim() : null,
    ...(form.instrumentTypes.length > 0 ? { instrument_types: [...form.instrumentTypes] } : {}),
    ...(form.boards.length > 0 ? { boards: [...form.boards] } : {}),
    ...(form.requiredBuyRule.conditions.length > 0
      ? { required_buy_conditions: form.requiredBuyRule.conditions.map((condition) => ({ ...condition })) }
      : {}),
    ...(form.requiredSellRule.conditions.length > 0
      ? { required_sell_conditions: form.requiredSellRule.conditions.map((condition) => ({ ...condition })) }
      : {}),
    max_positions: form.maxPositions,
    max_trials: trialCount,
    timeout_seconds: 7200,
    validation_candidates: 50,
    holdout_candidates: 20,
    min_trades: 5,
    max_drawdown: 0.3,
    max_complexity: 12,
    multifidelity_enabled: true,
  }
}

function apiMessage(error: unknown): string {
  if (error instanceof FormulaScreenApiError) return error.message
  if (error instanceof Error && error.message) return `策略实验失败：${error.message}`
  return '策略实验失败，请检查本地 DuckDB 数据和配置后重试。'
}

function toggleInstrumentType(type: InstrumentType): void {
  form.instrumentTypes = form.instrumentTypes.includes(type)
    ? form.instrumentTypes.filter((item) => item !== type)
    : [...form.instrumentTypes, type]
}

function toggleBoard(board: InstrumentBoard): void {
  form.boards = form.boards.includes(board)
    ? form.boards.filter((item) => item !== board)
    : [...form.boards, board]
}

function setRequiredRule(side: 'buy' | 'sell', rule: RuleGroupPayload): void {
  const normalized: RuleGroupPayload = {
    signals: [],
    conditions: rule.conditions.map((condition) => ({ ...condition })),
    combine_mode: 'all',
    minimum_matches: null,
  }
  if (side === 'buy') form.requiredBuyRule = normalized
  else form.requiredSellRule = normalized
}

async function submit(): Promise<void> {
  errors.value = validate()
  message.value = ''
  if (Object.values(errors.value).some(Boolean)) return
  loading.value = true
  result.value = null
  selectedCandidate.value = null
  try {
    const created = await createStrategyDiscovery(buildPayload())
    job.value = {
      job_id: created.job_id,
      status: 'queued',
      progress: 0,
      total_candidates: 0,
      total_scanned: 0,
      errors: 0,
      skipped: 0,
      error: null,
      result: null,
    }
    for (;;) {
      const state = await getStrategyDiscovery(created.job_id)
      job.value = state
      if (state.status === 'completed') {
        result.value = await getStrategyDiscoveryResults(created.job_id)
        message.value = `策略实验完成：夏普榜 ${result.value.results_by_sharpe.length} 组，收益榜 ${result.value.results_by_return.length} 组。`
        await loadHistory()
        return
      }
      if (state.status === 'failed') {
        message.value = state.error ?? '策略实验任务失败，请检查配置和服务状态后重试。'
        return
      }
      if (state.status === 'cancelled') {
        message.value = state.error ?? '策略实验已取消。'
        return
      }
      await new Promise((resolve) => window.setTimeout(resolve, 220))
    }
  } catch (error) {
    message.value = apiMessage(error)
  } finally {
    loading.value = false
  }
}

async function cancel(): Promise<void> {
  if (!job.value || !loading.value) return
  try {
    await cancelStrategyDiscovery(job.value.job_id)
    message.value = '已请求取消策略实验，正在等待当前候选结束。'
  } catch (error) {
    message.value = apiMessage(error)
  }
}

function metric(candidate: StrategyDiscoveryCandidate, split: string, key: string): number | null {
  const value = candidate.metrics[split]?.[key]
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function formatPercent(value: number | null): string {
  return value === null ? '—' : `${(value * 100).toFixed(2)}%`
}

function formatNumber(value: number | null): string {
  return value === null ? '—' : value.toFixed(2)
}

function metadataNumber(state: StrategyDiscoveryJobState | null, key: string): number | null {
  const value = state?.metadata?.[key]
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function progressValue(state: StrategyDiscoveryJobState | null): number {
  const overall = metadataNumber(state, 'overall_progress')
  return Math.min(1, Math.max(0, overall ?? state?.progress ?? 0))
}

function progressPercent(state: StrategyDiscoveryJobState | null): string {
  return `${Math.round(progressValue(state) * 100)}%`
}

function progressPhase(state: StrategyDiscoveryJobState | null): string {
  if (!state) return '准备中'
  if (state.status === 'completed') return '已完成'
  if (state.status === 'failed') return '实验失败'
  if (state.status === 'cancelled') return '已取消'
  const label = state.metadata?.phase_label
  if (typeof label === 'string' && label) return label
  return state.phase ?? '准备中'
}

function progressCount(state: StrategyDiscoveryJobState | null, key: string, fallback: number): number {
  return metadataNumber(state, key) ?? fallback
}

function formatDuration(state: StrategyDiscoveryJobState | null): string {
  if (state?.status === 'completed') return '已完成'
  const seconds = metadataNumber(state, 'estimated_seconds_remaining')
  if (seconds === null) return '计算中'
  if (seconds < 60) return `${Math.max(1, Math.ceil(seconds))} 秒`
  const minutes = Math.ceil(seconds / 60)
  if (minutes < 60) return `${minutes} 分钟`
  return `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分钟`
}

function holdoutStatus(candidate: StrategyDiscoveryCandidate): string {
  const value = candidate.metrics.holdout?.constraints_passed
  return value === true ? '通过' : value === false ? '未通过' : '—'
}

function selectCandidate(candidate: StrategyDiscoveryCandidate): void {
  selectedCandidate.value = candidate
}

function applyCandidate(candidate: StrategyDiscoveryCandidate): void {
  sessionStorage.setItem(APPLY_STORAGE_KEY, JSON.stringify(candidate.config))
  sessionStorage.setItem(APPLY_METADATA_KEY, JSON.stringify({
    candidate_hash: candidate.candidate_hash,
    data_fingerprint: result.value?.data_fingerprint ?? null,
    validation_metrics: candidate.metrics.validation ?? null,
  }))
  if (import.meta.env.MODE !== 'test') window.location.assign('/portfolio-backtest')
}

async function loadHistory(): Promise<void> {
  try {
    history.value = await fetchStrategyDiscoveryHistory()
  } catch (error) {
    message.value = apiMessage(error)
  }
}

async function loadHistoryDetail(historyId: string): Promise<void> {
  try {
    const loaded = await getStrategyDiscoveryHistory(historyId)
    result.value = loaded
    restoreHistoryRequest(loaded.request)
    activeBoard.value = 'sharpe'
    message.value = `已加载历史实验 ${historyId}。`
  } catch (error) {
    message.value = apiMessage(error)
  }
}

function restoreHistoryRequest(raw: unknown): void {
  if (!raw || typeof raw !== 'object') return
  const request = raw as Record<string, unknown>
  const base = request.base_config && typeof request.base_config === 'object'
    ? request.base_config as Record<string, unknown>
    : {}
  if (typeof base.universe === 'string' && ['all', 'sh', 'sz', 'custom'].includes(base.universe)) form.universe = base.universe as Universe
  if (typeof base.universe_file === 'string') form.universeFile = base.universe_file
  if (Array.isArray(base.instrument_types)) {
    form.instrumentTypes = base.instrument_types.filter((item): item is InstrumentType => INSTRUMENT_TYPE_OPTIONS.some((option) => option.key === item))
  }
  if (Array.isArray(base.boards)) {
    form.boards = base.boards.filter((item): item is InstrumentBoard => BOARD_OPTIONS.some((option) => option.key === item))
  }
  form.requiredBuyRule = requiredRuleFromHistory(request.required_buy_conditions)
  form.requiredSellRule = requiredRuleFromHistory(request.required_sell_conditions)
  if (typeof base.max_positions === 'number' && Number.isInteger(base.max_positions)) form.maxPositions = base.max_positions
}

function requiredRuleFromHistory(raw: unknown): RuleGroupPayload {
  if (!Array.isArray(raw)) return emptyRule()
  const operators = new Set<ConditionOperator>([
    'gt', 'gte', 'lt', 'lte', 'eq', 'neq', 'cross_up', 'cross_down',
  ])
  const conditions = raw.flatMap((item): ScreenCondition[] => {
    if (!item || typeof item !== 'object') return []
    const value = item as Record<string, unknown>
    const left = value.left_value
    const operator = value.operator
    const rightValue = value.right_value
    const rightConstant = value.right_constant
    if (
      typeof left !== 'string'
      || !operators.has(operator as ConditionOperator)
      || (rightValue !== null && typeof rightValue !== 'string')
      || (rightConstant !== null && typeof rightConstant !== 'number')
      || (typeof rightValue === 'string') === (typeof rightConstant === 'number')
    ) return []
    return [{
      left_value: left,
      operator: operator as ConditionOperator,
      right_value: typeof rightValue === 'string' ? rightValue : null,
      right_constant: typeof rightConstant === 'number' ? rightConstant : null,
    }]
  })
  return { signals: [], conditions: conditions.slice(0, 3), combine_mode: 'all', minimum_matches: null }
}

async function removeHistory(historyId: string): Promise<void> {
  try {
    await deleteStrategyDiscoveryHistory(historyId)
    await loadHistory()
    message.value = '历史实验已删除。'
  } catch (error) {
    message.value = apiMessage(error)
  }
}

onMounted(async () => {
  try {
    capabilities.value = await fetchStrategyDiscoveryCapabilities()
  } catch (error) {
    message.value = apiMessage(error)
  }
  await loadHistory()
})
</script>

<template>
  <div class="screen-page strategy-discovery-page" data-testid="strategy-discovery-page">
    <header class="topbar">
      <a class="brand" href="/indicator-lab" aria-label="指标实验室首页"><img class="brand-mark" src="/indicator-lab-mark.png" alt="" aria-hidden="true" width="34" height="34"><span><strong>Indicator Lab</strong><small>指标实验室</small></span></a>
      <nav aria-label="主导航">
        <a class="nav-link" href="/indicator-lab">公式选股</a>
        <a class="nav-link" href="/backtest">单股回测</a>
        <a class="nav-link" href="/portfolio-backtest">组合回测</a>
        <a class="nav-link active" href="/strategy-lab" aria-current="page">策略实验室</a>
        <a class="nav-link" href="/strategy-fitness">策略适配性</a>
        <a class="nav-link" href="/market-data">本地行情</a>
      </nav>
      <div class="topbar-status"><span class="status-dot" aria-hidden="true"></span> 本地数据模式</div>
    </header>

    <main class="content-shell">
      <section class="page-intro">
        <div><p class="eyebrow">STRATEGY DISCOVERY / DAILY</p><h1>策略实验室</h1><p class="intro-copy">只设置品种范围和持仓数量，系统自动使用本地全部已完成日线寻找排序、买卖和风险控制配置。</p></div>
        <div class="intro-note"><span class="note-label">自动流程</span><strong>寻优 · 验证 · 最终留出</strong><small>真实组合回测裁定候选结果</small></div>
      </section>

      <div v-if="message" class="notice" :class="{ error: !message.includes('完成') && !message.includes('加载') && !message.includes('删除') }" role="alert" data-testid="strategy-discovery-message">{{ message }}</div>

      <div class="workspace-grid strategy-discovery-grid">
        <form class="config-panel panel" data-testid="strategy-discovery-config" @submit.prevent="submit">
          <div class="panel-heading"><div><span class="section-kicker">01 / CONFIGURE</span><h2>实验设置</h2></div><span class="required-hint">* 必填</span></div>
          <fieldset class="form-section">
            <legend>数据范围</legend>
            <p class="helper">策略实验只读取本地 DuckDB 中全部已完成日线，不会上传行情，也不需要填写日期。</p>
            <div class="compact-fields">
              <div><label for="strategy-discovery-universe">品种范围</label><select id="strategy-discovery-universe" v-model="form.universe" data-testid="strategy-discovery-universe"><option value="all">沪深全部品种</option><option value="sh">仅上海</option><option value="sz">仅深圳</option><option value="custom">自定义列表</option></select></div>
              <div><label for="strategy-discovery-positions">持仓数量 N</label><input id="strategy-discovery-positions" v-model.number="form.maxPositions" data-testid="strategy-discovery-positions" type="number" min="1" max="100" step="1"></div>
            </div>
            <div class="scope-config inline-scope-config" data-testid="strategy-discovery-scope"><div class="scope-options"><span>证券类型</span><label v-for="option in INSTRUMENT_TYPE_OPTIONS" :key="option.key"><input type="checkbox" :data-testid="`strategy-discovery-scope-type-${option.key}`" :checked="form.instrumentTypes.includes(option.key)" @change="toggleInstrumentType(option.key)">{{ option.label }}</label></div><div class="scope-options"><span>板块</span><label v-for="option in BOARD_OPTIONS" :key="option.key"><input type="checkbox" :data-testid="`strategy-discovery-scope-board-${option.key}`" :checked="form.boards.includes(option.key)" @change="toggleBoard(option.key)">{{ option.label }}</label></div><small class="scope-helper">未勾选表示全部；类型和板块同时选择时取交集。</small></div>
            <div v-if="form.universe === 'custom'" class="portfolio-inline-field"><label for="strategy-discovery-universe-file">品种列表文件</label><input id="strategy-discovery-universe-file" v-model="form.universeFile" data-testid="strategy-discovery-universe-file" type="text"><p v-if="errors.universeFile" class="field-error">{{ errors.universeFile }}</p></div>
            <p v-if="errors.maxPositions" class="field-error">{{ errors.maxPositions }}</p>
          </fieldset>
          <fieldset class="form-section required-conditions-section">
            <legend>用户必选条件（可选）</legend>
            <p class="helper">这里设置的指标条件会强制加入每一个候选；所有必选条件均按 AND 执行。固定条件可以减少无效搜索。</p>
            <RuleGroupEditor
              :model-value="form.requiredBuyRule"
              :values="availableValues"
              title="必选买入条件"
              testid="strategy-required-buy-rule"
              fixed
              :max-conditions="3"
              @update:model-value="setRequiredRule('buy', $event)"
            />
            <RuleGroupEditor
              :model-value="form.requiredSellRule"
              :values="availableValues"
              title="必选卖出条件"
              testid="strategy-required-sell-rule"
              fixed
              :max-conditions="3"
              @update:model-value="setRequiredRule('sell', $event)"
            />
            <p class="scope-helper">自动补充规则名额：买入 {{ requiredBuySlots }} 项 · 卖出 {{ requiredSellSlots }} 项；排序、调仓和风控仍会继续寻优。</p>
          </fieldset>
          <fieldset class="form-section">
            <legend>搜索强度</legend>
            <select v-model="form.searchLevel" data-testid="strategy-discovery-search-level"><option value="low">低 · 1000 组</option><option value="medium">中 · 2000 组</option><option value="high">高 · 3000 组</option></select>
            <p class="helper">固定搜索范围：3 组公式指标的用户可选输出 + MA5/10/20/60、RSI14、MACD、SIGNAL、Histogram；Z 等恒定输出不参与寻优。</p>
            <p class="helper">排序指标、买卖条件、止盈止损、移动止盈和冷却天数均自动搜索。佣金默认万分之一，最低 0.1 元。</p>
          </fieldset>
          <div v-if="capabilities" class="discovery-capability-note" data-testid="strategy-discovery-capabilities">指标 {{ capabilities.indicator_count }} 个 · 信号 {{ capabilities.signal_count }} 个 · Optuna {{ capabilities.optuna_available ? '可用' : '不可用' }} · LightGBM {{ capabilities.lightgbm_available ? '可用' : '不可用' }} · 回测 {{ capabilities.evaluation_workers ?? 8 }} 进程<span v-if="capabilities.enabled === false" class="negative"> · 请重新构建 Docker 镜像安装策略寻优依赖</span></div>
          <div class="action-stack"><button class="primary-button" data-testid="strategy-discovery-submit" type="submit" :disabled="!canSubmit"><span v-if="loading" class="spinner" aria-hidden="true"></span>{{ loading ? '实验进行中…' : '开始策略实验' }}</button><button v-if="loading" class="secondary-button" data-testid="strategy-discovery-cancel" type="button" @click="cancel">取消实验</button></div>
          <div v-if="job" class="discovery-progress" data-testid="strategy-discovery-progress"><div class="discovery-progress-heading"><strong>{{ progressPhase(job) }}</strong><span data-testid="strategy-discovery-overall-progress">总体 {{ progressPercent(job) }}</span></div><div class="progress-track"><span :style="{ width: progressPercent(job) }"></span></div><div class="discovery-progress-stats"><span>当前阶段 {{ progressCount(job, 'phase_current', job.total_scanned) }} / {{ progressCount(job, 'phase_total', job.total_candidates) }}</span><span>预计总评估 {{ progressCount(job, 'estimated_total_evaluations', job.total_candidates) }} 次</span><span data-testid="strategy-discovery-remaining">预计剩余 {{ formatDuration(job) }}</span></div><small v-if="job.metadata?.best_sharpe !== undefined">吞吐 {{ formatNumber(metadataNumber(job, 'throughput_per_second')) }} 候选/秒 · 当前最佳夏普 {{ formatNumber(typeof job.metadata.best_sharpe === 'number' ? job.metadata.best_sharpe : null) }} · 收益 {{ formatPercent(typeof job.metadata.best_return === 'number' ? job.metadata.best_return : null) }}</small></div>
        </form>

        <section class="results-panel panel" data-testid="strategy-discovery-results" aria-live="polite">
          <div class="panel-heading"><div><span class="section-kicker">02 / LEADERBOARD</span><h2>候选排行榜</h2></div><span v-if="result" class="required-hint">验证期排序</span></div>
          <div v-if="!result" class="empty-state"><div class="empty-icon" aria-hidden="true">/</div><h3>{{ loading ? '正在搜索策略候选…' : '还没有实验结果' }}</h3><p>实验完成后会分别展示夏普榜和收益榜。</p></div>
          <template v-else>
            <div class="formula-mode-tabs" role="tablist" aria-label="策略排行榜"><button type="button" data-testid="strategy-discovery-sharpe-board" :class="{ active: activeBoard === 'sharpe' }" role="tab" :aria-selected="activeBoard === 'sharpe'" @click="activeBoard = 'sharpe'">夏普榜前 10</button><button type="button" data-testid="strategy-discovery-return-board" :class="{ active: activeBoard === 'return' }" role="tab" :aria-selected="activeBoard === 'return'" @click="activeBoard = 'return'">收益榜前 10</button></div>
            <div v-if="!boards.length" class="inline-empty">没有满足当前约束的候选策略。</div>
            <div v-else class="table-wrap"><table><caption class="sr-only">策略候选排行榜</caption><thead><tr><th>#</th><th>策略配置</th><th>验证收益</th><th>验证夏普</th><th>验证回撤</th><th>留出收益</th><th>留出约束</th><th>操作</th></tr></thead><tbody><tr v-for="(candidate, index) in boards" :key="candidate.candidate_hash" :data-testid="`strategy-discovery-row-${candidate.candidate_hash}`"><td>{{ index + 1 }}</td><td><button class="table-link" type="button" @click="selectCandidate(candidate)">{{ candidate.human_readable_rules }}</button></td><td>{{ formatPercent(metric(candidate, 'validation', 'annual_return')) }}</td><td>{{ formatNumber(metric(candidate, 'validation', 'sharpe')) }}</td><td>{{ formatPercent(metric(candidate, 'validation', 'max_drawdown')) }}</td><td>{{ formatPercent(metric(candidate, 'holdout', 'annual_return')) }}</td><td :class="{ positive: holdoutStatus(candidate) === '通过', negative: holdoutStatus(candidate) === '未通过' }">{{ holdoutStatus(candidate) }}</td><td><button class="secondary-button" :data-testid="`strategy-discovery-apply-${candidate.candidate_hash}`" type="button" @click="applyCandidate(candidate)">应用</button></td></tr></tbody></table></div>
            <div v-if="selectedCandidate" class="discovery-detail" data-testid="strategy-discovery-detail"><div class="section-heading"><div><span class="section-kicker">CANDIDATE DETAIL</span><h3>策略配置</h3></div><button class="secondary-button" type="button" @click="selectedCandidate = null">关闭</button></div><p>{{ selectedCandidate.human_readable_rules }}</p><div class="metrics-strip"><div><span>验证收益</span><strong>{{ formatPercent(metric(selectedCandidate, 'validation', 'annual_return')) }}</strong></div><div><span>验证夏普</span><strong>{{ formatNumber(metric(selectedCandidate, 'validation', 'sharpe')) }}</strong></div><div><span>最终留出收益</span><strong>{{ formatPercent(metric(selectedCandidate, 'holdout', 'annual_return')) }}</strong></div><div><span>复杂度</span><strong>{{ selectedCandidate.complexity }}</strong></div></div><pre>{{ JSON.stringify(selectedCandidate.config, null, 2) }}</pre></div>
          </template>
        </section>
      </div>

      <section class="panel discovery-history-panel" data-testid="strategy-discovery-history">
        <div class="panel-heading"><div><span class="section-kicker">03 / HISTORY</span><h2>历史实验</h2></div><button class="secondary-button" type="button" @click="loadHistory">刷新</button></div>
        <div v-if="!history.length" class="inline-empty">暂无历史实验。</div>
        <div v-else class="table-wrap"><table><thead><tr><th>实验 ID</th><th>时间</th><th>候选数</th><th>最佳夏普</th><th>最佳收益</th><th>操作</th></tr></thead><tbody><tr v-for="item in history" :key="String(item.history_id)"><td>{{ item.history_id }}</td><td>{{ item.created_at }}</td><td>{{ item.total_trials }}</td><td>{{ item.best_sharpe ? '可查看' : '—' }}</td><td>{{ item.best_return ? '可查看' : '—' }}</td><td><button class="table-link" type="button" @click="loadHistoryDetail(String(item.history_id))">加载</button><button class="table-link danger-link" type="button" @click="removeHistory(String(item.history_id))">删除</button></td></tr></tbody></table></div>
      </section>
    </main>
  </div>
</template>
