<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'

import {
  cancelConditionResearch,
  createConditionResearch,
  fetchConditionResearchHistory,
  getConditionResearch,
  getConditionResearchHistory,
  getConditionResearchResults,
} from '../api/conditionResearch'
import { FormulaScreenApiError, fetchMetadata, parseFormula } from '../api/formulaScreen'
import { fetchActiveJobs } from '../api/jobs'
import DateRangeSelector from '../components/DateRangeSelector.vue'
import RuleGroupEditor from '../components/RuleGroupEditor.vue'
import type {
  ConditionResearchCandidate,
  ConditionResearchJobState,
  ConditionResearchPayload,
  ConditionResearchReport,
  ConditionResearchStockResult,
  CustomFormulaMetadata,
  DateRangeMode,
  DateRangeSelection,
  FormulaScreenMetadata,
  InstrumentBoard,
  RuleGroupPayload,
  ValueDefinition,
} from '../types'
import { valueOptionLabel } from '../utils/formulaScreen'

function emptyRule(): RuleGroupPayload {
  return { signals: [], conditions: [], required_conditions: [], combine_mode: 'all', minimum_matches: null }
}

interface ResearchHistorySummary {
  history_id: string
  created_at: string
  summary?: Record<string, unknown>
}

const metadata = ref<FormulaScreenMetadata | null>(null)
const customMetadata = ref<CustomFormulaMetadata | null>(null)
const parsedFormulaText = ref('')
const mode = ref<'preset' | 'custom'>('preset')
const form = reactive({
  universe: 'all' as 'all' | 'sh' | 'sz',
  boards: [] as InstrumentBoard[],
  buyRule: emptyRule(),
  sellRule: emptyRule(),
  formulaText: '',
  formulaParameters: {} as Record<string, number>,
  dateMode: 'recent5' as DateRangeMode,
  startDate: '',
  endDate: '',
  searchSides: 'both' as ConditionResearchPayload['search_sides'],
  minTrades: 20,
  minInstruments: 5,
  candidateLimit: 5,
  holdoutCandidates: 1,
  initialCash: 100_000,
  commission: 0.0001,
  minCommission: 0.1,
  stampTax: 0.001,
  slippage: 0,
  execution: 'next_open' as const,
})
const errors = ref<Record<string, string>>({})
const message = ref('')
const loading = ref(false)
const customParseLoading = ref(false)
const customParseError = ref('')
const showAdvanced = ref(false)
const job = ref<ConditionResearchJobState | null>(null)
const report = ref<ConditionResearchReport | null>(null)
const history = ref<ResearchHistorySummary[]>([])
const historyLoading = ref(false)
const selectedHistoryId = ref('')
const selectedStockView = ref('baseline:full')
const stockFilter = ref('')
const stockSort = ref<'win_rate' | 'payoff_ratio' | 'total_return' | 'sharpe' | 'code'>('win_rate')
const stockPage = ref(1)
const stockPageSize = 50

const availableValues = computed<ValueDefinition[]>(() => (
  mode.value === 'custom'
    ? customMetadata.value?.values ?? []
    : metadata.value?.indicators.flatMap((indicator) => indicator.values ?? []) ?? []
))
const dateRange = computed<DateRangeSelection>({
  get: () => ({ mode: form.dateMode, startDate: form.startDate, endDate: form.endDate }),
  set: (value) => {
    form.dateMode = value.mode
    form.startDate = value.startDate
    form.endDate = value.endDate
  },
})
const STOCK_BOARD_OPTIONS: { key: InstrumentBoard; label: string }[] = [
  { key: 'main', label: '主板' },
  { key: 'star', label: '科创板' },
  { key: 'chinext', label: '创业板' },
  { key: 'b_share', label: 'B 股' },
]
const selectedStockRows = computed(() => {
  if (!report.value) return []
  const [ruleId, period] = selectedStockView.value.split(':')
  if (ruleId === 'baseline') {
    return period === 'holdout'
      ? report.value.baseline.holdout?.stocks ?? []
      : report.value.baseline.full_period?.stocks ?? []
  }
  const candidate = report.value.candidates.find((item) => item.candidate_id === ruleId)
  if (!candidate) return []
  return period === 'holdout'
    ? candidate.holdout?.stocks ?? []
    : candidate.full_period?.stocks ?? []
})
const stockViewOptions = computed(() => {
  const options = [
    { value: 'baseline:full', label: '基础规则 · 全区间' },
    { value: 'baseline:holdout', label: '基础规则 · 最终留出' },
  ]
  for (const candidate of report.value?.candidates ?? []) {
    if (candidate.full_period) {
      options.push({
        value: candidate.candidate_id + ':full',
        label: (candidate.side === 'buy' ? '买入过滤' : '卖出触发')
          + ' · 全区间 · ' + conditionText(candidate),
      })
    }
    if (candidate.holdout) {
      options.push({
        value: candidate.candidate_id + ':holdout',
        label: (candidate.side === 'buy' ? '买入过滤' : '卖出触发')
          + ' · 最终留出 · ' + conditionText(candidate),
      })
    }
  }
  return options
})
const stockPeriodLabel = computed(() => (
  selectedStockView.value.endsWith(':holdout') ? '最终留出期' : '完整研究区间'
))
const filteredStockRows = computed(() => {
  const query = stockFilter.value.trim().toLowerCase()
  const matching = selectedStockRows.value.filter((stock) => (
    !query
    || stock.market.toLowerCase().includes(query)
    || stock.code.toLowerCase().includes(query)
    || (stock.name ?? '').toLowerCase().includes(query)
  ))
  return matching.sort((left, right) => {
    if (stockSort.value === 'code') {
      return (left.market + left.code).localeCompare(right.market + right.code)
    }
    const leftValue = left[stockSort.value]
    const rightValue = right[stockSort.value]
    return (rightValue ?? Number.NEGATIVE_INFINITY) - (leftValue ?? Number.NEGATIVE_INFINITY)
  })
})
const stockPageCount = computed(() => Math.max(1, Math.ceil(filteredStockRows.value.length / stockPageSize)))
const selectedPageRows = computed(() => (
  filteredStockRows.value.slice((stockPage.value - 1) * stockPageSize, stockPage.value * stockPageSize)
))

