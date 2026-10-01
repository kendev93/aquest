<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import PageGuide from '../components/PageGuide.vue'
import DateRangeSelector from '../components/DateRangeSelector.vue'
import IndividualResearchBacktestDetail from '../components/IndividualResearchBacktestDetail.vue'
import { fetchMetadata, FormulaScreenApiError } from '../api/formulaScreen'
import {
  cancelIndividualResearch,
  createIndividualResearch,
  deleteIndividualResearchHistory,
  fetchIndividualResearchHistory,
  getIndividualResearchHistory,
  getIndividualResearchJob,
} from '../api/individualResearch'
import { fetchLocalInstruments } from '../api/marketData'
import { createStrategy } from '../api/savedStrategies'
import { fetchActiveJobs } from '../api/jobs'
import type {
  DateRangeSelection,
  FormulaScreenMetadata,
  IndividualResearchCandidate,
  IndividualResearchFullBacktest,
  IndividualResearchHistorySummary,
  IndividualResearchJobState,
  IndividualResearchPayload,
  IndividualResearchResult,
  InstrumentBoard,
  LocalInstrument,
  PortfolioBacktestPayload,
  ValueDefinition,
} from '../types'
import type { SignalDefinition } from '../types'

const BOARD_OPTIONS: { key: InstrumentBoard; label: string }[] = [
  { key: 'main', label: '主板' },
  { key: 'star', label: '科创板' },
  { key: 'chinext', label: '创业板' },
]

const metadata = ref<FormulaScreenMetadata | null>(null)
const form = reactive({
  market: 'SH' as 'SH' | 'SZ',
  code: '',
  name: '',
  dateMode: 'full' as DateRangeSelection['mode'],
  startDate: '',
  endDate: '',
  reversalMultiple: 3,
  atrWindow: 14,
  minBars: 5,
  horizon: 10,
  valueIds: [] as string[],
  signalIds: [] as string[],
  candidateLimit: 10,
  riskTrials: 1000 as 1000 | 2000 | 3000,
  initialCash: 1_000_000,
  commission: 0.0001,
  minCommission: 0.1,
  stampTax: 0.001,
  slippage: 0,
  execution: 'next_open' as 'next_open' | 'next_close',
  positionMode: 'full' as 'full' | 'fixed',
  fixedSize: 1000,
})
const searchText = ref('')
const instrumentOptions = ref<LocalInstrument[]>([])
const selectedInstrumentRange = ref<Pick<LocalInstrument, 'bars' | 'data_start' | 'data_end'> | null>(null)
const instrumentLoading = ref(false)
const instrumentError = ref('')
const selectedValues = ref<Set<string>>(new Set())
const selectedSignals = ref<Set<string>>(new Set())
const running = ref(false)
const job = ref<IndividualResearchJobState | null>(null)
const result = ref<IndividualResearchResult | null>(null)
const history = ref<IndividualResearchHistorySummary[]>([])
const historyLoading = ref(false)
const historyBusyId = ref('')
const savedCandidateIds = ref<Set<string>>(new Set())
const savingCandidateId = ref('')
const expandedFullBacktestKey = ref('')
const message = ref('')
const messageIsError = ref(false)
const errors = ref<Record<string, string>>({})
let instrumentSearchTimer: ReturnType<typeof setTimeout> | null = null

const searchableValues = computed<ValueDefinition[]>(() => (
  metadata.value?.indicators.flatMap((indicator) => indicator.values ?? [])
    ?? []
))
const availableSignals = computed<SignalDefinition[]>(() => (
  metadata.value?.indicators.flatMap((indicator) => indicator.signals ?? []) ?? []
))
const dateRange = computed<DateRangeSelection>({
  get: () => ({ mode: form.dateMode, startDate: form.startDate, endDate: form.endDate }),
  set: (value) => {
    form.dateMode = value.mode
    form.startDate = value.startDate
    form.endDate = value.endDate
  },
})
const canSubmit = computed(() => (
  !running.value
  && metadata.value !== null
  && form.code.length === 6
  && selectedValues.value.size > 0
))
const sharpeCandidates = computed(() => result.value?.results_by_sharpe ?? [])
const returnCandidates = computed(() => result.value?.results_by_return ?? [])

function showMessage(text: string, isError = false): void {
  message.value = text
  messageIsError.value = isError
}

function apiMessage(error: unknown, fallback: string): string {
  return error instanceof FormulaScreenApiError
    ? error.message
    : error instanceof Error ? error.message : fallback
}

function toggleValue(valueId: string): void {
  const next = new Set(selectedValues.value)
  if (next.has(valueId)) next.delete(valueId)
  else next.add(valueId)
  selectedValues.value = next
}

function toggleSignal(signalId: string): void {
  const next = new Set(selectedSignals.value)
  if (next.has(signalId)) next.delete(signalId)
  else next.add(signalId)
  selectedSignals.value = next
}

function setAllValues(): void {
  selectedValues.value = new Set(searchableValues.value.map((value) => value.id))
}

function clearValues(): void {
  selectedValues.value = new Set()
}

function setAllSignals(): void {
  selectedSignals.value = new Set(availableSignals.value.map((signal) => signal.id))
}

function clearSignals(): void {
  selectedSignals.value = new Set()
}

