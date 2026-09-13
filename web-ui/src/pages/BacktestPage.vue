<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'

import {
  createBacktest,
  deleteBacktestHistory,
  fetchBacktestHistory,
  getBacktest,
  getBacktestHistory,
  getBacktestResults,
} from '../api/backtest'
import { FormulaScreenApiError, fetchMetadata, parseFormula } from '../api/formulaScreen'
import { fetchActiveJobs } from '../api/jobs'
import BacktestHistoryPanel from '../components/BacktestHistoryPanel.vue'
import DateRangeSelector from '../components/DateRangeSelector.vue'
import RuleGroupEditor from '../components/RuleGroupEditor.vue'
import type {
  BacktestHistorySummary,
  BacktestJobState,
  BacktestPayload,
  BacktestResult,
  CustomFormulaMetadata,
  DateRangeMode,
  DateRangeSelection,
  FormulaScreenMetadata,
  RuleGroupPayload,
  SignalDefinition,
  ValueDefinition,
} from '../types'
import { numericRuleForSignal, signalDisplayName } from '../utils/formulaScreen'

interface BacktestFormState {
  mode: 'preset' | 'custom'
  market: 'SH' | 'SZ'
  code: string
  buySignal: string
  sellSignal: string
  buyRule: RuleGroupPayload
  sellRule: RuleGroupPayload
  formulaText: string
  formulaParameters: Record<string, number>
  dateMode: DateRangeMode
  startDate: string
  endDate: string
  initialCash: number
  commission: number
  minCommission: number
  stampTax: number
  slippage: number
  execution: 'next_open' | 'next_close'
  positionMode: 'full' | 'fixed'
  fixedSize: number | null
}

const metadata = ref<FormulaScreenMetadata | null>(null)
const customMetadata = ref<CustomFormulaMetadata | null>(null)
const parsedFormulaText = ref('')
const form = reactive<BacktestFormState>({
  mode: 'preset',
  market: 'SH',
  code: '',
  buySignal: '',
  sellSignal: '',
  buyRule: { signals: [], conditions: [], combine_mode: 'all', minimum_matches: null },
  sellRule: { signals: [], conditions: [], combine_mode: 'all', minimum_matches: null },
  formulaText: '',
  formulaParameters: {},
  dateMode: 'recent5',
  startDate: '',
  endDate: '',
  initialCash: 100_000,
  commission: 0.0001,
  minCommission: 0.1,
  stampTax: 0.001,
  slippage: 0,
  execution: 'next_open',
  positionMode: 'full',
  fixedSize: 100,
})
const errors = ref<Record<string, string>>({})
const message = ref('')
const loading = ref(false)
const customParseLoading = ref(false)
const customParseError = ref('')
const showAdvanced = ref(false)
const job = ref<BacktestJobState | null>(null)
const result = ref<BacktestResult | null>(null)
const history = ref<BacktestHistorySummary[]>([])
const historyLoading = ref(false)

const presetSignals = computed<SignalDefinition[]>(() => (
  metadata.value?.indicators.flatMap((indicator) => indicator.signals) ?? []
))
const availableSignals = computed<SignalDefinition[]>(() => (
  form.mode === 'custom'
    ? customMetadata.value?.signals ?? []
    : presetSignals.value
))
const availableValues = computed<ValueDefinition[]>(() => (
  form.mode === 'custom'
    ? customMetadata.value?.values ?? []
    : metadata.value?.indicators.flatMap((indicator) => indicator.values ?? []) ?? []
))
const canSubmit = computed(() => (
  !loading.value
  && !customParseLoading.value
  && metadata.value !== null
  && (form.mode !== 'custom' || customMetadata.value !== null)
))
const dateRange = computed<DateRangeSelection>({
  get: () => ({ mode: form.dateMode, startDate: form.startDate, endDate: form.endDate }),
  set: (value) => {
    form.dateMode = value.mode
    form.startDate = value.startDate
    form.endDate = value.endDate
  },
})
const sampledEquity = computed(() => {
  const curve = result.value?.equity_curve ?? []
  return curve.length > 60 ? curve.slice(-60) : curve
})
const chartPoints = computed(() => {
  const curve = result.value?.equity_curve ?? []
  if (curve.length < 2) return ''
  const sample = curve.length > 120
    ? curve.filter((_, index) => index % Math.ceil(curve.length / 120) === 0)
    : curve
  const values = sample.map((point) => point.total ?? 0)
  const minimum = Math.min(...values)
  const maximum = Math.max(...values)
  const span = maximum - minimum || 1
  return sample.map((point, index) => {
    const x = (index / Math.max(sample.length - 1, 1)) * 800
    const y = 220 - (((point.total ?? 0) - minimum) / span) * 190
    return `${x.toFixed(1)},${y.toFixed(1)}`
  }).join(' ')
})
const chartSummary = computed(() => {
  const curve = result.value?.equity_curve ?? []
  if (!curve.length) return '暂无净值数据'
  const first = curve[0].total ?? 0
  const last = curve[curve.length - 1].total ?? 0
  return `净值从 ${formatMoney(first)} 变为 ${formatMoney(last)}，共 ${curve.length} 个交易日`
})

