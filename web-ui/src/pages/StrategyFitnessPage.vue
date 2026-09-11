<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'

import {
  createStrategyFitness,
  deleteStrategyFitnessHistory,
  fetchStrategyFitnessHistory,
  getStrategyFitness,
  getStrategyFitnessHistory,
  getStrategyFitnessResults,
} from '../api/strategyFitness'
import { FormulaScreenApiError, fetchMetadata, parseFormula } from '../api/formulaScreen'
import BacktestHistoryPanel from '../components/BacktestHistoryPanel.vue'
import RuleGroupEditor from '../components/RuleGroupEditor.vue'
import type {
  BacktestExecution,
  BacktestHistorySummary,
  CustomFormulaMetadata,
  FitnessPhaseMetrics,
  FitnessLabel,
  FormulaScreenMetadata,
  InstrumentBoard,
  InstrumentType,
  SignalDefinition,
  StrategyFitnessJobState,
  StrategyFitnessPayload,
  StrategyFitnessReport,
  RuleGroupPayload,
  Universe,
  ValueDefinition,
} from '../types'
import { numericRuleForSignal, valueOptionLabel } from '../utils/formulaScreen'

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

interface FitnessFormState {
  mode: 'preset' | 'custom'
  buyRule: RuleGroupPayload
  universe: Universe
  universeFile: string
  rankingValue: string
  rankOrder: 'asc' | 'desc'
  formulaText: string
  formulaParameters: Record<string, number>
  sellRule: RuleGroupPayload
  stopLossEnabled: boolean
  stopLossPct: number
  takeProfitEnabled: boolean
  takeProfitPct: number
  startDate: string
  endDate: string
  trainRatio: number
  validationRatio: number
  minTrades: number
  maxTestDrawdown: number
  initialCash: number
  commission: number
  minCommission: number
  stampTax: number
  slippage: number
  execution: BacktestExecution
}

const metadata = ref<FormulaScreenMetadata | null>(null)
const customMetadata = ref<CustomFormulaMetadata | null>(null)
const parsedFormulaText = ref('')
const form = reactive<FitnessFormState>({
  mode: 'preset',
  buyRule: { signals: [], conditions: [], combine_mode: 'any', minimum_matches: null },
  universe: 'all',
  universeFile: '',
  instrumentTypes: [] as InstrumentType[],
  boards: [] as InstrumentBoard[],
  rankingValue: '',
  rankOrder: 'desc',
  formulaText: '',
  formulaParameters: {},
  sellRule: { signals: [], conditions: [], combine_mode: 'any', minimum_matches: null },
  stopLossEnabled: true,
  stopLossPct: 8,
  takeProfitEnabled: false,
  takeProfitPct: 20,
  startDate: '',
  endDate: '',
  trainRatio: 60,
  validationRatio: 20,
  minTrades: 5,
  maxTestDrawdown: 30,
  initialCash: 1_000_000,
  commission: 0.0001,
  minCommission: 0.1,
  stampTax: 0.001,
  slippage: 0,
  execution: 'next_open',
})
const errors = ref<Record<string, string>>({})
const message = ref('')
const loading = ref(false)
const customParseLoading = ref(false)
const customParseError = ref('')
const showAdvanced = ref(false)
const job = ref<StrategyFitnessJobState | null>(null)
const report = ref<StrategyFitnessReport | null>(null)
const history = ref<BacktestHistorySummary[]>([])
const historyLoading = ref(false)

const presetSignals = computed<SignalDefinition[]>(() => (
  metadata.value?.indicators.flatMap((indicator) => indicator.signals) ?? []
))
const presetValues = computed<ValueDefinition[]>(() => (
  metadata.value?.indicators.flatMap((indicator) => indicator.values ?? []) ?? []
))
const availableSignals = computed<SignalDefinition[]>(() => (
  form.mode === 'custom' ? customMetadata.value?.signals ?? [] : presetSignals.value
))
const availableValues = computed<ValueDefinition[]>(() => (
  form.mode === 'custom' ? customMetadata.value?.values ?? [] : presetValues.value
))
const canSubmit = computed(() => (
  !loading.value
  && !customParseLoading.value
  && metadata.value !== null
  && (form.mode !== 'custom' || customMetadata.value !== null)
))
const passedCount = computed(() => report.value?.results.filter((item) => item.passed).length ?? 0)