async function searchInstruments(): Promise<void> {
  instrumentLoading.value = true
  instrumentError.value = ''
  try {
    const response = await fetchLocalInstruments({ keyword: searchText.value.trim(), page_size: 20 })
    instrumentOptions.value = response.items.filter(
      (item) => item.instrument_type === 'stock' && item.bars > 0,
    )
  } catch (error) {
    instrumentOptions.value = []
    instrumentError.value = apiMessage(error, '读取本地股票目录失败。')
  } finally {
    instrumentLoading.value = false
  }
}

function scheduleInstrumentSearch(): void {
  if (instrumentSearchTimer !== null) clearTimeout(instrumentSearchTimer)
  instrumentSearchTimer = setTimeout(() => void searchInstruments(), 250)
}

function selectInstrument(item: LocalInstrument): void {
  form.market = item.market
  form.code = item.code
  form.name = item.name ?? ''
  selectedInstrumentRange.value = {
    bars: item.bars,
    data_start: item.data_start,
    data_end: item.data_end,
  }
  searchText.value = `${item.market} ${item.code}${item.name ? ` · ${item.name}` : ''}`
  instrumentOptions.value = []
  errors.value = { ...errors.value, code: '' }
}

function validate(): Record<string, string> {
  const next: Record<string, string> = {}
  if (!form.code) next.code = '请从本地行情目录选择一只股票。'
  if (selectedValues.value.size === 0) next.valueIds = '至少选择一个可搜索指标。'
  if (form.positionMode === 'fixed' && (!Number.isInteger(form.fixedSize) || form.fixedSize < 100 || form.fixedSize % 100 !== 0)) {
    next.fixedSize = '固定股数必须是 100 的正整数倍。'
  }
  if (form.dateMode === 'custom') {
    if (!form.startDate) next.startDate = '请选择开始日期。'
    if (!form.endDate) next.endDate = '请选择结束日期。'
    if (form.startDate && form.endDate && form.startDate > form.endDate) next.endDate = '结束日期不能早于开始日期。'
  }
  return next
}

function buildPayload(): IndividualResearchPayload {
  return {
    market: form.market,
    code: form.code,
    date_mode: form.dateMode,
    start_date: form.dateMode === 'custom' ? form.startDate : null,
    end_date: form.dateMode === 'custom' ? form.endDate : null,
    reversal_multiple: form.reversalMultiple,
    atr_window: form.atrWindow,
    min_bars: form.minBars,
    horizon: form.horizon,
    value_ids: [...selectedValues.value],
    signal_ids: [...selectedSignals.value],
    candidate_limit: form.candidateLimit,
    risk_trials: form.riskTrials,
    initial_cash: form.initialCash,
    commission: form.commission,
    min_commission: form.minCommission,
    stamp_tax: form.stampTax,
    slippage: form.slippage,
    execution: form.execution,
    position_mode: form.positionMode,
    fixed_size: form.positionMode === 'fixed' ? form.fixedSize : null,
  }
}

async function pollJob(jobId: string): Promise<void> {
  running.value = true
  try {
    for (;;) {
      const state = await getIndividualResearchJob(jobId)
      job.value = state
      if (state.status === 'completed') {
        result.value = state.result
        if (result.value) result.value.history_id = jobId
        showMessage(state.result?.reason ?? '个股研究完成。')
        await loadHistory()
        return
      }
      if (state.status === 'failed') {
        showMessage(state.error ?? '个股研究失败，请检查配置和本地行情。', true)
        return
      }
      if (state.status === 'cancelled') {
        showMessage('个股研究已取消。')
        return
      }
      await new Promise((resolve) => window.setTimeout(resolve, 500))
    }
  } catch (error) {
    showMessage(apiMessage(error, '读取个股研究任务状态失败。'), true)
  } finally {
    running.value = false
  }
}

async function submit(): Promise<void> {
  errors.value = validate()
  showMessage('')
  result.value = null
  if (Object.values(errors.value).some(Boolean)) return
  try {
    const created = await createIndividualResearch(buildPayload())
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
      description: '个股研究',
      phase: 'queued',
      metadata: {},
      cancel_requested: false,
    }
    await pollJob(created.job_id)
  } catch (error) {
    showMessage(apiMessage(error, '个股研究任务提交失败。'), true)
  }
}

async function cancel(): Promise<void> {
  if (!job.value || !running.value) return
  try {
    await cancelIndividualResearch(job.value.job_id)
    showMessage('正在取消个股研究…')
  } catch (error) {
    showMessage(apiMessage(error, '取消个股研究失败。'), true)
  }
}

async function loadHistory(): Promise<void> {
  historyLoading.value = true
  try {
    history.value = await fetchIndividualResearchHistory()
  } catch (error) {
    showMessage(apiMessage(error, '读取个股研究历史失败。'), true)
  } finally {
    historyLoading.value = false
  }
}