function ruleForSignal(signal: string): RuleGroupPayload {
  return numericRuleForSignal(signal, availableValues.value)
}

function hasRuleItems(rule: RuleGroupPayload): boolean {
  return rule.signals.length + rule.conditions.length > 0
}

function isSimpleRule(rule: RuleGroupPayload, signal: string): boolean {
  return rule.combine_mode === 'all'
    && rule.minimum_matches === null
    && rule.conditions.length === 0
    && rule.signals.length === 1
    && rule.signals[0] === signal
}

function setBuyRule(rule: RuleGroupPayload): void {
  form.buyRule = rule
  form.buySignal = isSimpleRule(rule, rule.signals[0] ?? '') ? rule.signals[0] ?? '' : ''
}

function setSellRule(rule: RuleGroupPayload): void {
  form.sellRule = rule
  form.sellSignal = isSimpleRule(rule, rule.signals[0] ?? '') ? rule.signals[0] ?? '' : ''
}

function setLegacySignals(buySignal: string, sellSignal: string): void {
  form.buySignal = buySignal
  form.sellSignal = sellSignal
  form.buyRule = ruleForSignal(buySignal)
  form.sellRule = ruleForSignal(sellSignal)
}

function setMode(mode: BacktestFormState['mode']): void {
  if (form.mode === mode) return
  form.mode = mode
  customParseError.value = ''
  errors.value = {}
  message.value = ''
  if (mode === 'preset') {
    customMetadata.value = null
    parsedFormulaText.value = ''
    setLegacySignals(
      presetSignals.value.find((signal) => signal.id.endsWith('prepare_rally'))?.id ?? presetSignals.value[0]?.id ?? '',
      presetSignals.value.find((signal) => signal.id.endsWith('end_zone'))?.id ?? presetSignals.value[1]?.id ?? '',
    )
  } else {
    setLegacySignals('', '')
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
    const parsed = await parseFormula(form.formulaText)
    customMetadata.value = parsed
    parsedFormulaText.value = form.formulaText.trim()
    const previousParameters = form.formulaParameters
    form.formulaParameters = Object.fromEntries(
      parsed.parameters.map((parameter) => [
        parameter.name,
        previousParameters[parameter.name] ?? parameter.default,
      ]),
    )
    setLegacySignals(parsed.signals[0]?.id ?? '', parsed.signals[1]?.id ?? '')
    errors.value = {}
    message.value = `公式解析完成：识别 ${parsed.parameters.length} 个参数、${parsed.signals.length} 个输出。`
  } catch (error) {
    customMetadata.value = null
    parsedFormulaText.value = ''
    setLegacySignals('', '')
    customParseError.value = error instanceof FormulaScreenApiError
      ? error.message
      : '公式解析失败，请检查语法和函数是否受支持。'
  } finally {
    customParseLoading.value = false
  }
}