watch([selectedStockView, stockFilter, stockSort], () => {
  stockPage.value = 1
})

function initialRule(operator: 'gt' | 'lt'): RuleGroupPayload {
  const firstValue = availableValues.value[0]?.id ?? ''
  return {
    signals: [],
    conditions: firstValue
      ? [{ left_value: firstValue, operator, right_value: null, right_constant: 0 }]
      : [],
    required_conditions: [],
    combine_mode: 'all',
    minimum_matches: null,
  }
}

function resetRules(): void {
  form.buyRule = initialRule('gt')
  form.sellRule = initialRule('lt')
}

function toggleBoard(board: InstrumentBoard): void {
  form.boards = form.boards.includes(board)
    ? form.boards.filter((value) => value !== board)
    : [...form.boards, board]
}

function setBuyRule(rule: RuleGroupPayload): void {
  form.buyRule = { ...rule, signals: [] }
}

function setSellRule(rule: RuleGroupPayload): void {
  form.sellRule = { ...rule, signals: [] }
}

function setMode(next: 'preset' | 'custom'): void {
  if (mode.value === next) return
  mode.value = next
  customMetadata.value = null
  customParseError.value = ''
  if (next === 'preset') {
    parsedFormulaText.value = ''
    resetRules()
  } else {
    form.buyRule = emptyRule()
    form.sellRule = emptyRule()
  }
}

async function parseCustomFormula(): Promise<void> {
  customParseError.value = ''
  if (!form.formulaText.trim()) {
    customParseError.value = '请输入通达信公式后再解析。'
    return
  }
  customParseLoading.value = true
  try {
    const parsed = await parseFormula(form.formulaText.trim())
    customMetadata.value = parsed
    parsedFormulaText.value = form.formulaText.trim()
    form.formulaParameters = Object.fromEntries(
      parsed.parameters.map((parameter) => [
        parameter.name,
        form.formulaParameters[parameter.name] ?? parameter.default,
      ]),
    )
    form.buyRule = initialRule('gt')
    form.sellRule = initialRule('lt')
    errors.value = {}
  } catch (error) {
    customMetadata.value = null
    form.buyRule = emptyRule()
    form.sellRule = emptyRule()
    customParseError.value = error instanceof FormulaScreenApiError
      ? error.message
      : '公式解析失败，请检查语法。'
  } finally {
    customParseLoading.value = false
  }
}