function firstSignal(suffix: string): string {
  return availableSignals.value.find((signal) => signal.id.endsWith(suffix))?.id
    ?? availableSignals.value[0]?.id
    ?? ''
}

function ruleForSignal(signal: string): RuleGroupPayload {
  return numericRuleForSignal(signal, availableValues.value)
}

function setBuyRule(rule: RuleGroupPayload): void {
  form.buyRule = rule
}

function setSellRule(rule: RuleGroupPayload): void {
  form.sellRule = rule
}

function resetPresetDefaults(): void {
  const buySignal = firstSignal('prepare_rally')
  form.buyRule = ruleForSignal(buySignal)
  form.sellRule = ruleForSignal(firstSignal('end_zone'))
  form.rankingValue = availableValues.value[0]?.id ?? ''
}

function setMode(mode: FitnessFormState['mode']): void {
  if (form.mode === mode) return
  form.mode = mode
  errors.value = {}
  message.value = ''
  customParseError.value = ''
  if (mode === 'preset') {
    customMetadata.value = null
    parsedFormulaText.value = ''
    resetPresetDefaults()
  } else {
    form.buyRule = ruleForSignal('')
    form.sellRule = ruleForSignal('')
    form.rankingValue = ''
  }
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
      parsed.parameters.map((parameter) => [parameter.name, previousParameters[parameter.name] ?? parameter.default]),
    )
    form.buyRule = ruleForSignal(parsed.signals[0]?.id ?? '')
    form.sellRule = ruleForSignal(parsed.signals[1]?.id ?? '')
    form.rankingValue = parsed.values?.[0]?.id ?? ''
    errors.value = {}
    message.value = `公式解析完成：识别 ${parsed.parameters.length} 个参数、${parsed.signals.length} 个买卖输出、${parsed.values?.length ?? 0} 个指标输出。`
  } catch (error) {
    customMetadata.value = null
    parsedFormulaText.value = ''
    form.buyRule = ruleForSignal('')
    form.sellRule = ruleForSignal('')
    form.rankingValue = ''
    customParseError.value = error instanceof FormulaScreenApiError
      ? error.message
      : '公式解析失败，请检查语法和函数是否受支持。'
  } finally {
    customParseLoading.value = false
  }
}