async function loadHistoryDetail(historyId: string): Promise<void> {
  historyBusyId.value = historyId
  try {
    const record = await getIndividualResearchHistory(historyId)
    result.value = { ...record.result, history_id: historyId }
    selectedInstrumentRange.value = null
    job.value = null
    const request = record.request
    if (request.market === 'SH' || request.market === 'SZ') form.market = request.market
    if (typeof request.code === 'string') form.code = request.code
    form.name = typeof request.name === 'string'
      ? request.name
      : typeof record.result.name === 'string' ? record.result.name : ''
    if (request.date_mode === 'custom' || request.date_mode === 'full' || request.date_mode === 'recent5') {
      form.dateMode = request.date_mode
    }
    form.startDate = typeof request.start_date === 'string' ? request.start_date : ''
    form.endDate = typeof request.end_date === 'string' ? request.end_date : ''
    if (typeof request.reversal_multiple === 'number') form.reversalMultiple = request.reversal_multiple
    if (typeof request.atr_window === 'number') form.atrWindow = request.atr_window
    if (typeof request.min_bars === 'number') form.minBars = request.min_bars
    if (typeof request.horizon === 'number') form.horizon = request.horizon
    if ([1000, 2000, 3000].includes(Number(request.risk_trials))) {
      form.riskTrials = Number(request.risk_trials) as 1000 | 2000 | 3000
    }
    if (typeof request.candidate_limit === 'number') form.candidateLimit = request.candidate_limit
    if (typeof request.initial_cash === 'number') form.initialCash = request.initial_cash
    if (typeof request.commission === 'number') form.commission = request.commission
    if (typeof request.min_commission === 'number') form.minCommission = request.min_commission
    if (typeof request.stamp_tax === 'number') form.stampTax = request.stamp_tax
    if (typeof request.slippage === 'number') form.slippage = request.slippage
    if (request.execution === 'next_open' || request.execution === 'next_close') form.execution = request.execution
    if (request.position_mode === 'full' || request.position_mode === 'fixed') form.positionMode = request.position_mode
    if (typeof request.fixed_size === 'number') form.fixedSize = request.fixed_size
    if (Array.isArray(request.value_ids)) selectedValues.value = new Set(request.value_ids.filter((item): item is string => typeof item === 'string'))
    if (Array.isArray(request.signal_ids)) selectedSignals.value = new Set(request.signal_ids.filter((item): item is string => typeof item === 'string'))
    searchText.value = `${form.market} ${form.code}${form.name ? ` · ${form.name}` : ''}`
    showMessage(`已载入 ${form.market} ${form.code} 的个股研究历史。`)
  } catch (error) {
    showMessage(apiMessage(error, '读取个股研究历史详情失败。'), true)
  } finally {
    historyBusyId.value = ''
  }
}

async function deleteHistory(historyId: string): Promise<void> {
  if (!window.confirm('删除这条个股研究历史？')) return
  try {
    await deleteIndividualResearchHistory(historyId)
    if (result.value?.history_id === historyId) result.value = null
    await loadHistory()
    showMessage('个股研究历史已删除。')
  } catch (error) {
    showMessage(apiMessage(error, '删除个股研究历史失败。'), true)
  }
}

function validationMetrics(candidate: IndividualResearchCandidate): IndividualResearchCandidate['metrics']['validation'] {
  return candidate.metrics.validation ?? null
}

function formatPercent(value: number | null | undefined): string {
  return typeof value === 'number' && Number.isFinite(value) ? `${(value * 100).toFixed(2)}%` : '—'
}

function formatNumber(value: number | null | undefined, digits = 2): string {
  return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(digits) : '—'
}

function formatSignedPercent(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—'
  const sign = value > 0 ? '+' : ''
  return `${sign}${(value * 100).toFixed(2)}%`
}

function ruleText(candidate: IndividualResearchCandidate, side: 'buy' | 'sell'): string {
  const storedRule = candidate.condition_rules?.[side]
  const nestedRule = storedRule?.rule
  const storedDirectRule = storedRule && (
    Array.isArray(storedRule.signals) || Array.isArray(storedRule.conditions)
  ) ? storedRule : null
  const legacyNestedRule = nestedRule && typeof nestedRule === 'object'
    ? nestedRule as Record<string, unknown>
    : null
  const configRule = side === 'buy' ? candidate.config.buy_rule : candidate.config.sell_rule
  const fallbackRule = configRule && typeof configRule === 'object'
    ? configRule as unknown as Record<string, unknown>
    : null
  const rule = storedDirectRule ?? legacyNestedRule ?? fallbackRule
  if (!rule) return '—'
  const signals = Array.isArray(rule.signals) ? rule.signals.filter((item): item is string => typeof item === 'string') : []
  const conditions = Array.isArray(rule.conditions) ? rule.conditions : []
  const operatorSymbols: Record<string, string> = {
    gt: '>',
    gte: '≥',
    lt: '<',
    lte: '≤',
    eq: '=',
    neq: '≠',
    cross_up: '上穿',
    cross_down: '下穿',
  }
  const parts = [
    ...signals,
    ...conditions.flatMap((item) => {
      if (!item || typeof item !== 'object') return []
      const condition = item as Record<string, unknown>
      const operator = String(condition.operator ?? '')
      return [`${String(condition.left_value ?? '')} ${operatorSymbols[operator] ?? operator} ${String(condition.right_constant ?? condition.right_value ?? '')}`]
    }),
  ]
  const separator = rule.combine_mode === 'any' ? ' 或 ' : ' 且 '
  return parts.join(parts.length > 1 ? separator : ' / ') || '—'
}

function riskControlsNote(candidate: IndividualResearchCandidate): string {
  const config = candidate.config as unknown as Record<string, unknown>
  const parts = [
    ['止损', 'stop_loss_pct', 'stop_loss_atr_multiplier', 'stop_loss_cooldown_days'],
    ['止盈', 'take_profit_pct', 'take_profit_atr_multiplier', 'take_profit_cooldown_days'],
    ['移动止盈', 'trailing_stop_pct', 'trailing_stop_atr_multiplier', 'trailing_stop_cooldown_days'],
  ].flatMap(([label, pctKey, atrKey, cooldownKey]) => {
    const pct = config[pctKey]
    const atr = config[atrKey]
    const cooldown = config[cooldownKey]
    const cooldownText = typeof cooldown === 'number' && cooldown > 0 ? `/冷却${cooldown}日` : ''
    if (typeof pct === 'number') return [`${label} ${(pct * 100).toFixed(0)}%${cooldownText}`]
    if (typeof atr === 'number') return [`${label} ATR×${atr}${cooldownText}`]
    return [`${label}关闭`]
  })
  return parts.join(' · ')
}