function validate(): Record<string, string> {
  const next: Record<string, string> = {}
  if (!form.buyRule.conditions.length) {
    next.buyRule = '请至少设置一个买入指标条件。'
  }
  if (!form.sellRule.conditions.length) {
    next.sellRule = '请至少设置一个卖出指标条件。'
  }
  if (mode.value === 'custom') {
    if (!form.formulaText.trim()) next.formulaText = '请输入自定义公式。'
    else if (!customMetadata.value || parsedFormulaText.value !== form.formulaText.trim()) {
      next.formulaText = '请先解析当前公式。'
    }
    if (!availableValues.value.length) next.formulaText = '公式需要包含至少一个数值输出供条件挖掘。'
  }
  if (form.dateMode === 'custom') {
    if (!form.startDate) next.startDate = '请选择开始日期。'
    if (!form.endDate) next.endDate = '请选择结束日期。'
    if (form.startDate && form.endDate && form.startDate >= form.endDate) {
      next.dateRange = '研究区间至少需要 3 天，结束日期必须晚于开始日期。'
    }
  }
  if (!Number.isInteger(form.minTrades) || form.minTrades < 1) next.minTrades = '最少交易数必须是正整数。'
  if (!Number.isInteger(form.minInstruments) || form.minInstruments < 1) next.minInstruments = '覆盖股票数必须是正整数。'
  if (!Number.isInteger(form.candidateLimit) || form.candidateLimit < 1 || form.candidateLimit > 10) {
    next.candidateLimit = '候选条件数量必须在 1 到 10 之间。'
  }
  if (!Number.isInteger(form.holdoutCandidates) || form.holdoutCandidates < 1 || form.holdoutCandidates > 3) {
    next.holdoutCandidates = '最终留出候选数必须在 1 到 3 之间。'
  }
  return next
}

function buildPayload(): ConditionResearchPayload {
  return {
    universe: form.universe,
    ...(form.boards.length ? { boards: [...form.boards] as ConditionResearchPayload['boards'] } : {}),
    buy_rule: form.buyRule,
    sell_rule: form.sellRule,
    formula_text: mode.value === 'custom' ? form.formulaText.trim() : null,
    formula_parameters: mode.value === 'custom' ? { ...form.formulaParameters } : {},
    date_mode: form.dateMode,
    ...(form.dateMode === 'custom'
      ? { start_date: form.startDate, end_date: form.endDate }
      : {}),
    search_sides: form.searchSides,
    initial_cash: form.initialCash,
    commission: form.commission,
    min_commission: form.minCommission,
    stamp_tax: form.stampTax,
    slippage: form.slippage,
    execution: form.execution,
    min_trades: form.minTrades,
    min_instruments: form.minInstruments,
    candidate_limit: form.candidateLimit,
    holdout_candidates: form.holdoutCandidates,
  }
}

async function submit(): Promise<void> {
  errors.value = validate()
  message.value = ''
  if (Object.values(errors.value).some(Boolean)) return
  loading.value = true
  report.value = null
  try {
    const created = await createConditionResearch(buildPayload())
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
    await pollJob(created.job_id)
  } catch (error) {
    message.value = apiMessage(error)
  } finally {
    loading.value = false
  }
}

async function pollJob(jobId: string): Promise<void> {
  for (;;) {
    const state = await getConditionResearch(jobId)
    job.value = state
    if (state.status === 'completed') {
      report.value = await getConditionResearchResults(jobId)
      selectedStockView.value = 'baseline:full'
      message.value = '研究完成：处理 ' + report.value.processed + ' 只股票，发现 '
        + report.value.candidates.length + ' 条候选规则。'
      await loadHistory()
      return
    }
    if (state.status === 'failed' || state.status === 'cancelled') {
      message.value = state.error ?? (state.status === 'cancelled' ? '研究任务已取消。' : '买卖条件研究失败。')
      return
    }
    await new Promise((resolve) => window.setTimeout(resolve, 220))
  }
}

async function cancel(): Promise<void> {
  if (!job.value || !loading.value) return
  try {
    await cancelConditionResearch(job.value.job_id)
    message.value = '已请求取消，等待当前股票回测结束。'
  } catch (error) {
    message.value = apiMessage(error)
  }
}

async function loadHistory(): Promise<void> {
  historyLoading.value = true
  try {
    history.value = await fetchConditionResearchHistory() as ResearchHistorySummary[]
    if (!selectedHistoryId.value && history.value.length) {
      selectedHistoryId.value = history.value[0].history_id
    }
  } catch (error) {
    message.value = apiMessage(error)
  } finally {
    historyLoading.value = false
  }
}

async function resumeActiveJob(): Promise<void> {
  try {
    const active = (await fetchActiveJobs()).find((item) => item.description === '买卖条件研究')
    if (!active) return
    job.value = active as unknown as ConditionResearchJobState
    loading.value = true
    await pollJob(active.job_id)
  } catch (error) {
    message.value = apiMessage(error)
  } finally {
    loading.value = false
  }
}

async function loadHistoryDetail(): Promise<void> {
  if (!selectedHistoryId.value) return
  try {
    const saved = await getConditionResearchHistory(selectedHistoryId.value)
    report.value = saved.result
    selectedStockView.value = 'baseline:full'
    message.value = '已加载历史买卖条件研究。'
  } catch (error) {
    message.value = apiMessage(error)
  }
}