function validate(): Record<string, string> {
  const next: Record<string, string> = {}
  const code = form.code.trim()
  if (!/^\d{6}$/.test(code)) next.code = '请输入六位股票代码。'
  if (form.mode === 'custom') {
    if (!form.formulaText.trim()) next.formulaText = '请输入通达信公式。'
    else if (!customMetadata.value) next.formulaText = '请先解析公式，再选择买卖输出。'
    if (customMetadata.value) {
      for (const parameter of customMetadata.value.parameters) {
        const value = form.formulaParameters[parameter.name]
        if (!Number.isFinite(value) || value < parameter.minimum || value > parameter.maximum) {
          next.formulaParameters = `参数 ${parameter.name} 必须在 ${parameter.minimum} 到 ${parameter.maximum} 之间。`
          break
        }
        if (parameter.step === 1 && !Number.isInteger(value)) {
          next.formulaParameters = `参数 ${parameter.name} 必须是整数。`
          break
        }
      }
    }
  }
  if (!hasRuleItems(form.buyRule)) next.buySignal = '请至少设置一个买入指标条件。'
  if (!hasRuleItems(form.sellRule)) next.sellSignal = '请至少设置一个卖出指标条件。'
  else if (isSimpleRule(form.buyRule, form.buySignal) && isSimpleRule(form.sellRule, form.sellSignal) && form.buySignal === form.sellSignal) next.sellSignal = '买入信号和卖出信号不能相同。'
  if (form.dateMode === 'custom') {
    if (!form.startDate) next.startDate = '请选择开始日期。'
    if (!form.endDate) next.endDate = '请选择结束日期。'
  }
  if (form.startDate && form.endDate && form.startDate > form.endDate) next.endDate = '结束日期不能早于开始日期。'
  if (!Number.isFinite(form.initialCash) || form.initialCash <= 0) next.initialCash = '初始资金必须大于 0。'
  if (form.positionMode === 'fixed' && (!Number.isInteger(form.fixedSize) || !form.fixedSize || form.fixedSize < 100 || form.fixedSize % 100 !== 0)) {
    next.fixedSize = '固定股数必须是 100 的整数倍。'
  }
  return next
}

function buildPayload(): BacktestPayload {
  const payload: BacktestPayload = {
    market: form.market,
    code: form.code.trim(),
    buy_signal: form.buySignal,
    sell_signal: form.sellSignal,
    date_mode: form.dateMode,
    formula_text: form.mode === 'custom' ? form.formulaText.trim() : null,
    formula_parameters: form.mode === 'custom' ? { ...form.formulaParameters } : {},
    start_date: form.startDate || null,
    end_date: form.endDate || null,
    initial_cash: form.initialCash,
    commission: form.commission,
    min_commission: form.minCommission,
    stamp_tax: form.stampTax,
    slippage: form.slippage,
    execution: form.execution,
    position_mode: form.positionMode,
    fixed_size: form.positionMode === 'fixed' ? form.fixedSize : null,
  }
  if (!isSimpleRule(form.buyRule, form.buySignal)) {
    payload.buy_signal = null
    payload.buy_rule = form.buyRule
  }
  if (!isSimpleRule(form.sellRule, form.sellSignal)) {
    payload.sell_signal = null
    payload.sell_rule = form.sellRule
  }
  return payload
}

function apiMessage(error: unknown): string {
  if (error instanceof FormulaScreenApiError) return error.message
  return '回测失败，请检查本地 DuckDB 数据和配置后重试。'
}

async function submit(): Promise<void> {
  errors.value = validate()
  message.value = ''
  if (Object.values(errors.value).some(Boolean)) return
  loading.value = true
  result.value = null
  try {
    const created = await createBacktest(buildPayload())
    job.value = {
      job_id: created.job_id,
      status: 'queued',
      progress: 0,
      total_candidates: 1,
      total_scanned: 0,
      errors: 0,
      error: null,
      result: null,
    }
    await pollBacktest(created.job_id)
  } catch (error) {
    message.value = apiMessage(error)
  } finally {
    loading.value = false
  }
}

async function pollBacktest(jobId: string): Promise<void> {
  for (;;) {
    const state = await getBacktest(jobId)
    job.value = state
    if (state.status === 'completed') {
      result.value = await getBacktestResults(jobId)
      message.value = `回测完成：${result.value.trades.length} 笔成交，区间 ${result.value.start_date} 至 ${result.value.end_date}。`
      await loadHistory()
      return
    }
    if (state.status === 'failed') throw new Error(state.error ?? '回测任务失败')
    await new Promise((resolve) => window.setTimeout(resolve, 220))
  }
}