function candidateName(candidate: IndividualResearchCandidate): string {
  return `${form.market} ${form.code} · 个股策略 ${candidate.candidate_hash.slice(0, 8)}`.slice(0, 60)
}

async function saveCandidate(candidate: IndividualResearchCandidate): Promise<void> {
  if (!result.value) return
  savingCandidateId.value = candidate.candidate_hash
  try {
    const validation = validationMetrics(candidate)
    const request = candidate.config
    await createStrategy({
      name: candidateName(candidate),
      note: `来源：个股研究 ${result.value.history_id ?? ''}；${form.market} ${form.code}；${riskControlsNote(candidate)}`.slice(0, 500),
      request,
      summary: {
        start_date: request.start_date ?? result.value.start_date,
        end_date: request.end_date ?? result.value.end_date,
        bars: result.value.date_range.days,
        max_positions: 1,
        ranking_value: null,
        rank_order: null,
        universe: 'codes',
        total_candidates: 1,
        performance: validation ? {
          total_return: validation.total_return ?? null,
          annual_return: validation.annual_return ?? null,
          max_drawdown: validation.max_drawdown ?? null,
          sharpe: validation.sharpe ?? null,
          win_rate: validation.win_rate ?? null,
          total_trades: validation.total_trades ?? null,
        } : {},
      },
      source_history_id: result.value.history_id ?? null,
    })
    const next = new Set(savedCandidateIds.value)
    next.add(candidate.candidate_hash)
    savedCandidateIds.value = next
    showMessage('策略已保存到策略库；请到策略追踪页检查后手动上线。')
  } catch (error) {
    showMessage(apiMessage(error, '保存个股策略失败。'), true)
  } finally {
    savingCandidateId.value = ''
  }
}

function splitMetricsText(candidate: IndividualResearchCandidate, split: 'train' | 'validation' | 'holdout'): string {
  const metrics = candidate.metrics[split]
  if (!metrics) return '未复核'
  return [
    `收益 ${formatPercent(metrics.total_return)}`,
    `年化 ${formatPercent(metrics.annual_return)}`,
    `夏普 ${formatNumber(metrics.sharpe, 3)}`,
    `胜率 ${formatPercent(metrics.win_rate)}`,
    `盈亏比 ${formatNumber(metrics.payoff_ratio, 2)}`,
    `回撤 ${formatPercent(metrics.max_drawdown)}`,
    `${metrics.closed_trades ?? metrics.total_trades ?? 0} 笔`,
  ].join(' · ')
}

function fullBacktestKey(board: 'sharpe' | 'return', candidate: IndividualResearchCandidate): string {
  return `${board}:${candidate.candidate_hash}`
}

function toggleFullBacktest(key: string): void {
  expandedFullBacktestKey.value = expandedFullBacktestKey.value === key ? '' : key
}

function candidateFullBacktest(candidate: IndividualResearchCandidate): IndividualResearchFullBacktest | null {
  const candidateId = candidate.full_backtest_id ?? candidate.candidate_hash
  return result.value?.full_backtests?.[candidateId] ?? null
}

function fullBacktestText(candidate: IndividualResearchCandidate): string {
  const full = candidateFullBacktest(candidate)
  if (!full) return '完整区间回测明细不可用'
  if (full.error) return `回测失败：${full.error}`
  const metrics = full.performance
  return [
    `策略收益 ${formatPercent(metrics.total_return)}`,
    `持有基准 ${full.benchmark ? formatSignedPercent(full.benchmark_return) : '不可用'}`,
    `超额收益 ${formatSignedPercent(full.excess_return)}`,
    `年化 ${formatPercent(metrics.annual_return)}`,
    `夏普 ${formatNumber(metrics.sharpe, 3)}`,
    `回撤 ${formatPercent(metrics.max_drawdown)}`,
    `胜率 ${formatPercent(metrics.win_rate)}`,
    `盈亏比 ${formatNumber(metrics.payoff_ratio, 2)}`,
    `${full.closed_trades ?? full.trades.length} 笔交易`,
  ].join(' · ')
}

function formatTime(value: string): string {
  return value ? value.replace('T', ' ').slice(0, 16) : '—'
}

onMounted(async () => {
  try {
    metadata.value = await fetchMetadata()
    if (!selectedValues.value.size) setAllValues()
    if (!selectedSignals.value.size) setAllSignals()
    await loadHistory()
    const active = (await fetchActiveJobs()).find((item) => item.description === '个股研究')
    if (active) {
      const state = await getIndividualResearchJob(active.job_id)
      job.value = state
      if (state.status === 'queued' || state.status === 'running') await pollJob(state.job_id)
      else if (state.status === 'completed') result.value = state.result
    }
  } catch (error) {
    showMessage(apiMessage(error, '个股研究初始化失败，请检查本地服务。'), true)
  }
})
</script>