function formatPercent(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : (value * 100).toFixed(2) + '%'
}

function formatNumber(value: number | null | undefined, digits = 2): string {
  return value === null || value === undefined ? '—' : value.toFixed(digits)
}

function winRateDelta(stock: ConditionResearchStockResult): string {
  if (stock.win_rate === null || stock.baseline_win_rate === undefined || stock.baseline_win_rate === null) {
    return '—'
  }
  const delta = (stock.win_rate - stock.baseline_win_rate) * 100
  return (delta > 0 ? '+' : '') + delta.toFixed(2) + ' pp'
}

function conditionText(candidate: ConditionResearchCandidate): string {
  const labels: Record<string, string> = {
    gt: '>', gte: '≥', lt: '<', lte: '≤', eq: '=', neq: '≠', cross_up: '上穿', cross_down: '下穿',
  }
  const operator = labels[candidate.condition.operator] ?? candidate.condition.operator
  const leftValue = availableValues.value.find((value) => value.id === candidate.condition.left_value)
  const rightValue = candidate.condition.right_value
    ? availableValues.value.find((value) => value.id === candidate.condition.right_value)
    : null
  const leftLabel = leftValue ? valueOptionLabel(leftValue) : candidate.condition.left_value
  const rightLabel = rightValue
    ? valueOptionLabel(rightValue)
    : formatNumber(candidate.condition.right_constant, 3)
  return leftLabel + ' ' + operator + ' ' + rightLabel
}

function phaseCount(state: ConditionResearchJobState | null, key: 'phase_current' | 'phase_total'): number {
  const value = state?.metadata?.[key]
  return typeof value === 'number' && Number.isFinite(value) ? value : 0
}

function apiMessage(error: unknown): string {
  return error instanceof FormulaScreenApiError
    ? error.message
    : '买卖条件研究失败，请检查行情数据和配置后重试。'
}

onMounted(async () => {
  try {
    metadata.value = await fetchMetadata()
    resetRules()
    await loadHistory()
    await resumeActiveJob()
  } catch (error) {
    message.value = apiMessage(error)
  }
})
</script>