async function resumeActiveBacktest(): Promise<void> {
  try {
    const active = (await fetchActiveJobs()).find((item) => item.description === '历史回测')
    if (!active) return
    job.value = active as unknown as BacktestJobState
    loading.value = true
    await pollBacktest(active.job_id)
  } catch (error) {
    message.value = apiMessage(error)
  } finally {
    loading.value = false
  }
}

async function loadHistory(): Promise<void> {
  historyLoading.value = true
  try {
    history.value = await fetchBacktestHistory()
  } catch (error) {
    message.value = apiMessage(error)
  } finally {
    historyLoading.value = false
  }
}

async function loadHistoryDetail(historyId: string): Promise<void> {
  try {
    const loaded = await getBacktestHistory(historyId)
    result.value = loaded.result
    message.value = `已加载历史回测 ${historyId}。`
  } catch (error) {
    message.value = apiMessage(error)
  }
}

async function removeHistory(historyId: string): Promise<void> {
  try {
    await deleteBacktestHistory(historyId)
    await loadHistory()
    message.value = '历史回测已删除。'
  } catch (error) {
    message.value = apiMessage(error)
  }
}

function formatPercent(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : `${(value * 100).toFixed(2)}%`
}

function formatNumber(value: number | null | undefined, digits = 2): string {
  return value === null || value === undefined ? '—' : value.toFixed(digits)
}