function validate(): Record<string, string> {
  const next: Record<string, string> = {}
  const buyRule = form.buyRule
  const sellRule = form.sellRule
  if (!buyRule.signals.length && !buyRule.conditions.length) next.buyRule = '请至少设置一个买入条件。'
  if (!form.rankingValue) next.rankingValue = '请选择用于记录策略上下文的指标输出。'
  if (form.universe === 'custom' && !form.universeFile.trim()) next.universeFile = '请输入自定义股票列表文件。'
  if (form.mode === 'custom') {
    if (!form.formulaText.trim()) next.formulaText = '请输入通达信公式。'
    else if (!customMetadata.value) next.formulaText = '请先解析公式，再配置适配性评估。'
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
  if (form.startDate && form.endDate && form.startDate > form.endDate) next.endDate = '结束日期不能早于开始日期。'
  if (!Number.isFinite(form.trainRatio) || form.trainRatio <= 0) next.trainRatio = '训练比例必须大于 0。'
  if (!Number.isFinite(form.validationRatio) || form.validationRatio <= 0) next.validationRatio = '验证比例必须大于 0。'
  if (form.trainRatio + form.validationRatio >= 100) next.validationRatio = '训练和验证比例之和必须小于 100%。'
  if (!Number.isInteger(form.minTrades) || form.minTrades < 1) next.minTrades = '最少成交笔数必须是正整数。'
  if (!Number.isFinite(form.maxTestDrawdown) || form.maxTestDrawdown < 0 || form.maxTestDrawdown > 100) next.maxTestDrawdown = '最大回撤阈值必须在 0 到 100% 之间。'
  if (!sellRule.signals.length && !sellRule.conditions.length && !form.stopLossEnabled && !form.takeProfitEnabled) next.sellRules = '至少配置一个卖出规则。'
  if (form.stopLossEnabled && (!Number.isFinite(form.stopLossPct) || form.stopLossPct <= 0)) next.stopLossPct = '止损比例必须大于 0。'
  if (form.takeProfitEnabled && (!Number.isFinite(form.takeProfitPct) || form.takeProfitPct <= 0)) next.takeProfitPct = '止盈比例必须大于 0。'
  if (!Number.isFinite(form.initialCash) || form.initialCash <= 0) next.initialCash = '初始资金必须大于 0。'
  return next
}

function buildPayload(): StrategyFitnessPayload {
  const buyRule = form.buyRule
  const sellRule = form.sellRule
  return {
    universe: form.universe,
    universe_file: form.universe === 'custom' ? form.universeFile.trim() : null,
    ...(form.instrumentTypes.length > 0 ? { instrument_types: [...form.instrumentTypes] } : {}),
    ...(form.boards.length > 0 ? { boards: [...form.boards] } : {}),
    selected_signals: [],
    combine_mode: 'all',
    minimum_matches: null,
    buy_rule: buyRule,
    ranking_value: form.rankingValue,
    rank_order: form.rankOrder,
    formula_text: form.mode === 'custom' ? form.formulaText.trim() : null,
    formula_parameters: form.mode === 'custom' ? { ...form.formulaParameters } : {},
    sell_signal: null,
    stop_loss_pct: form.stopLossEnabled ? form.stopLossPct / 100 : null,
    take_profit_pct: form.takeProfitEnabled ? form.takeProfitPct / 100 : null,
    sell_value: null,
    sell_value_operator: null,
    sell_value_threshold: null,
    compare_left_value: null,
    compare_operator: null,
    compare_right_value: null,
    sell_rule: sellRule.signals.length || sellRule.conditions.length ? sellRule : null,
    start_date: form.startDate || null,
    end_date: form.endDate || null,
    train_ratio: form.trainRatio / 100,
    validation_ratio: form.validationRatio / 100,
    min_trades: form.minTrades,
    max_test_drawdown: form.maxTestDrawdown / 100,
    initial_cash: form.initialCash,
    commission: form.commission,
    min_commission: form.minCommission,
    stamp_tax: form.stampTax,
    slippage: form.slippage,
    execution: form.execution,
  }
}

function apiMessage(error: unknown): string {
  if (error instanceof FormulaScreenApiError) return error.message
  return '策略适配性评估失败，请检查本地 DuckDB 数据和配置后重试。'
}

async function submit(): Promise<void> {
  errors.value = validate()
  message.value = ''
  if (Object.values(errors.value).some(Boolean)) return
  loading.value = true
  report.value = null
  try {
    const created = await createStrategyFitness(buildPayload())
    job.value = {
      job_id: created.job_id,
      status: 'queued',
      progress: 0,
      total_candidates: 0,
      total_scanned: 0,
      errors: 0,
      error: null,
      result: null,
    }
    for (;;) {
      const state = await getStrategyFitness(created.job_id)
      job.value = state
      if (state.status === 'completed') {
        report.value = await getStrategyFitnessResults(created.job_id)
        message.value = `适配性评估完成：处理 ${report.value.processed} 只股票，其中 ${passedCount.value} 只达到推荐阈值。`
        await loadHistory()
        return
      }
      if (state.status === 'failed') {
        message.value = state.error ?? '策略适配性评估任务失败，请检查配置和服务状态后重试。'
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

async function loadHistory(): Promise<void> {
  historyLoading.value = true
  try {
    history.value = await fetchStrategyFitnessHistory()
  } catch (error) {
    message.value = apiMessage(error)
  } finally {
    historyLoading.value = false
  }
}

async function loadHistoryDetail(historyId: string): Promise<void> {
  try {
    const loaded = await getStrategyFitnessHistory(historyId)
    report.value = loaded.result
    message.value = `已加载历史策略适配性评估 ${historyId}。`
  } catch (error) {
    message.value = apiMessage(error)
  }
}

async function removeHistory(historyId: string): Promise<void> {
  try {
    await deleteStrategyFitnessHistory(historyId)
    await loadHistory()
    message.value = '历史策略适配性评估已删除。'
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

function labelText(label: FitnessLabel): string {
  return { strong: '高适配', watch: '观察', weak: '低适配', insufficient: '样本不足' }[label]
}

function phaseText(phase: FitnessPhaseMetrics): string {
  return `${phase.total_trades} 笔 · 收益 ${formatPercent(phase.total_return)} · 回撤 ${formatPercent(phase.max_drawdown)} · 胜率 ${formatPercent(phase.win_rate)} · 盈亏比 ${formatNumber(phase.profit_factor)} · 期望 ${formatPercent(phase.expectancy)}`
}

function downloadResult(): void {
  if (!report.value) return
  const url = URL.createObjectURL(new Blob([JSON.stringify(report.value, null, 2)], { type: 'application/json' }))
  const link = document.createElement('a')
  link.href = url
  link.download = 'strategy-fitness-report.json'
  link.click()
  window.setTimeout(() => URL.revokeObjectURL(url), 0)
}

onMounted(async () => {
  try {
    metadata.value = await fetchMetadata()
    resetPresetDefaults()
    await loadHistory()
  } catch (error) {
    message.value = apiMessage(error)
  }
})

watch(() => form.formulaText, (value) => {
  if (form.mode !== 'custom' || customMetadata.value === null) return
  if (value.trim() === parsedFormulaText.value) return
  customMetadata.value = null
  parsedFormulaText.value = ''
  form.buyRule = ruleForSignal('')
  form.sellRule = ruleForSignal('')
  form.rankingValue = ''
  customParseError.value = '公式已修改，请重新解析。'
})
</script>

<template>
  <div class="screen-page fitness-page" data-testid="strategy-fitness-page">
    <header class="topbar">
      <a class="brand" href="/indicator-lab" aria-label="指标实验室首页"><img class="brand-mark" src="/indicator-lab-mark.png" alt="" aria-hidden="true" width="34" height="34"><span><strong>Indicator Lab</strong><small>指标实验室</small></span></a>
      <nav aria-label="主导航">
        <a class="nav-link" href="/indicator-lab">公式选股</a>
        <a class="nav-link" href="/backtest">单股回测</a>
        <a class="nav-link" href="/portfolio-backtest">组合回测</a>
        <a class="nav-link" href="/strategy-lab">策略实验室</a>
        <a class="nav-link active" href="/strategy-fitness" aria-current="page">策略适配性</a>
        <a class="nav-link" href="/market-data">本地行情</a>
      </nav>
      <div class="topbar-status"><span class="status-dot" aria-hidden="true"></span> 本地数据模式</div>
    </header>

    <main class="content-shell">
      <section class="page-intro">
        <div><p class="eyebrow">STRATEGY FITNESS / TIME SPLIT</p><h1>策略适配性</h1><p class="intro-copy">用同一套买卖规则分别回测每个标的，按训练、验证、测试三段时间判断是否适合。</p></div>
        <div class="intro-note"><span class="note-label">评估原则</span><strong>时间顺序 · 样本外优先</strong><small>先筛适配性，再交给组合回测按当前指标排序</small></div>
      </section>

      <div v-if="message" class="notice" :class="{ error: !message.includes('完成') }" role="alert" data-testid="fitness-message">{{ message }}</div>

      <div class="workspace-grid fitness-grid">
        <form class="config-panel panel fitness-config" data-testid="fitness-config" @submit.prevent="submit">
          <div class="scope-config inline-scope-config" data-testid="fitness-scope"><div class="scope-options"><span>证券类型</span><label v-for="option in INSTRUMENT_TYPE_OPTIONS" :key="option.key"><input type="checkbox" :data-testid="`fitness-scope-type-${option.key}`" :checked="form.instrumentTypes.includes(option.key)" @change="toggleInstrumentType(option.key)">{{ option.label }}</label></div><div class="scope-options"><span>板块</span><label v-for="option in BOARD_OPTIONS" :key="option.key"><input type="checkbox" :data-testid="`fitness-scope-board-${option.key}`" :checked="form.boards.includes(option.key)" @change="toggleBoard(option.key)">{{ option.label }}</label></div><small class="scope-helper">未勾选表示全部；类型和板块同时选择时取交集。</small></div>
          <div class="panel-heading"><div><span class="section-kicker">01 / CONFIGURE</span><h2>评估配置</h2></div><span class="required-hint">* 必填</span></div>
          <div class="formula-mode-tabs" role="tablist" aria-label="公式来源"><button type="button" :class="{ active: form.mode === 'preset' }" data-testid="fitness-mode-preset" role="tab" :aria-selected="form.mode === 'preset'" @click="setMode('preset')">预置指标</button><button type="button" :class="{ active: form.mode === 'custom' }" data-testid="fitness-mode-custom" role="tab" :aria-selected="form.mode === 'custom'" @click="setMode('custom')">自定义公式</button></div>

          <fieldset v-if="form.mode === 'custom'" class="form-section custom-formula-section"><legend>粘贴通达信公式</legend><textarea v-model="form.formulaText" data-testid="fitness-formula" rows="6" spellcheck="false" placeholder="例如：买入:CROSS(C,MA(C,20)); 卖出:CROSS(MA(C,20),C); 强度:C/MA(C,20);"></textarea><button class="secondary-button" data-testid="fitness-parse-formula" type="button" :disabled="customParseLoading" @click="parseCustomFormula">{{ customParseLoading ? '解析中…' : '解析公式' }}</button><p class="helper">命名输出可用于买入/卖出指标条件、指标比较和排序。</p><p v-if="customParseError" class="field-error">{{ customParseError }}</p><div v-if="customMetadata" class="custom-formula-meta"><span>{{ customMetadata.parameters.length }} 个参数</span><span>{{ customMetadata.signals.length }} 个买卖输出</span><span>{{ customMetadata.values?.length ?? 0 }} 个指标输出</span></div><div v-if="customMetadata?.parameters.length" class="parameter-grid"><div v-for="parameter in customMetadata.parameters" :key="parameter.name"><label :for="`fitness-param-${parameter.name}`">{{ parameter.name }}</label><input :id="`fitness-param-${parameter.name}`" v-model.number="form.formulaParameters[parameter.name]" :data-testid="`fitness-param-${parameter.name}`" type="number" :min="parameter.minimum" :max="parameter.maximum" :step="parameter.step"></div></div><p v-if="errors.formulaText" class="field-error">{{ errors.formulaText }}</p><p v-if="errors.formulaParameters" class="field-error">{{ errors.formulaParameters }}</p></fieldset>

          <fieldset class="form-section"><legend>数据范围</legend><p class="helper">读取已经导入本地 DuckDB 的日线，不会上传行情文件。请先在“本地行情”页面导入数据。</p><div class="compact-fields fitness-fields-top"><div><label for="fitness-universe">品种范围</label><select id="fitness-universe" v-model="form.universe" data-testid="fitness-universe"><option value="all">沪深全部品种</option><option value="sh">仅上海</option><option value="sz">仅深圳</option><option value="custom">自定义列表</option></select></div><div v-if="form.universe === 'custom'"><label for="fitness-universe-file">列表文件</label><input id="fitness-universe-file" v-model="form.universeFile" data-testid="fitness-universe-file" type="text" placeholder="每行 SH 600000"></div></div><p v-if="errors.universeFile" class="field-error">{{ errors.universeFile }}</p></fieldset>

          <p class="helper rule-editor-intro">适配性评估与组合回测使用同一套指标条件构建器，支持指标之间比较和指标与固定值比较。</p>
          <RuleGroupEditor
            :model-value="form.buyRule"
            :values="availableValues"
            :condition-operators="metadata?.condition_operators"
            title="买入条件组合"
            testid="fitness-buy-rule"
            @update:model-value="setBuyRule"
          />
          <p v-if="errors.buyRule" class="field-error">{{ errors.buyRule }}</p>

          <fieldset class="form-section"><legend>策略输出</legend><label for="fitness-ranking-value">记录指标输出 <span class="required">*</span></label><select id="fitness-ranking-value" v-model="form.rankingValue" data-testid="fitness-ranking-value" :disabled="availableValues.length === 0"><option value="" disabled>请选择指标输出</option><option v-for="value in availableValues" :key="value.id" :value="value.id">{{ valueOptionLabel(value) }}</option></select><p v-if="errors.rankingValue" class="field-error">{{ errors.rankingValue }}</p><div class="compact-fields fitness-fields-top"><div><label for="fitness-rank-order">组合参考排序</label><select id="fitness-rank-order" v-model="form.rankOrder" data-testid="fitness-rank-order"><option value="desc">从高到低</option><option value="asc">从低到高</option></select></div><div><label>用途</label><p class="field-static">适配性评估不使用当前排序值决定单股交易；它用于记录策略上下文。</p></div></div></fieldset>

          <fieldset class="form-section"><legend>卖出规则 <span class="required">至少一项</span></legend><RuleGroupEditor :model-value="form.sellRule" :values="availableValues" :condition-operators="metadata?.condition_operators" title="卖出条件" testid="fitness-sell-rule" @update:model-value="setSellRule" /><div class="rule-toggle"><label><input v-model="form.stopLossEnabled" data-testid="fitness-stop-loss-enabled" type="checkbox"><span>止损</span></label><input v-model.number="form.stopLossPct" data-testid="fitness-stop-loss" type="number" min="0.01" max="100" step="0.5" aria-label="止损百分比"><span>%</span></div><div class="rule-toggle"><label><input v-model="form.takeProfitEnabled" data-testid="fitness-take-profit-enabled" type="checkbox"><span>止盈</span></label><input v-model.number="form.takeProfitPct" data-testid="fitness-take-profit" type="number" min="0.01" max="1000" step="0.5" aria-label="止盈百分比"><span>%</span></div><p v-if="errors.sellRules" class="field-error">{{ errors.sellRules }}</p><p v-if="errors.stopLossPct" class="field-error">{{ errors.stopLossPct }}</p><p v-if="errors.takeProfitPct" class="field-error">{{ errors.takeProfitPct }}</p><p class="helper">规则条件在收盘确认；止损和止盈作为独立的风险控制。</p></fieldset>

          <fieldset class="form-section"><legend>时间切分</legend><div class="compact-fields"><div><label for="fitness-start">开始日期</label><input id="fitness-start" v-model="form.startDate" data-testid="fitness-start" type="date"></div><div><label for="fitness-end">结束日期</label><input id="fitness-end" v-model="form.endDate" data-testid="fitness-end" type="date"></div></div><p v-if="errors.endDate" class="field-error">{{ errors.endDate }}</p><div class="compact-fields fitness-fields-top"><div><label for="fitness-train-ratio">训练比例 (%)</label><input id="fitness-train-ratio" v-model.number="form.trainRatio" data-testid="fitness-train-ratio" type="number" min="1" max="98" step="5"></div><div><label for="fitness-validation-ratio">验证比例 (%)</label><input id="fitness-validation-ratio" v-model.number="form.validationRatio" data-testid="fitness-validation-ratio" type="number" min="1" max="98" step="5"></div></div><p v-if="errors.trainRatio" class="field-error">{{ errors.trainRatio }}</p><p v-if="errors.validationRatio" class="field-error">{{ errors.validationRatio }}</p><div class="compact-fields fitness-fields-top"><div><label for="fitness-min-trades">验证/测试最少成交</label><input id="fitness-min-trades" v-model.number="form.minTrades" data-testid="fitness-min-trades" type="number" min="1" step="1"></div><div><label for="fitness-max-drawdown">测试期最大回撤 (%)</label><input id="fitness-max-drawdown" v-model.number="form.maxTestDrawdown" data-testid="fitness-max-drawdown" type="number" min="0" max="100" step="5"></div></div><p v-if="errors.minTrades" class="field-error">{{ errors.minTrades }}</p><p v-if="errors.maxTestDrawdown" class="field-error">{{ errors.maxTestDrawdown }}</p><p class="helper">默认 60% 训练、20% 验证、20% 测试；比例按全市场共同日期切分，避免股票之间时间边界不同。</p></fieldset>

          <button class="advanced-toggle" data-testid="fitness-advanced-toggle" type="button" :aria-expanded="showAdvanced" @click="showAdvanced = !showAdvanced"><span>{{ showAdvanced ? '收起高级设置' : '高级设置' }}</span><small>资金 · 执行 · 费用</small></button><div v-if="showAdvanced" class="advanced-settings" data-testid="fitness-advanced-settings"><fieldset class="form-section compact-fields"><div><label for="fitness-initial-cash">初始资金</label><input id="fitness-initial-cash" v-model.number="form.initialCash" data-testid="fitness-initial-cash" type="number" min="1" step="10000"></div><div><label for="fitness-execution">成交方式</label><select id="fitness-execution" v-model="form.execution" data-testid="fitness-execution"><option value="next_open">下一根开盘</option><option value="next_close">下一根收盘</option></select></div></fieldset><p v-if="errors.initialCash" class="field-error">{{ errors.initialCash }}</p><fieldset class="form-section compact-fields"><div><label for="fitness-commission">佣金费率</label><input id="fitness-commission" v-model.number="form.commission" data-testid="fitness-commission" type="number" min="0" max="0.1" step="0.0001"></div><div><label for="fitness-min-commission">最低佣金</label><input id="fitness-min-commission" v-model.number="form.minCommission" data-testid="fitness-min-commission" type="number" min="0" step="0.01"></div></fieldset><fieldset class="form-section compact-fields"><div><label for="fitness-stamp-tax">印花税率</label><input id="fitness-stamp-tax" v-model.number="form.stampTax" data-testid="fitness-stamp-tax" type="number" min="0" max="0.1" step="0.0001"></div><div><label for="fitness-slippage">每股滑点（元）</label><input id="fitness-slippage" v-model.number="form.slippage" data-testid="fitness-slippage" type="number" min="0" step="0.001"></div></fieldset></div>
          <div class="action-stack"><button class="primary-button" data-testid="start-fitness" type="button" :disabled="!canSubmit" @click="submit"><span v-if="loading" class="spinner" aria-hidden="true"></span>{{ loading ? '评估中…' : '开始适配性评估' }}</button></div><p class="privacy-note">只使用每个时间点之前的数据；结果用于研究，不是投资建议。</p>
        </form>

        <section class="results-panel panel" data-testid="fitness-results" aria-live="polite"><div class="panel-heading results-heading"><div><span class="section-kicker">02 / RESULTS</span><h2>标的适配结果</h2></div><button v-if="report" class="secondary-button export-backtest" type="button" data-testid="export-fitness" @click="downloadResult">导出 JSON</button></div><div v-if="!report" class="empty-state" data-testid="fitness-empty"><div class="empty-icon" aria-hidden="true">/</div><h3>{{ loading ? '正在逐只评估策略…' : '还没有适配性结果' }}</h3><p>{{ loading ? '按相同交易规则生成训练、验证和测试三段报告。' : '配置策略和时间切分后，开始评估股票池。' }}</p></div><template v-else><div class="backtest-summary"><span class="result-badge">通过 {{ passedCount }} / {{ report.processed }}</span><span>{{ report.start_date }} → {{ report.end_date }}</span><span>训练截止 {{ report.train_end_date }}</span><span>验证截止 {{ report.validation_end_date }}</span><span>测试最少 {{ report.min_trades }} 笔</span><span>测试回撤 ≤ {{ formatPercent(report.max_test_drawdown) }}</span></div><div class="metrics-strip fitness-metrics"><div><span>股票总数</span><strong>{{ report.total_candidates }}</strong></div><div><span>已处理</span><strong>{{ report.processed }}</strong></div><div><span>达到推荐</span><strong class="positive" data-testid="fitness-passed-count">{{ passedCount }}</strong></div><div><span>跳过 / 错误</span><strong>{{ report.skipped }} / {{ report.errors }}</strong></div></div><div class="progress-track fitness-progress" aria-label="适配性评估进度"><span :style="{ width: `${Math.round((job?.progress ?? 1) * 100)}%` }"></span></div><div v-if="!report.results.length" class="inline-empty">没有股票完成三段适配性评估。</div><div v-else class="table-wrap"><table><caption class="sr-only">策略适配性评估结果</caption><thead><tr><th>市场</th><th>代码</th><th>适配性</th><th>检查项</th><th>训练期</th><th>验证期</th><th>测试期</th><th>有效区间</th></tr></thead><tbody><tr v-for="item in report.results" :key="`${item.market}-${item.code}`"><td><span class="market-pill" :class="item.market.toLowerCase()">{{ item.market }}</span></td><td><strong class="code">{{ item.code }}</strong></td><td><span class="fitness-score" :class="item.label"><strong>{{ item.suitability_score.toFixed(2) }}</strong><small>{{ labelText(item.label) }}</small></span></td><td><strong>{{ item.passed_checks }} / {{ item.total_checks }}</strong><div class="fitness-periods">{{ item.positive_periods }} 段收益为正</div></td><td :data-testid="`fitness-train-${item.code}`" class="fitness-phase">{{ phaseText(item.train) }}</td><td :data-testid="`fitness-validation-${item.code}`" class="fitness-phase">{{ phaseText(item.validation) }}</td><td :data-testid="`fitness-test-${item.code}`" class="fitness-phase">{{ phaseText(item.test) }}</td><td class="muted-code">{{ item.data_start }}<br>至 {{ item.data_end }}</td></tr></tbody></table></div><div v-if="report.results.length" class="fitness-details"><details v-for="item in report.results.slice(0, 20)" :key="`details-${item.market}-${item.code}`"><summary>{{ item.market }} {{ item.code }} · 查看检查项</summary><div class="fitness-checks"><span v-for="check in item.checks" :key="check.id" :class="{ passed: check.passed }">{{ check.passed ? '通过' : '未通过' }} · {{ check.label }}</span></div></details></div><div v-if="report.failure_reasons && Object.keys(report.failure_reasons).length" class="failure-summary"><strong>失败摘要</strong><span v-for="(count, reason) in report.failure_reasons" :key="reason">{{ reason }} · {{ count }}</span></div><p v-if="report.diagnostic" class="diagnostic" data-testid="fitness-diagnostic">{{ report.diagnostic }}</p></template></section>
      </div>

      <BacktestHistoryPanel
        :items="history"
        :loading="historyLoading"
        testid="fitness-history"
        title="历史策略适配性评估"
        empty-text="暂无历史策略适配性评估。"
        @refresh="loadHistory"
        @load="loadHistoryDetail"
        @remove="removeHistory"
      />
    </main>
  </div>
</template>