<template>
  <div class="screen-page condition-research-page" data-testid="condition-research-page">
    <header class="topbar">
      <a class="brand" href="/indicator-lab" aria-label="指标实验室首页">
        <img class="brand-mark" src="/indicator-lab-mark.png" alt="" aria-hidden="true" width="34" height="34">
        <span><strong>Indicator Lab</strong><small>指标实验室</small></span>
      </a>
      <nav aria-label="主导航">
        <a class="nav-link" href="/indicator-lab">公式选股</a>
        <a class="nav-link" href="/backtest">单股回测</a>
        <a class="nav-link" href="/portfolio-backtest">组合回测</a>
        <a class="nav-link" href="/strategy-lab">策略实验室</a>
        <a class="nav-link active" href="/condition-research" aria-current="page">买卖条件研究</a>
        <a class="nav-link" href="/strategy-fitness">策略适配性</a>
        <a class="nav-link" href="/market-data">本地行情</a>
      </nav>
      <div class="topbar-status"><span class="status-dot" aria-hidden="true"></span> 本地数据模式</div>
    </header>

    <main class="content-shell">
      <section class="page-intro">
        <div>
          <p class="eyebrow">BUY / SELL CONDITION RESEARCH</p>
          <h1>买卖条件研究</h1>
          <p class="intro-copy">从全市场逐股回测中，挖掘并验证更有效的买入、卖出条件。</p>
        </div>
        <div class="intro-note">
          <span class="note-label">研究口径</span>
          <strong>逐股独立 · 不做组合</strong>
          <small>基础规则固定，候选条件按时间留出验证</small>
        </div>
      </section>

      <div v-if="message" class="notice" :class="{ error: message.includes('失败') || message.includes('错误') }" role="alert" data-testid="condition-research-message">
        {{ message }}
      </div>

      <div class="workspace-grid condition-research-grid">
        <form class="config-panel panel condition-research-config" data-testid="condition-research-config" @submit.prevent="submit">
          <div class="panel-heading">
            <div><span class="section-kicker">01 / CONFIGURE</span><h2>基础买卖规则</h2></div>
            <span class="required-hint">* 必填</span>
          </div>

          <div class="formula-mode-tabs" role="tablist" aria-label="指标来源">
            <button type="button" :class="{ active: mode === 'preset' }" data-testid="condition-mode-preset" role="tab" :aria-selected="mode === 'preset'" @click="setMode('preset')">预置指标</button>
            <button type="button" :class="{ active: mode === 'custom' }" data-testid="condition-mode-custom" role="tab" :aria-selected="mode === 'custom'" @click="setMode('custom')">自定义公式</button>
          </div>

          <fieldset v-if="mode === 'custom'" class="form-section custom-formula-section">
            <legend>通达信公式</legend>
            <textarea v-model="form.formulaText" data-testid="condition-formula" rows="6" spellcheck="false" placeholder="例如：买入:CROSS(C,MA(C,20)); 卖出:CROSS(MA(C,20),C); 强度:C/MA(C,20);"></textarea>
            <button class="secondary-button" data-testid="condition-parse-formula" type="button" :disabled="customParseLoading" @click="parseCustomFormula">{{ customParseLoading ? '解析中…' : '解析公式' }}</button>
            <p class="helper">自动搜索会使用公式里的数值输出作为候选特征。</p>
            <p v-if="customParseError" class="field-error">{{ customParseError }}</p>
            <div v-if="customMetadata?.parameters.length" class="parameter-grid">
              <div v-for="parameter in customMetadata.parameters" :key="parameter.name">
                <label :for="'condition-param-' + parameter.name">{{ parameter.name }}</label>
                <input :id="'condition-param-' + parameter.name" v-model.number="form.formulaParameters[parameter.name]" :data-testid="'condition-param-' + parameter.name" type="number" :min="parameter.minimum" :max="parameter.maximum" :step="parameter.step">
              </div>
            </div>
          </fieldset>

          <fieldset class="form-section">
            <legend>股票范围</legend>
            <div class="compact-fields">
              <div>
                <label for="condition-universe">市场</label>
                <select id="condition-universe" v-model="form.universe" data-testid="condition-universe">
                  <option value="all">沪深全部股票</option>
                  <option value="sh">仅上海</option>
                  <option value="sz">仅深圳</option>
                </select>
              </div>
            </div>
            <div class="scope-options condition-board-options" data-testid="condition-board-scope">
              <span>板块</span>
              <label v-for="option in STOCK_BOARD_OPTIONS" :key="option.key">
                <input
                  type="checkbox"
                  :data-testid="'condition-board-' + option.key"
                  :checked="form.boards.includes(option.key)"
                  @change="toggleBoard(option.key)"
                >{{ option.label }}
              </label>
              <small>未勾选表示所选市场的全部股票板块。</small>
            </div>
            <p class="helper">每只股票独立运行同一套规则，不设置跨股票资金分配或持仓数量上限。</p>
          </fieldset>

          <RuleGroupEditor
            :model-value="form.buyRule"
            :values="availableValues"
            :condition-operators="metadata?.condition_operators"
            title="买入基础条件"
            testid="condition-buy-rule"
            :max-conditions="3"
            @update:model-value="setBuyRule"
          />
          <p v-if="errors.buyRule" class="field-error">{{ errors.buyRule }}</p>
          <RuleGroupEditor
            :model-value="form.sellRule"
            :values="availableValues"
            :condition-operators="metadata?.condition_operators"
            title="卖出基础条件"
            testid="condition-sell-rule"
            :max-conditions="3"
            @update:model-value="setSellRule"
          />
          <p v-if="errors.sellRule" class="field-error">{{ errors.sellRule }}</p>

          <fieldset class="form-section">
            <legend>搜索目标与验证</legend>
            <DateRangeSelector v-model="dateRange" prefix="condition" testid="condition-date-range" helper="按时间顺序使用 60% 寻优、20% 验证、20% 最终留出；默认最近五年已完成日线。" />
            <p v-if="errors.dateRange" class="field-error">{{ errors.dateRange }}</p>
            <div class="compact-fields condition-fields-top">
              <div>
                <label for="condition-search-sides">搜索哪一侧</label>
                <select id="condition-search-sides" v-model="form.searchSides" data-testid="condition-search-sides">
                  <option value="both">先分别搜索买入和卖出</option>
                  <option value="buy">只搜索买入条件</option>
                  <option value="sell">只搜索卖出条件</option>
                </select>
              </div>
              <div>
                <label for="condition-min-trades">最少交易样本</label>
                <input id="condition-min-trades" v-model.number="form.minTrades" data-testid="condition-min-trades" type="number" min="1" step="1">
              </div>
            </div>
            <p v-if="errors.minTrades" class="field-error">{{ errors.minTrades }}</p>
            <div class="compact-fields condition-fields-top">
              <div>
                <label for="condition-min-instruments">最少覆盖股票数</label>
                <input id="condition-min-instruments" v-model.number="form.minInstruments" data-testid="condition-min-instruments" type="number" min="1" step="1">
              </div>
              <div>
                <label for="condition-candidate-limit">最多候选条件/侧</label>
                <input id="condition-candidate-limit" v-model.number="form.candidateLimit" data-testid="condition-candidate-limit" type="number" min="1" max="10" step="1">
              </div>
            </div>
            <div class="compact-fields condition-fields-top">
              <div>
                <label for="condition-holdout-candidates">最终留出候选/侧</label>
                <input id="condition-holdout-candidates" v-model.number="form.holdoutCandidates" data-testid="condition-holdout-candidates" type="number" min="1" max="3" step="1">
              </div>
              <div><label>主目标</label><p class="field-static">跨股票交易胜率提升</p></div>
            </div>
            <p v-if="errors.holdoutCandidates" class="field-error">{{ errors.holdoutCandidates }}</p>
            <p v-if="errors.minInstruments" class="field-error">{{ errors.minInstruments }}</p>
            <p v-if="errors.candidateLimit" class="field-error">{{ errors.candidateLimit }}</p>
            <p class="helper">主目标是提高跨股票合并交易胜率；候选还需保持盈亏比和平均单笔净收益，并通过最终留出期复验。</p>
          </fieldset>

          <button class="advanced-toggle" data-testid="condition-advanced-toggle" type="button" :aria-expanded="showAdvanced" @click="showAdvanced = !showAdvanced">
            <span>{{ showAdvanced ? '收起交易费用' : '交易费用' }}</span><small>费用 · 执行</small>
          </button>
          <div v-if="showAdvanced" class="advanced-settings" data-testid="condition-advanced-settings">
            <div class="compact-fields">
              <div><label for="condition-cash">每股初始资金</label><input id="condition-cash" v-model.number="form.initialCash" data-testid="condition-cash" type="number" min="1" step="10000"></div>
              <div><label for="condition-execution">成交方式</label><select id="condition-execution" v-model="form.execution" data-testid="condition-execution"><option value="next_open">下一根开盘</option><option value="next_close">下一根收盘</option></select></div>
            </div>
            <div class="compact-fields">
              <div><label for="condition-commission">佣金费率</label><input id="condition-commission" v-model.number="form.commission" data-testid="condition-commission" type="number" min="0" max="0.1" step="0.0001"></div>
              <div><label for="condition-min-commission">最低佣金</label><input id="condition-min-commission" v-model.number="form.minCommission" data-testid="condition-min-commission" type="number" min="0" step="0.01"></div>
            </div>
            <div class="compact-fields">
              <div><label for="condition-stamp-tax">印花税率</label><input id="condition-stamp-tax" v-model.number="form.stampTax" data-testid="condition-stamp-tax" type="number" min="0" max="0.1" step="0.0001"></div>
              <div><label for="condition-slippage">每股滑点</label><input id="condition-slippage" v-model.number="form.slippage" data-testid="condition-slippage" type="number" min="0" step="0.001"></div>
            </div>
          </div>

          <div class="action-stack">
            <button class="primary-button" data-testid="start-condition-research" type="submit" :disabled="loading || !metadata">
              <span v-if="loading" class="spinner" aria-hidden="true"></span>
              {{ loading ? '正在研究…' : '开始买卖条件研究' }}
            </button>
            <button v-if="loading" class="secondary-button" data-testid="cancel-condition-research" type="button" @click="cancel">取消任务</button>
          </div>
          <p class="privacy-note">所有股票独立回测，不生成组合净值；历史发现不代表未来收益。</p>
        </form>

        <section class="results-panel panel" data-testid="condition-research-results" aria-live="polite">
          <div class="panel-heading results-heading">
            <div><span class="section-kicker">02 / RESULTS</span><h2>跨股票规则结果</h2></div>
          </div>
          <div v-if="!report" class="empty-state" data-testid="condition-research-empty">
            <div class="empty-icon" aria-hidden="true">/</div>
            <h3>{{ loading ? '正在逐只股票回测并挖掘条件…' : '还没有买卖条件研究结果' }}</h3>
            <p>{{ loading ? '候选条件会先在训练样本中发现，再经过验证期和最终留出期复验。' : '设置基础买卖规则后，系统会逐只股票回测并寻找盈利、亏损交易的共同特征。' }}</p>
          </div>
          <template v-else>
            <div class="backtest-summary">
              <span class="result-badge">{{ report.processed }} / {{ report.total_candidates }} 只股票</span>
              <span>{{ report.start_date }} → {{ report.end_date }}</span>
              <span>训练 {{ report.splits.train.end_date }} 前</span>
              <span>候选 {{ report.candidates.length }} 条</span>
              <span v-if="report.data_fingerprint" title="本次研究使用的行情指纹">数据 {{ report.data_fingerprint.slice(0, 12) }}</span>
            </div>
            <div class="metrics-strip condition-metrics">
              <div><span>基础全区间胜率</span><strong data-testid="condition-baseline-win-rate">{{ formatPercent(report.baseline.full_period?.pooled_trades.win_rate) }}</strong></div>
              <div><span>基础验证胜率</span><strong>{{ formatPercent(report.baseline.validation?.pooled_trades.win_rate) }}</strong></div>
              <div><span>全区间交易数</span><strong>{{ report.baseline.full_period?.pooled_trades.closed_trades ?? '—' }}</strong></div>
              <div><span>基础全区间盈亏比</span><strong>{{ formatNumber(report.baseline.full_period?.pooled_trades.payoff_ratio) }}</strong></div>
              <div><span>跳过 / 错误</span><strong>{{ report.skipped }} / {{ report.errors }}</strong></div>
            </div>
            <div v-if="loading && job" class="progress-track condition-progress" aria-label="条件研究进度">
              <span :style="{ width: Math.round(job.progress * 100) + '%' }"></span>
            </div>
            <div v-if="!report.candidates.length" class="inline-empty">
              {{ report.diagnostic ?? '没有候选条件同时满足样本数和股票覆盖要求。' }}
            </div>
            <div v-else class="table-wrap">
              <table>
                <caption class="sr-only">买卖条件候选验证结果</caption>
                <thead><tr><th>方向</th><th>新增条件</th><th>样本 / 股票</th><th>训练 lift</th><th>验证胜率</th><th>全区间胜率</th><th>股票胜率提升率</th><th>盈亏比</th><th>留出胜率</th><th>状态</th></tr></thead>
                <tbody>
                  <tr
                    v-for="candidate in report.candidates"
                    :key="candidate.candidate_id"
                    :data-testid="'condition-research-candidate-' + candidate.candidate_id"
                    :class="{ 'condition-candidate-selected': selectedStockView === candidate.candidate_id }"
                  >
                    <td>{{ candidate.side === 'buy' ? '买入过滤' : '卖出触发' }}</td>
                    <td class="condition-rule">{{ conditionText(candidate) }}</td>
                    <td>{{ candidate.mining.support_count }} / {{ candidate.mining.instrument_count }}</td>
                    <td>{{ formatNumber(candidate.mining.lift) }}</td>
                    <td>{{ formatPercent(candidate.validation.pooled_trades.win_rate) }}</td>
                    <td>{{ formatPercent(candidate.full_period?.pooled_trades.win_rate) }}</td>
                    <td>{{ formatPercent(candidate.validation.per_stock.improved_stock_ratio) }}</td>
                    <td>{{ formatNumber(candidate.validation.pooled_trades.payoff_ratio) }}</td>
                    <td>{{ formatPercent(candidate.holdout?.pooled_trades.win_rate) }}</td>
                    <td>
                      <span :class="candidate.passed_constraints ? 'positive' : 'muted-code'">{{ candidate.passed_constraints ? '通过验证' : candidate.constraint_failures[0] ?? '未入围' }}</span>
                      <button
                        v-if="candidate.full_period"
                        class="text-button"
                        :data-testid="'show-condition-stock-' + candidate.candidate_id"
                        type="button"
                        @click="selectedStockView = candidate.candidate_id + ':full'"
                      >个股</button>
                      <span v-else class="muted-code">未进留出期</span>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
            <section class="condition-stock-details">
              <div class="section-heading">
                <div><span class="section-kicker">PER-STOCK DETAIL</span><h3>{{ stockPeriodLabel }}个股表现</h3></div>
                <div class="condition-stock-controls">
                  <div class="condition-stock-view">
                    <label for="condition-stock-view">规则与区间</label>
                    <select id="condition-stock-view" v-model="selectedStockView" data-testid="condition-stock-view">
                      <option v-for="option in stockViewOptions" :key="option.value" :value="option.value">{{ option.label }}</option>
                    </select>
                  </div>
                  <div class="condition-stock-view">
                    <label for="condition-stock-filter">筛选股票</label>
                    <input id="condition-stock-filter" v-model="stockFilter" data-testid="condition-stock-filter" type="search" placeholder="代码或名称">
                  </div>
                  <div class="condition-stock-view">
                    <label for="condition-stock-sort">排序方式</label>
                    <select id="condition-stock-sort" v-model="stockSort" data-testid="condition-stock-sort">
                      <option value="win_rate">胜率从高到低</option>
                      <option value="payoff_ratio">盈亏比从高到低</option>
                      <option value="total_return">收益从高到低</option>
                      <option value="sharpe">Sharpe 从高到低</option>
                      <option value="code">市场和代码</option>
                    </select>
                  </div>
                </div>
              </div>
              <div v-if="!selectedStockRows.length" class="inline-empty">
                {{ selectedStockView.startsWith('baseline') ? '基础规则没有可展示的逐股票结果。' : '此候选未进入对应区间复验列表。' }}
              </div>
              <template v-else>
                <div class="condition-stock-pagination">
                  <span>筛选出 {{ filteredStockRows.length }} / {{ selectedStockRows.length }} 只股票 · 第 {{ stockPage }} / {{ stockPageCount }} 页</span>
                  <div>
                    <button class="secondary-button" type="button" :disabled="stockPage <= 1" @click="stockPage -= 1">上一页</button>
                    <button class="secondary-button" type="button" :disabled="stockPage >= stockPageCount" @click="stockPage += 1">下一页</button>
                  </div>
                </div>
                <div class="table-wrap">
                  <table>
                    <thead><tr><th>市场</th><th>代码</th><th>名称</th><th>交易数</th><th>胜率</th><th>较基础规则</th><th>盈亏比</th><th>收益</th><th>Sharpe</th><th>回撤</th></tr></thead>
                    <tbody>
                      <tr v-for="stock in selectedPageRows" :key="stock.market + stock.code">
                        <td>{{ stock.market }}</td><td>{{ stock.code }}</td><td>{{ stock.name || '—' }}</td>
                        <td>{{ stock.closed_trades }}</td><td>{{ formatPercent(stock.win_rate) }}</td>
                        <td>{{ winRateDelta(stock) }}</td>
                        <td>{{ formatNumber(stock.payoff_ratio) }}</td><td>{{ formatPercent(stock.total_return) }}</td>
                        <td>{{ formatNumber(stock.sharpe) }}</td><td>{{ formatPercent(stock.max_drawdown) }}</td>
                      </tr>
                    </tbody>
                  </table>
                </div>
              </template>
            </section>
          </template>
        </section>
      </div>

      <div v-if="loading && job" class="task-progress task-progress-panel" data-testid="condition-research-progress">
        <div class="task-progress-heading"><span>{{ job.phase ?? '买卖条件研究进度' }}</span><strong>{{ Math.round(job.progress * 100) }}%</strong></div>
        <div class="progress-track"><span :style="{ width: Math.round(job.progress * 100) + '%' }"></span></div>
        <small>本阶段已处理 {{ phaseCount(job, 'phase_current') }} / {{ phaseCount(job, 'phase_total') }} 只股票</small>
      </div>

      <section class="panel condition-history" data-testid="condition-research-history">
        <div class="panel-heading"><div><span class="section-kicker">SAVED RESEARCH</span><h2>历史研究</h2></div></div>
        <div class="compact-fields">
          <div>
            <label for="condition-history-select">已完成研究</label>
            <select id="condition-history-select" v-model="selectedHistoryId" data-testid="condition-history-select">
              <option value="">选择历史记录</option>
              <option v-for="item in history" :key="item.history_id" :value="item.history_id">
                {{ item.created_at }} · {{ item.history_id.slice(0, 8) }}
              </option>
            </select>
          </div>
          <button class="secondary-button" data-testid="load-condition-history" type="button" :disabled="historyLoading || !selectedHistoryId" @click="loadHistoryDetail">加载历史结果</button>
        </div>
      </section>
    </main>
  </div>