function formatMoney(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : `¥${value.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

function downloadResult(): void {
  if (!result.value) return
  const url = URL.createObjectURL(new Blob([JSON.stringify(result.value, null, 2)], { type: 'application/json' }))
  const link = document.createElement('a')
  link.href = url
  link.download = `backtest-${result.value.market}-${result.value.code}.json`
  link.click()
  window.setTimeout(() => URL.revokeObjectURL(url), 0)
}

onMounted(async () => {
  try {
    metadata.value = await fetchMetadata()
    const signals = presetSignals.value
    setLegacySignals(
      signals.find((signal) => signal.id.endsWith('prepare_rally'))?.id ?? signals[0]?.id ?? '',
      signals.find((signal) => signal.id.endsWith('end_zone'))?.id ?? signals[1]?.id ?? '',
    )
    await loadHistory()
    await resumeActiveBacktest()
  } catch (error) {
    message.value = apiMessage(error)
  }
})

watch(() => form.formulaText, (value) => {
  if (form.mode !== 'custom' || customMetadata.value === null) return
  if (value.trim() === parsedFormulaText.value) return
  customMetadata.value = null
  parsedFormulaText.value = ''
  setLegacySignals('', '')
  customParseError.value = '公式已修改，请重新解析。'
})
</script>

<template>
  <div class="screen-page backtest-page" data-testid="backtest-page">
    <header class="topbar">
      <a class="brand" href="/indicator-lab" aria-label="指标实验室首页">
        <img class="brand-mark" src="/indicator-lab-mark.png" alt="" aria-hidden="true" width="34" height="34">
        <span><strong>Indicator Lab</strong><small>指标实验室</small></span>
      </a>
      <nav aria-label="主导航">
        <a class="nav-link" href="/indicator-lab">公式选股</a>
        <a class="nav-link active" href="/backtest" aria-current="page">单股回测</a>
        <a class="nav-link" href="/portfolio-backtest">组合回测</a>
        <a class="nav-link" href="/strategy-lab">策略实验室</a>
        <a class="nav-link" href="/strategy-fitness">策略适配性</a>
        <a class="nav-link" href="/market-data">本地行情</a>
      </nav>
      <div class="topbar-status"><span class="status-dot" aria-hidden="true"></span> 本地数据模式</div>
    </header>

    <main class="content-shell">
      <section class="page-intro">
        <div>
          <p class="eyebrow">FORMULA BACKTEST / DAILY</p>
          <h1>公式回测</h1>
          <p class="intro-copy">把同一套通达信条件放进历史行情，观察信号、成交与资金曲线。</p>
        </div>
        <div class="intro-note"><span class="note-label">执行假设</span><strong>下一根 K 线成交</strong><small>默认 next open · 支持佣金、印花税与滑点</small></div>
      </section>

      <div v-if="message" class="notice" :class="{ error: !message.includes('完成') }" role="alert" data-testid="backtest-message">{{ message }}</div>

      <div class="workspace-grid backtest-grid">
        <form class="config-panel panel" data-testid="backtest-config" @submit.prevent="submit">
          <div class="panel-heading">
            <div><span class="section-kicker">01 / CONFIGURE</span><h2>回测配置</h2></div>
            <span class="required-hint">* 必填</span>
          </div>

          <div class="formula-mode-tabs" role="tablist" aria-label="公式来源">
            <button type="button" :class="{ active: form.mode === 'preset' }" data-testid="backtest-mode-preset" role="tab" :aria-selected="form.mode === 'preset'" @click="setMode('preset')">预置指标</button>
            <button type="button" :class="{ active: form.mode === 'custom' }" data-testid="backtest-mode-custom" role="tab" :aria-selected="form.mode === 'custom'" @click="setMode('custom')">自定义公式</button>
          </div>

          <fieldset v-if="form.mode === 'custom'" class="form-section custom-formula-section">
            <legend>粘贴通达信公式</legend>
            <textarea v-model="form.formulaText" data-testid="backtest-formula" rows="7" spellcheck="false" placeholder="例如：买入:CROSS(C,MA(C,20)); 卖出:CROSS(MA(C,20),C);"></textarea>
            <button class="secondary-button" data-testid="backtest-parse-formula" type="button" :disabled="customParseLoading" @click="parseCustomFormula">{{ customParseLoading ? '解析中…' : '解析公式' }}</button>
            <p class="helper">公式需要至少两个可用于买入/卖出的命名输出；参数使用 <code>名称:=数值</code>。</p>
            <p v-if="customParseError" class="field-error">{{ customParseError }}</p>
            <div v-if="customMetadata" class="custom-formula-meta"><span>{{ customMetadata.parameters.length }} 个参数</span><span>{{ customMetadata.signals.length }} 个输出</span><span>至少 {{ customMetadata.minimum_bars }} 根 K 线</span></div>
            <div v-if="customMetadata?.parameters.length" class="parameter-grid">
              <div v-for="parameter in customMetadata.parameters" :key="parameter.name"><label :for="`backtest-param-${parameter.name}`">{{ parameter.name }}</label><input :id="`backtest-param-${parameter.name}`" v-model.number="form.formulaParameters[parameter.name]" type="number" :min="parameter.minimum" :max="parameter.maximum" :step="parameter.step"></div>
            </div>
            <p v-if="errors.formulaParameters" class="field-error">{{ errors.formulaParameters }}</p>
          </fieldset>

          <fieldset class="form-section">
            <legend>标的与数据</legend>
            <div class="compact-fields">
              <div><label for="backtest-market">市场 <span class="required">*</span></label><select id="backtest-market" v-model="form.market" data-testid="backtest-market"><option value="SH">上海 SH</option><option value="SZ">深圳 SZ</option></select></div>
              <div><label for="backtest-code">股票代码 <span class="required">*</span></label><input id="backtest-code" v-model="form.code" data-testid="backtest-code" type="text" inputmode="numeric" maxlength="6" placeholder="600000" autocomplete="off"></div>
            </div>
            <p v-if="errors.code" class="field-error">{{ errors.code }}</p>
            <p class="helper">读取已导入本地 DuckDB 的已完成日线；请先在“本地行情”页面导入数据。</p>
          </fieldset>

          <p class="helper rule-editor-intro">买入和卖出都使用指标条件构建器：可以比较两个指标，也可以比较指标和固定值；支持 AND、OR 或至少满足 N 个。</p>
          <RuleGroupEditor
            :model-value="form.buyRule"
            :values="availableValues"
            :condition-operators="metadata?.condition_operators"
            title="买入条件组合"
            testid="backtest-buy-rule"
            @update:model-value="setBuyRule"
          />
          <p v-if="errors.buySignal" class="field-error">{{ errors.buySignal }}</p>
          <RuleGroupEditor
            :model-value="form.sellRule"
            :values="availableValues"
            :condition-operators="metadata?.condition_operators"
            title="卖出条件组合"
            testid="backtest-sell-rule"
            @update:model-value="setSellRule"
          />
          <p v-if="errors.sellSignal" class="field-error">{{ errors.sellSignal }}</p>

          <DateRangeSelector v-model="dateRange" prefix="backtest" testid="backtest-date-range" />
          <p v-if="errors.startDate" class="field-error">{{ errors.startDate }}</p>
          <p v-if="errors.endDate" class="field-error">{{ errors.endDate }}</p>

          <button class="advanced-toggle" data-testid="backtest-advanced-toggle" type="button" :aria-expanded="showAdvanced" @click="showAdvanced = !showAdvanced"><span>{{ showAdvanced ? '收起交易设置' : '交易设置' }}</span><small>资金 · 执行 · 成本</small></button>
          <div v-if="showAdvanced" class="advanced-settings" data-testid="backtest-advanced-settings">
            <fieldset class="form-section compact-fields"><div><label for="initial-cash">初始资金</label><input id="initial-cash" v-model.number="form.initialCash" data-testid="initial-cash" type="number" min="1" step="1000"></div><div><label for="execution">成交方式</label><select id="execution" v-model="form.execution" data-testid="execution"><option value="next_open">下一根开盘</option><option value="next_close">下一根收盘</option></select></div></fieldset>
            <p v-if="errors.initialCash" class="field-error">{{ errors.initialCash }}</p>
            <fieldset class="form-section compact-fields"><div><label for="position-mode">仓位方式</label><select id="position-mode" v-model="form.positionMode" data-testid="position-mode"><option value="full">全仓</option><option value="fixed">固定股数</option></select></div><div v-if="form.positionMode === 'fixed'"><label for="fixed-size">固定股数</label><input id="fixed-size" v-model.number="form.fixedSize" data-testid="fixed-size" type="number" min="100" step="100"></div></fieldset>
            <p v-if="errors.fixedSize" class="field-error">{{ errors.fixedSize }}</p>
            <fieldset class="form-section compact-fields"><div><label for="commission">佣金费率</label><input id="commission" v-model.number="form.commission" data-testid="commission" type="number" min="0" max="0.1" step="0.0001"></div><div><label for="min-commission">最低佣金</label><input id="min-commission" v-model.number="form.minCommission" data-testid="min-commission" type="number" min="0" step="0.01"></div></fieldset>
            <fieldset class="form-section compact-fields"><div><label for="stamp-tax">印花税率</label><input id="stamp-tax" v-model.number="form.stampTax" data-testid="stamp-tax" type="number" min="0" max="0.1" step="0.0001"></div><div><label for="slippage">每股滑点（元）</label><input id="slippage" v-model.number="form.slippage" data-testid="slippage" type="number" min="0" step="0.001"></div></fieldset>
          </div>

          <button class="primary-button" data-testid="start-backtest" type="submit" :disabled="!canSubmit"><span v-if="loading" class="spinner" aria-hidden="true"></span>{{ loading ? '回测中…' : '开始回测' }}</button>
          <p class="privacy-note">回测只读取本机 DuckDB，不会上传行情数据；成功结果会自动保存到本机历史记录。</p>
        </form>

        <section class="results-panel panel" data-testid="backtest-results" aria-live="polite">
          <div class="panel-heading results-heading"><div><span class="section-kicker">02 / RESULTS</span><h2>回测结果</h2></div><button v-if="result" class="secondary-button export-backtest" type="button" data-testid="export-backtest" @click="downloadResult">导出 JSON</button></div>
          <div v-if="!result" class="empty-state" data-testid="backtest-empty"><div class="empty-icon" aria-hidden="true">/</div><h3>{{ loading ? '正在运行历史回测…' : '还没有回测结果' }}</h3><p>{{ loading ? '正在逐日模拟信号和成交，完成后显示净值曲线。' : '填写股票和买卖条件后开始一次回测。' }}</p></div>
          <template v-else>
            <div class="backtest-summary"><span class="result-badge"><span v-if="result.name">{{ result.name }} · </span>{{ result.market }} {{ result.code }}</span><span>{{ result.start_date }} → {{ result.end_date }}</span><span>{{ result.bars }} 根日线</span><span>买入：{{ signalDisplayName(result.buy_signal, metadata, customMetadata) }}</span><span>卖出：{{ signalDisplayName(result.sell_signal, metadata, customMetadata) }}</span></div>
            <div class="metrics-strip backtest-metrics">
              <div><span>总收益</span><strong :class="{ positive: (result.performance.total_return ?? 0) >= 0, negative: (result.performance.total_return ?? 0) < 0 }">{{ formatPercent(result.performance.total_return) }}</strong></div>
              <div><span>年化收益</span><strong>{{ formatPercent(result.performance.annual_return) }}</strong></div>
              <div><span>最大回撤</span><strong class="negative">{{ formatPercent(result.performance.max_drawdown) }}</strong></div>
              <div><span>夏普比率</span><strong>{{ formatNumber(result.performance.sharpe) }}</strong></div>
              <div><span>已完成交易</span><strong>{{ formatNumber(result.performance.total_trades, 0) }}</strong></div>
              <div><span>胜率</span><strong>{{ formatPercent(result.performance.win_rate) }}</strong></div>
            </div>

            <section class="chart-card" aria-labelledby="equity-title"><div class="chart-heading"><div><span class="section-kicker">EQUITY CURVE</span><h3 id="equity-title">资金曲线</h3></div><span class="chart-legend"><i aria-hidden="true"></i>总资产</span></div><svg class="equity-chart" data-testid="equity-chart" viewBox="0 0 800 240" role="img" :aria-label="chartSummary"><polyline :points="chartPoints" fill="none" stroke="var(--accent)" stroke-linecap="round" stroke-linejoin="round" stroke-width="3"></polyline></svg><p class="chart-summary">{{ chartSummary }}</p></section>

            <section class="data-section"><div class="section-heading"><div><span class="section-kicker">TRADES</span><h3>成交记录</h3></div><span>{{ result.trades.length }} 笔</span></div><div v-if="result.trades.length === 0" class="inline-empty">区间内没有形成可执行成交。</div><div v-else class="table-wrap"><table data-testid="backtest-trades"><caption class="sr-only">回测成交记录</caption><thead><tr><th>日期</th><th>标的</th><th>方向</th><th>数量</th><th>价格</th><th>费用</th><th>已实现盈亏</th></tr></thead><tbody><tr v-for="(trade, index) in result.trades" :key="`${trade.date}-${index}`"><td class="muted-code">{{ trade.date }}</td><td>{{ trade.name || result.name || `${result.market} ${result.code}` }}</td><td><span class="direction" :class="trade.direction.toLowerCase()">{{ trade.direction === 'BUY' ? '买入' : '卖出' }}</span></td><td>{{ formatNumber(trade.size, 0) }}</td><td>{{ formatMoney(trade.price) }}</td><td>{{ formatMoney((trade.commission ?? 0) + (trade.slippage ?? 0)) }}</td><td :class="{ positive: trade.pnl >= 0, negative: trade.pnl < 0 }">{{ formatMoney(trade.pnl) }}</td></tr></tbody></table></div></section>

            <section class="data-section"><div class="section-heading"><div><span class="section-kicker">RECENT EQUITY</span><h3>最近净值</h3></div><span>最近 {{ sampledEquity.length }} 日</span></div><div class="table-wrap"><table data-testid="equity-table"><caption class="sr-only">最近资金曲线数据</caption><thead><tr><th>日期</th><th>现金</th><th>持仓市值</th><th>总资产</th><th>回撤</th></tr></thead><tbody><tr v-for="point in sampledEquity" :key="point.date"><td class="muted-code">{{ point.date }}</td><td>{{ formatMoney(point.cash) }}</td><td>{{ formatMoney(point.position_value) }}</td><td><strong>{{ formatMoney(point.total) }}</strong></td><td>{{ formatPercent(point.drawdown_pct) }}</td></tr></tbody></table></div></section>
            <p v-if="result.diagnostic" class="diagnostic" data-testid="backtest-diagnostic">{{ result.diagnostic }}</p>
          </template>
        </section>
      </div>
      <div v-if="loading && job" class="task-progress task-progress-panel" data-testid="backtest-progress"><div class="task-progress-heading"><span>回测任务进度</span><strong>{{ Math.round(job.progress * 100) }}%</strong></div><div class="progress-track"><span :style="{ width: `${Math.round(job.progress * 100)}%` }"></span></div><small>已处理 {{ job.total_scanned }} / {{ job.total_candidates || 1 }}</small></div>

      <BacktestHistoryPanel
        :items="history"
        :loading="historyLoading"
        testid="backtest-history"
        title="历史单股回测"
        empty-text="暂无历史单股回测。"
        @refresh="loadHistory"
        @load="loadHistoryDetail"
        @remove="removeHistory"
      />
    </main>
  </div>
</template>