<template>
  <div class="screen-page individual-research-page" data-testid="individual-research-page">
    <header class="topbar">
      <a class="brand" href="/indicator-lab" aria-label="指标实验室首页">
        <img class="brand-mark" src="/indicator-lab-mark.png" alt="" aria-hidden="true" width="34" height="34">
        <span><strong>Indicator Lab</strong><small>指标实验室</small></span>
      </a>
      <nav aria-label="主导航">
        <a class="nav-link" href="/indicator-lab">公式选股</a>
        <a class="nav-link" href="/portfolio-backtest">组合回测</a>
        <a class="nav-link" href="/strategy-lab">策略实验室</a>
        <a class="nav-link" href="/condition-research">买卖条件研究</a>
        <a class="nav-link active" href="/individual-research" aria-current="page">个股研究</a>
        <a class="nav-link" href="/strategy-fitness">策略适配性</a>
        <a class="nav-link" href="/market-data">本地行情</a>
        <a class="nav-link" href="/swing-research">波段研究</a>
        <a class="nav-link" href="/strategy-tracking">策略追踪</a>
        <a class="nav-link" href="/factor-test">因子检验</a>
        <a class="nav-link" href="/history-compare">历史对比</a>
        <a class="nav-link" href="/instrument-lab">标的检验台</a>
        <a class="nav-link" href="/candidate-library">候选库</a>
        <a class="nav-link" href="/factor-attribution">因子归因</a>
        <a class="nav-link" href="/user-manual">使用手册</a>
      </nav>
      <div class="topbar-status"><span class="status-dot" aria-hidden="true"></span> 本地数据模式</div>
    </header>

    <main class="content-shell">
      <section class="page-intro">
        <div><span class="section-kicker">SINGLE SECURITY RESEARCH</span><h1>个股研究</h1>
          <p>默认研究所选股票的全部本地历史；系统挖掘买卖条件、优化退出风控，并自动回测最终候选供你比较。</p>
        </div>
      </section>
      <PageGuide page-key="individual-research" />
      <p v-if="message" class="notice" :class="{ error: messageIsError }" role="status" data-testid="individual-research-message">{{ message }}</p>

      <div class="workspace-grid">
        <form class="config-panel panel" data-testid="individual-research-form" @submit.prevent="submit">
          <div class="panel-heading"><div><span class="section-kicker">01 / TARGET</span><h2>研究配置</h2></div></div>
          <fieldset class="form-section">
            <legend>研究对象</legend>
            <label for="individual-stock-search">从本地行情选择一只股票</label>
            <div class="individual-instrument-search">
              <input id="individual-stock-search" v-model="searchText" data-testid="individual-stock-search" type="search" placeholder="输入代码或名称" @input="scheduleInstrumentSearch">
              <button class="secondary-button" type="button" data-testid="individual-stock-search-button" :disabled="instrumentLoading" @click="searchInstruments">{{ instrumentLoading ? '搜索中…' : '搜索' }}</button>
            </div>
            <div v-if="instrumentOptions.length" class="individual-instrument-options" data-testid="individual-stock-options">
              <button v-for="item in instrumentOptions" :key="`${item.market}-${item.code}`" type="button" class="individual-instrument-option" @click="selectInstrument(item)">
                <strong>{{ item.market }} {{ item.code }}</strong><span>{{ item.name || '未同步名称' }}</span><small>{{ item.board ? BOARD_OPTIONS.find((board) => board.key === item.board)?.label : '股票' }}</small>
              </button>
            </div>
            <p v-if="instrumentError" class="field-error">{{ instrumentError }}</p>
            <p v-if="errors.code" class="field-error">{{ errors.code }}</p>
            <p v-if="form.code" class="scope-helper">当前：{{ form.market }} {{ form.code }}<template v-if="form.name"> · {{ form.name }}</template></p>
          </fieldset>

          <DateRangeSelector v-model="dateRange" prefix="individual" testid="individual-date-range" default-mode="full" helper="默认使用所选股票全部本地行情；按交易日切成训练 / 验证 / 留出 60/20/20，并清除前视窗口边界。" />
          <p v-if="selectedInstrumentRange" class="scope-helper" data-testid="individual-stock-date-range">
            本地行情覆盖：{{ selectedInstrumentRange.data_start ?? '未知' }} → {{ selectedInstrumentRange.data_end ?? '未知' }} · {{ selectedInstrumentRange.bars }} 根日线
          </p>
          <p v-else-if="result" class="scope-helper" data-testid="individual-stock-date-range">
            本次研究区间：{{ result.start_date }} → {{ result.end_date }} · {{ result.date_range.days }} 个交易日
          </p>
          <p v-else class="scope-helper" data-testid="individual-stock-date-range">选择股票后会显示该股票本地行情的起止日期和日线数量。</p>
          <p v-if="errors.startDate" class="field-error">{{ errors.startDate }}</p><p v-if="errors.endDate" class="field-error">{{ errors.endDate }}</p>

          <fieldset class="form-section">
            <legend>波段定义</legend>
            <div class="compact-fields">
              <div><label for="individual-reversal">反转阈值（ATR 倍数）</label><input id="individual-reversal" v-model.number="form.reversalMultiple" data-testid="individual-reversal" type="number" min="0.1" max="20" step="0.1"></div>
              <div><label for="individual-atr">ATR 窗口</label><input id="individual-atr" v-model.number="form.atrWindow" data-testid="individual-atr" type="number" min="1" max="250" step="1"></div>
              <div><label for="individual-min-bars">最短波段（交易日）</label><input id="individual-min-bars" v-model.number="form.minBars" data-testid="individual-min-bars" type="number" min="0" max="1000" step="1"></div>
              <div><label for="individual-horizon">启动预测窗口（交易日）</label><input id="individual-horizon" v-model.number="form.horizon" data-testid="individual-horizon" type="number" min="1" max="250" step="1"></div>
            </div>
          </fieldset>

          <fieldset class="form-section">
            <legend>指标与信号</legend>
            <div class="individual-picker-actions">
              <span>指标 {{ selectedValues.size }} / {{ searchableValues.length }}</span>
              <button type="button" class="table-link" @click="setAllValues">全选指标</button>
              <button type="button" class="table-link" @click="clearValues">清空指标</button>
            </div>
            <div class="individual-picker-list" data-testid="individual-value-picker">
              <label v-for="value in searchableValues" :key="value.id" class="individual-picker-item">
                <input type="checkbox" :checked="selectedValues.has(value.id)" @change="toggleValue(value.id)">
                <span>{{ value.display_name }}</span>
              </label>
            </div>
            <p v-if="errors.valueIds" class="field-error">{{ errors.valueIds }}</p>
            <div class="individual-picker-actions individual-signal-heading">
              <span>买卖信号 {{ selectedSignals.size }} / {{ availableSignals.length }}</span>
              <button type="button" class="table-link" @click="setAllSignals">全选信号</button>
              <button type="button" class="table-link" @click="clearSignals">清空信号</button>
            </div>
            <div class="individual-picker-list" data-testid="individual-signal-picker">
              <label v-for="signal in availableSignals" :key="signal.id" class="individual-picker-item">
                <input type="checkbox" :checked="selectedSignals.has(signal.id)" @change="toggleSignal(signal.id)">
                <span :title="signal.description">{{ signal.display_name }}</span>
              </label>
            </div>
          </fieldset>

          <fieldset class="form-section">
            <legend>计算与仓位</legend>
            <div class="compact-fields">
              <div><label for="individual-risk-trials">退出风控寻优强度</label><select id="individual-risk-trials" v-model.number="form.riskTrials" data-testid="individual-risk-trials"><option :value="1000">低 · 1000 组</option><option :value="2000">中 · 2000 组</option><option :value="3000">高 · 3000 组</option></select></div>
              <div><label for="individual-candidate-limit">每侧候选规则上限</label><input id="individual-candidate-limit" v-model.number="form.candidateLimit" data-testid="individual-candidate-limit" type="number" min="1" max="10" step="1"></div>
              <div><label for="individual-cash">初始资金</label><input id="individual-cash" v-model.number="form.initialCash" data-testid="individual-cash" type="number" min="1" step="1"></div>
              <div><label for="individual-execution">成交方式</label><select id="individual-execution" v-model="form.execution" data-testid="individual-execution"><option value="next_open">下一根开盘</option><option value="next_close">下一根收盘</option></select></div>
              <div><label for="individual-position-mode">仓位方式</label><select id="individual-position-mode" v-model="form.positionMode" data-testid="individual-position-mode"><option value="full">按可用资金</option><option value="fixed">固定股数</option></select></div>
              <div v-if="form.positionMode === 'fixed'"><label for="individual-fixed-size">固定股数</label><input id="individual-fixed-size" v-model.number="form.fixedSize" data-testid="individual-fixed-size" type="number" min="100" step="100"><p v-if="errors.fixedSize" class="field-error">{{ errors.fixedSize }}</p></div>
            </div>
            <p class="helper">单股条件可用指标库全部数值指标，包括不用于跨股票排序的价格尺度指标。训练段搜索单指标条件与双指标 AND / OR 组合；每侧最多 10 条、最多验证 100 组买卖组合，双榜各前五组进入退出风控寻优。</p>
          </fieldset>

          <div class="action-stack">
            <button class="primary-button" data-testid="individual-start" type="submit" :disabled="!canSubmit">{{ running ? '个股研究中…' : '开始个股研究' }}</button>
            <button v-if="running" class="secondary-button" data-testid="individual-cancel" type="button" @click="cancel">取消研究</button>
          </div>
        </form>

        <section class="results-panel panel" data-testid="individual-results" aria-live="polite">
          <div class="panel-heading"><div><span class="section-kicker">02 / RESULTS</span><h2>{{ result ? `${result.market} ${result.code} 个股研究` : '研究结果' }}</h2></div></div>
          <div v-if="running && job" class="task-progress-panel" data-testid="individual-progress">
            <div class="task-progress-heading"><span>{{ job.metadata.phase_label ?? job.phase ?? '准备中' }}</span><strong>{{ Math.round(job.progress * 100) }}%</strong></div>
            <div class="progress-track"><span :style="{ width: `${Math.round(job.progress * 100)}%` }"></span></div>
            <small>{{ job.metadata.phase_label ?? '后台正在逐阶段计算买卖条件与退出风控' }}</small>
          </div>
          <div v-if="!result" class="empty-state" data-testid="individual-empty">
            <div class="empty-icon" aria-hidden="true">/</div><h3>{{ running ? '正在识别波段并搜索买卖条件…' : '还没有个股研究结果' }}</h3>
            <p>{{ running ? '训练段挖掘条件后，系统会对验证段入围规则优化止盈止损。' : '选择一只本地行情中的股票开始研究。' }}</p>
          </div>
          <template v-else>
            <div v-if="result.reason" class="notice" data-testid="individual-result-reason">{{ result.reason }}</div>
            <div class="metrics-strip individual-segmentation" data-testid="individual-segmentation">
              <div><span>上涨波段</span><strong>{{ result.segmentation.up_segments ?? '—' }}</strong></div>
              <div><span>下跌波段</span><strong>{{ result.segmentation.down_segments ?? '—' }}</strong></div>
              <div><span>完成波段</span><strong>{{ result.segmentation.completed_segments ?? '—' }}</strong></div>
              <div><span>中位波段幅度</span><strong>{{ formatPercent(result.segmentation.median_up_amplitude as number | null) }} / {{ formatPercent(result.segmentation.median_down_amplitude as number | null) }}</strong></div>
            </div>
            <div class="individual-split-grid" data-testid="individual-splits">
              <div v-for="phase in ['train', 'validation', 'holdout'] as const" :key="phase" class="individual-split-card">
                <strong>{{ phase === 'train' ? '训练段' : phase === 'validation' ? '验证段' : '留出段' }}</strong>
                <span>{{ result.split_metadata[phase].start ?? '—' }} → {{ result.split_metadata[phase].end ?? '—' }}</span>
                <small>{{ result.split_metadata[phase].days }} 个交易日 · 清除边界 {{ result.split_metadata[phase].purged_days }} 日</small>
              </div>
            </div>

            <section class="individual-leaderboard" data-testid="individual-sharpe-board">
              <div class="section-heading"><h3>验证段夏普榜</h3><span class="required-hint">验证段排序 · 留出段只复核 · 全区间明细供对比</span></div>
              <div v-if="!sharpeCandidates.length" class="inline-empty">暂无可计算候选。</div>
              <div v-else class="table-wrap"><table><thead><tr><th>买入规则</th><th>卖出规则</th><th>退出风控</th><th>训练段</th><th>验证段</th><th>留出段</th><th>全区间回测</th><th>样本 / 风险</th><th>策略追踪</th></tr></thead><tbody>
                <tr v-for="(candidate, index) in sharpeCandidates" :key="candidate.candidate_hash" :data-testid="`individual-sharpe-${index}`">
                  <td>{{ ruleText(candidate, 'buy') }}</td><td>{{ ruleText(candidate, 'sell') }}</td><td>{{ riskControlsNote(candidate) }}</td>
                  <td class="individual-split-metrics">{{ splitMetricsText(candidate, 'train') }}</td><td class="individual-split-metrics">{{ splitMetricsText(candidate, 'validation') }}</td><td class="individual-split-metrics">{{ splitMetricsText(candidate, 'holdout') }}</td>
                  <td class="individual-split-metrics"><span>{{ fullBacktestText(candidate) }}</span><button v-if="candidateFullBacktest(candidate) && !candidateFullBacktest(candidate)?.error" class="table-link" type="button" :data-testid="`individual-full-details-sharpe-${index}`" @click="toggleFullBacktest(fullBacktestKey('sharpe', candidate))">{{ expandedFullBacktestKey === fullBacktestKey('sharpe', candidate) ? '收起交易明细' : '查看交易明细' }}</button></td>
                  <td><span v-for="badge in candidate.quality?.badges ?? []" :key="badge" class="individual-badge" :class="{ danger: badge === '风险高', warning: badge === '样本不足' }">{{ badge }}</span></td>
                  <td><button class="secondary-button" type="button" :data-testid="`individual-save-sharpe-${index}`" :disabled="savingCandidateId === candidate.candidate_hash || savedCandidateIds.has(candidate.candidate_hash)" @click="saveCandidate(candidate)">{{ savedCandidateIds.has(candidate.candidate_hash) ? '已保存' : savingCandidateId === candidate.candidate_hash ? '保存中…' : '保存并手动追踪' }}</button></td>
                </tr>
                <template v-for="(candidate, index) in sharpeCandidates" :key="`full-${candidate.candidate_hash}`"><tr v-if="expandedFullBacktestKey === fullBacktestKey('sharpe', candidate)" :data-testid="`individual-full-backtest-sharpe-${index}`"><td colspan="9"><IndividualResearchBacktestDetail :backtest="candidateFullBacktest(candidate)" /></td></tr></template>
              </tbody></table></div>
            </section>

            <section class="individual-leaderboard" data-testid="individual-return-board">
              <div class="section-heading"><h3>验证段年化收益榜</h3><span class="required-hint">验证段排序 · 双榜候选去重</span></div>
              <div v-if="!returnCandidates.length" class="inline-empty">暂无可计算候选。</div>
              <div v-else class="table-wrap"><table><thead><tr><th>买入规则</th><th>卖出规则</th><th>退出风控</th><th>训练段</th><th>验证段</th><th>留出段</th><th>全区间回测</th><th>样本 / 风险</th><th>策略追踪</th></tr></thead><tbody>
                <tr v-for="(candidate, index) in returnCandidates" :key="candidate.candidate_hash" :data-testid="`individual-return-${index}`">
                  <td>{{ ruleText(candidate, 'buy') }}</td><td>{{ ruleText(candidate, 'sell') }}</td><td>{{ riskControlsNote(candidate) }}</td>
                  <td class="individual-split-metrics">{{ splitMetricsText(candidate, 'train') }}</td><td class="individual-split-metrics">{{ splitMetricsText(candidate, 'validation') }}</td><td class="individual-split-metrics">{{ splitMetricsText(candidate, 'holdout') }}</td>
                  <td class="individual-split-metrics"><span>{{ fullBacktestText(candidate) }}</span><button v-if="candidateFullBacktest(candidate) && !candidateFullBacktest(candidate)?.error" class="table-link" type="button" :data-testid="`individual-full-details-return-${index}`" @click="toggleFullBacktest(fullBacktestKey('return', candidate))">{{ expandedFullBacktestKey === fullBacktestKey('return', candidate) ? '收起交易明细' : '查看交易明细' }}</button></td>
                  <td><span v-for="badge in candidate.quality?.badges ?? []" :key="badge" class="individual-badge" :class="{ danger: badge === '风险高', warning: badge === '样本不足' }">{{ badge }}</span></td>
                  <td><button class="secondary-button" type="button" :data-testid="`individual-save-return-${index}`" :disabled="savingCandidateId === candidate.candidate_hash || savedCandidateIds.has(candidate.candidate_hash)" @click="saveCandidate(candidate)">{{ savedCandidateIds.has(candidate.candidate_hash) ? '已保存' : savingCandidateId === candidate.candidate_hash ? '保存中…' : '保存并手动追踪' }}</button></td>
                </tr>
                <template v-for="(candidate, index) in returnCandidates" :key="`full-${candidate.candidate_hash}`"><tr v-if="expandedFullBacktestKey === fullBacktestKey('return', candidate)" :data-testid="`individual-full-backtest-return-${index}`"><td colspan="9"><IndividualResearchBacktestDetail :backtest="candidateFullBacktest(candidate)" /></td></tr></template>
              </tbody></table></div>
            </section>
            <p class="scope-helper">保存后会写入现有策略库；到策略追踪页检查策略名称与股票代码，再手动上线。未选中的候选不会自动加入追踪。</p>
          </template>
        </section>
      </div>

      <section class="panel individual-history" data-testid="individual-history">
        <div class="panel-heading"><div><span class="section-kicker">HISTORY</span><h2>个股研究历史</h2></div><button class="secondary-button" type="button" :disabled="historyLoading" @click="loadHistory">{{ historyLoading ? '读取中…' : '刷新' }}</button></div>
        <div v-if="!history.length" class="inline-empty">暂无个股研究历史。</div>
        <div v-else class="table-wrap"><table><thead><tr><th>股票</th><th>完成时间</th><th>区间</th><th>候选</th><th>操作</th></tr></thead><tbody>
          <tr v-for="item in history" :key="item.history_id"><td>{{ item.summary.name || item.summary.code || '个股研究' }}<small class="muted-code">{{ item.summary.market }} {{ item.summary.code }}</small></td><td>{{ formatTime(item.finished_at) }}</td><td>{{ item.summary.start_date ?? '—' }} → {{ item.summary.end_date ?? '—' }}</td><td>{{ item.summary.candidate_count ?? 0 }}</td><td><button class="table-link" type="button" :disabled="historyBusyId !== ''" @click="loadHistoryDetail(item.history_id)">{{ historyBusyId === item.history_id ? '载入中…' : '载入' }}</button><button class="table-link danger-link" type="button" :disabled="historyBusyId !== ''" @click="deleteHistory(item.history_id)">删除</button></td></tr>
        </tbody></table></div>
      </section>
    </main>
  </div>