</template>

<style scoped>
.condition-research-grid {
  grid-template-columns: minmax(320px, 0.92fr) minmax(0, 1.4fr);
}

.condition-research-config,
.condition-stock-details {
  display: grid;
  gap: 1rem;
  min-width: 0;
}

.condition-fields-top {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0.8rem;
}

.condition-candidate-selected {
  background: color-mix(in srgb, var(--accent) 8%, transparent);
}

.condition-rule {
  min-width: 180px;
  font-family: var(--font-mono, monospace);
  font-size: 0.85rem;
}

.condition-stock-details {
  margin-top: 1.4rem;
}

.condition-stock-pagination {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
  color: var(--text-muted);
  font-size: 0.82rem;
}

.condition-stock-pagination > div {
  display: flex;
  gap: 0.5rem;
}

.condition-stock-controls {
  display: grid;
  gap: 0.65rem;
  grid-template-columns: repeat(3, minmax(150px, 1fr));
}

.condition-stock-view {
  display: grid;
  gap: 0.35rem;
  min-width: 0;
}

.condition-history {
  margin-top: 1.25rem;
}

@media (max-width: 1050px) {
  .condition-research-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 640px) {
  .condition-fields-top,
  .condition-stock-controls {
    grid-template-columns: 1fr;
  }
}
</style>