</template>

<style scoped>
.individual-instrument-search { display: flex; align-items: center; gap: 8px; }
.individual-instrument-search input { flex: 1; min-width: 0; }
.individual-instrument-options { display: grid; max-height: 240px; overflow: auto; margin-top: 6px; border: 1px solid var(--border); background: var(--surface); }
.individual-instrument-option { display: flex; align-items: center; gap: 10px; padding: 8px 10px; border: 0; border-bottom: 1px solid var(--border); background: transparent; color: var(--text); cursor: pointer; text-align: left; }
.individual-instrument-option:hover { background: var(--surface-raised); }
.individual-instrument-option span { flex: 1; }
.individual-instrument-option small { color: var(--subtle); }
.individual-picker-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; margin: 7px 0; color: var(--subtle); font-size: 11px; }
.individual-signal-heading { margin-top: 14px; }
.individual-picker-list { display: grid; grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); gap: 5px 10px; max-height: 190px; overflow-y: auto; padding: 8px; border: 1px solid var(--border); }
.individual-picker-item { display: flex; align-items: center; gap: 7px; min-width: 0; color: var(--muted); font-size: 11px; }
.individual-picker-item span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.individual-segmentation { margin-bottom: 12px; }
.individual-split-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin: 10px 0 18px; }
.individual-split-card { display: grid; gap: 5px; padding: 10px; border: 1px solid var(--border); background: var(--surface); color: var(--muted); font-size: 11px; }
.individual-split-card strong { color: var(--text); }
.individual-split-card small { color: var(--subtle); }
.individual-leaderboard { margin-top: 18px; }
.individual-leaderboard .table-wrap { max-height: 560px; }
.individual-leaderboard table { min-width: 1700px; }
.individual-split-metrics { max-width: 300px; color: var(--muted); font-size: 10px; line-height: 1.6; }
.individual-badge { display: inline-block; margin: 2px; padding: 3px 5px; border: 1px solid rgba(34, 197, 94, 0.35); border-radius: 4px; color: var(--accent); font-size: 10px; white-space: nowrap; }
.individual-badge.warning { border-color: rgba(245, 158, 11, 0.4); color: var(--warning); }
.individual-badge.danger, .individual-risk-high { color: var(--danger); }
.individual-history { margin-top: 20px; }
.individual-history small { display: block; margin-top: 3px; }
@media (max-width: 900px) { .individual-split-grid { grid-template-columns: 1fr; } }
</style>
