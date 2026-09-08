<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'

import { createJob, createSyncJob, fetchMetadata, FormulaScreenApiError, getJob, getResults, getSyncJob, parseFormula } from '../api/formulaScreen'
import type { ConditionOperator, CustomFormulaMetadata, FormulaScreenMetadata, InstrumentBoard, InstrumentType, JobState, MarketSyncJobState, ResultsMeta, SavedConditionSet, ScreenCondition, ScreenFormState, ScreenResult, RuleMode, ValueDefinition } from '../types'
import { buildScanPayload, createSavedConditionSet, DEFAULT_PRESET_SIGNALS, deleteConditionSet, filterKnownSignals, loadSavedConditionSets, loadSavedForm, resultsToCsv, saveConditionSet, saveForm, signalDisplayName, validateScreenForm, valueOptionLabel } from '../utils/formulaScreen'

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

const CONDITION_OPERATOR_OPTIONS: { value: ConditionOperator; label: string }[] = [
  { value: 'gt', label: '大于' },
  { value: 'gte', label: '大于等于' },
  { value: 'lt', label: '小于' },
  { value: 'lte', label: '小于等于' },
  { value: 'eq', label: '等于' },
  { value: 'neq', label: '不等于' },
  { value: 'cross_up', label: '上穿' },
  { value: 'cross_down', label: '下穿' },
]

function emptyCondition(leftValue = 'indicator_three.var1'): ScreenCondition {
  return {
    left_value: leftValue,
    operator: 'lt',
    right_value: null,
    right_constant: 10,
  }
}

const metadata = ref<FormulaScreenMetadata | null>(null)
const customMetadata = ref<CustomFormulaMetadata | null>(null)
const savedForm = loadSavedForm()
const form = reactive<ScreenFormState>({
  mode: savedForm.mode ?? 'preset',
  ruleMode: savedForm.ruleMode ?? 'signals',
  selectedSignals: savedForm.selectedSignals ?? [...DEFAULT_PRESET_SIGNALS],
  conditions: savedForm.conditions ?? [emptyCondition()],
  combineMode: savedForm.combineMode ?? 'at_least',
  minimumMatches: savedForm.minimumMatches ?? 2,
  universe: savedForm.universe ?? 'all',
  universeFile: savedForm.universeFile ?? '',
  instrumentTypes: savedForm.instrumentTypes ?? [],
  boards: savedForm.boards ?? [],
  workers: savedForm.workers ?? 8,
  period: savedForm.period ?? 'daily',
  formulaText: savedForm.formulaText ?? '',
  formulaParameters: savedForm.formulaParameters ?? {},
})
const errors = ref<Record<string, string>>({})
const message = ref('')
const loading = ref(false)
const job = ref<JobState | null>(null)
const results = ref<ScreenResult[]>([])
const resultMeta = ref<ResultsMeta | null>(null)
const customParseLoading = ref(false)
const customParseError = ref('')
const parsedFormulaText = ref('')
const showAdvanced = ref(false)
const syncLoading = ref(false)
const syncJob = ref<MarketSyncJobState | null>(null)
const savedConditionSets = ref<SavedConditionSet[]>(loadSavedConditionSets())
const selectedConditionSetId = ref('')
const conditionSetName = ref('')

const progressPercent = computed(() => Math.round((job.value?.progress ?? 0) * 100))
const syncProgressPercent = computed(() => Math.round((syncJob.value?.progress ?? 0) * 100))
const syncRemaining = computed(() => Math.max(
  (syncJob.value?.total_candidates ?? 0) - (syncJob.value?.total_scanned ?? 0),
  0,
))
const presetSignals = computed(() => (
  metadata.value?.indicators.flatMap((indicator) => indicator.signals) ?? []
))
const valueGroups = computed<{ label: string; values: ValueDefinition[] }[]>(() => {
  if (form.mode === 'custom') {
    return customMetadata.value?.values?.length
      ? [{ label: '当前自定义指标', values: customMetadata.value.values }]
      : []
  }
  return (metadata.value?.indicators ?? [])
    .map((indicator) => ({ label: indicator.display_name, values: indicator.values ?? [] }))
    .filter((group) => group.values.length > 0)
})
const availableValues = computed(() => valueGroups.value.flatMap((group) => group.values))
const conditionOperators = computed(() => metadata.value?.condition_operators?.length
  ? metadata.value.condition_operators
  : CONDITION_OPERATOR_OPTIONS)
const canSubmit = computed(() => (
  !loading.value
  && !syncLoading.value
  && !customParseLoading.value
  && metadata.value !== null
  && (form.mode !== 'custom' || customMetadata.value !== null)
  && (form.ruleMode !== 'conditions' || form.conditions.length > 0)
))

function normalizeConditionsToAvailableValues(): void {
  const values = availableValues.value
  if (values.length === 0 || form.conditions.length === 0) return

  const availableIds = new Set(values.map((value) => value.id))
  const fallbackLeft = values[0].id
  form.conditions = form.conditions.map((condition) => {
    const leftValue = availableIds.has(condition.left_value)
      ? condition.left_value
      : fallbackLeft
    if (condition.right_value === null || availableIds.has(condition.right_value)) {
      return { ...condition, left_value: leftValue }
    }

    const fallbackRight = values.find((value) => value.id !== leftValue)?.id
    return fallbackRight
      ? { ...condition, left_value: leftValue, right_value: fallbackRight, right_constant: null }
      : { ...condition, left_value: leftValue, right_value: null, right_constant: condition.right_constant ?? 0 }
  })
}

function setMode(mode: ScreenFormState['mode']): void {
  if (form.mode === mode) return
  form.mode = mode
  form.selectedSignals = mode === 'preset' ? [...DEFAULT_PRESET_SIGNALS] : []
  customMetadata.value = null
  parsedFormulaText.value = ''
  errors.value = {}
  message.value = ''
  customParseError.value = ''
  normalizeConditionsToAvailableValues()
}

function setRuleMode(ruleMode: RuleMode): void {
  form.ruleMode = ruleMode
  if (ruleMode === 'conditions' && form.conditions.length === 0) {
    form.conditions = [emptyCondition(availableValues.value[0]?.id)]
  }
  if (ruleMode === 'conditions' && form.combineMode === 'at_least') {
    form.minimumMatches = Math.min(
      Math.max(form.minimumMatches ?? 1, 1),
      Math.max(form.conditions.length, 1),
    )
  }
  normalizeConditionsToAvailableValues()
  errors.value = {}
  message.value = ''
}

function toggleSignal(signalId: string, checked: boolean): void {
  const next = new Set(form.selectedSignals)
  if (checked) next.add(signalId)
  else next.delete(signalId)
  form.selectedSignals = [...next]
  if (errors.value.selectedSignals) errors.value = { ...errors.value, selectedSignals: '' }
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

function updateCondition(index: number, patch: Partial<ScreenCondition>): void {
  form.conditions = form.conditions.map((condition, current) => (
    current === index ? { ...condition, ...patch } : condition
  ))
  if (errors.value.conditions || errors.value[`condition-${index}`]) {
    const nextErrors = { ...errors.value }
    delete nextErrors.conditions
    delete nextErrors[`condition-${index}`]
    errors.value = nextErrors
  }
}

function addCondition(): void {
  form.conditions = [...form.conditions, emptyCondition(availableValues.value[0]?.id)]
}

function removeCondition(index: number): void {
  if (form.conditions.length <= 1) return
  form.conditions = form.conditions.filter((_, current) => current !== index)
  if (form.combineMode === 'at_least') {
    form.minimumMatches = Math.min(
      Math.max(form.minimumMatches ?? 1, 1),
      form.conditions.length,
    )
  }
}

function setConditionRightType(index: number, type: 'constant' | 'value'): void {
  const condition = form.conditions[index]
  if (!condition) return
  if (type === 'constant') {
    updateCondition(index, {
      right_value: null,
      right_constant: condition.right_constant ?? 0,
    })
    return
  }
  const fallback = availableValues.value.find((value) => value.id !== condition.left_value)?.id
    ?? availableValues.value[0]?.id
    ?? null
  updateCondition(index, { right_value: condition.right_value ?? fallback, right_constant: null })
}

function valueLabel(valueId: string | null): string {
  if (!valueId) return '未选择'
  return availableValues.value.find((value) => value.id === valueId)?.display_name ?? valueId
}

function resultValueLabel(valueId: string): string {
  return valueLabel(valueId) === valueId
    ? valueId.split('.').at(-1)?.toUpperCase() ?? valueId
    : valueLabel(valueId)
}

function conditionOperatorLabel(operator: ConditionOperator): string {
  return conditionOperators.value.find((item) => item.value === operator)?.label ?? operator
}

function conditionText(condition: ScreenCondition): string {
  const right = condition.right_value !== null
    ? valueLabel(condition.right_value)
    : String(condition.right_constant ?? '')
  return `${valueLabel(condition.left_value)} ${conditionOperatorLabel(condition.operator)} ${right}`
}

function resultLabel(signalId: string): string {
  const match = /^condition_(\d+)$/.exec(signalId)
  if (match) {
    const condition = form.conditions[Number(match[1]) - 1]
    return condition ? conditionText(condition) : `条件 ${match[1]}`
  }
  return signalDisplayName(signalId, metadata.value, customMetadata.value)
}

async function applySelectedConditionSet(): Promise<void> {
  const selected = savedConditionSets.value.find((item) => item.id === selectedConditionSetId.value)
  if (!selected) return
  form.ruleMode = selected.ruleMode
  form.selectedSignals = [...selected.selectedSignals]
  form.conditions = selected.conditions.map((condition) => ({ ...condition }))
  form.combineMode = selected.combineMode
  form.minimumMatches = selected.minimumMatches
  form.mode = selected.mode
  form.formulaText = selected.formulaText
  form.formulaParameters = { ...selected.formulaParameters }
  customMetadata.value = null
  parsedFormulaText.value = ''
  errors.value = {}
  if (form.mode === 'custom' && form.formulaText.trim()) await parseCustomFormula()
  else normalizeConditionsToAvailableValues()
  message.value = `已加载指标组合：${selected.name}`
}

function saveCurrentConditionSet(): void {
  const name = conditionSetName.value.trim()
  if (!name) {
    errors.value = { ...errors.value, conditionSetName: '请输入组合名称。' }
    return
  }
  const validationErrors = validateScreenForm(form, customMetadata.value)
  if (Object.keys(validationErrors).length > 0) {
    errors.value = validationErrors
    return
  }
  const saved = createSavedConditionSet(name, form)
  savedConditionSets.value = saveConditionSet(saved)
  selectedConditionSetId.value = saved.id
  conditionSetName.value = ''
  message.value = `已保存指标组合：${saved.name}`
}

function deleteSelectedConditionSet(): void {
  if (!selectedConditionSetId.value) return
  savedConditionSets.value = deleteConditionSet(selectedConditionSetId.value)
  selectedConditionSetId.value = ''
  message.value = '已删除指标组合。'
}

function apiMessage(error: unknown, context: string): string {
  if (error instanceof FormulaScreenApiError) return error.message
  if (error instanceof Error && error.message) return `${context}：${error.message}`
  return context
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
    form.selectedSignals = parsed.signals.map((signal) => signal.id)
    normalizeConditionsToAvailableValues()
    const conditionCount = form.ruleMode === 'conditions'
      ? form.conditions.length
      : parsed.signals.length
    form.minimumMatches = Math.min(
      Math.max(form.minimumMatches ?? 1, 1),
      Math.max(conditionCount, 1),
    )
    errors.value = {}
    message.value = `公式解析完成：已识别 ${parsed.parameters.length} 个参数、${parsed.signals.length} 个输出信号。`
  } catch (error) {
    customMetadata.value = null
    parsedFormulaText.value = ''
    form.selectedSignals = []
    customParseError.value = error instanceof FormulaScreenApiError
      ? error.message
      : '公式解析失败，请检查语法和函数是否受支持。'
  } finally {
    customParseLoading.value = false
  }
}

async function pollUntilFinished(jobId: string): Promise<void> {
  for (;;) {
    const state = await getJob(jobId)
    job.value = state
    if (state.status === 'completed') {
      const payload = await getResults(jobId)
      results.value = payload.results
      resultMeta.value = payload.meta
      message.value = `扫描完成：命中 ${payload.meta.total_signals} 个条件，得到 ${payload.results.length} 只股票。`
      return
    }
    if (state.status === 'failed') {
      throw new FormulaScreenApiError(state.error ?? '扫描任务执行失败。', 500)
    }
    await new Promise((resolve) => window.setTimeout(resolve, 250))
  }
}

async function syncMarketData(): Promise<void> {
  syncLoading.value = true
  message.value = ''
  try {
    const universe = form.universe === 'custom' ? 'all' : form.universe
    const created = await createSyncJob({
      universe,
      ...(form.instrumentTypes.length > 0 ? { instrument_types: [...form.instrumentTypes] } : {}),
      ...(form.boards.length > 0 ? { boards: [...form.boards] } : {}),
    })
    syncJob.value = {
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
      const state = await getSyncJob(created.job_id)
      syncJob.value = state
      if (state.status === 'completed') {
        const result = state.result
        message.value = result
          ? `行情同步完成：写入 ${result.written_bars} 根，更新 ${result.updated_files} 个标的。`
          : '行情同步完成。'
        return
      }
      if (state.status === 'failed') {
        throw new FormulaScreenApiError(state.error ?? '行情同步执行失败。', 500)
      }
      await new Promise((resolve) => window.setTimeout(resolve, 250))
    }
  } catch (error) {
    message.value = apiMessage(error, '行情同步失败，请检查网络和 DuckDB 数据状态后重试')
  } finally {
    syncLoading.value = false
  }
}

async function submit(): Promise<void> {
  errors.value = validateScreenForm(form, customMetadata.value)
  message.value = ''
  if (Object.values(errors.value).some(Boolean)) return

  loading.value = true
  results.value = []
  resultMeta.value = null
  let jobCreated = false
  try {
    const created = await createJob(buildScanPayload(form))
    jobCreated = true
    job.value = {
      job_id: created.job_id,
      status: 'queued',
      progress: 0,
      total_candidates: 0,
      total_scanned: 0,
      total_signals: 0,
      errors: 0,
      skipped: 0,
      error: null,
    }
    await pollUntilFinished(created.job_id)
  } catch (error) {
    message.value = apiMessage(
      error,
      jobCreated
        ? '扫描任务执行失败，请检查后端服务、数据目录和配置后重试'
        : '扫描任务提交失败，请检查后端服务、数据目录和配置后重试',
    )
  } finally {
    loading.value = false
  }
}

function download(filename: string, content: string, type: string): void {
  const url = URL.createObjectURL(new Blob([content], { type }))
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  link.click()
  URL.revokeObjectURL(url)
}

function exportJson(): void {
  download('formula-screen-results.json', JSON.stringify({ summary: resultMeta.value, results: results.value }, null, 2), 'application/json')
}

function exportCsv(): void {
  download('formula-screen-results.csv', resultsToCsv(results.value), 'text/csv;charset=utf-8')
}

onMounted(async () => {
  try {
    metadata.value = await fetchMetadata()
    if (form.mode === 'preset') {
      form.selectedSignals = filterKnownSignals(
        form.selectedSignals,
        presetSignals.value.map((signal) => signal.id),
      )
      if (form.combineMode === 'at_least' && form.selectedSignals.length > 0) {
        form.minimumMatches = Math.min(
          Math.max(form.minimumMatches ?? 1, 1),
          form.selectedSignals.length,
        )
      }
    }
    if (form.ruleMode === 'conditions' && form.conditions.length === 0) {
      form.conditions = [emptyCondition(availableValues.value[0]?.id)]
    }
    if (form.mode === 'custom' && form.formulaText.trim()) {
      await parseCustomFormula()
    }
  } catch (error) {
    message.value = apiMessage(error, '公式元数据加载失败，请检查后端服务和网络后重试')
  }
})

watch(form, (value) => saveForm(value), { deep: true })
watch(() => form.formulaText, (value) => {
  if (form.mode !== 'custom' || customMetadata.value === null) return
  if (value.trim() === parsedFormulaText.value) return
  customMetadata.value = null
  parsedFormulaText.value = ''
  form.selectedSignals = []
  customParseError.value = '公式已修改，请重新解析。'
})
</script>

<template>
  <div class="screen-page" data-testid="formula-screen-page">
    <header class="topbar">
      <a class="brand" href="/indicator-lab" aria-label="指标实验室首页">
        <img class="brand-mark" src="/indicator-lab-mark.png" alt="" aria-hidden="true" width="34" height="34">
        <span><strong>Indicator Lab</strong><small>指标实验室</small></span>
      </a>
      <nav aria-label="主导航">
        <a class="nav-link active" href="/indicator-lab" aria-current="page">公式选股</a>
        <a class="nav-link" href="/backtest">单股回测</a>
        <a class="nav-link" href="/portfolio-backtest">组合回测</a>
        <a class="nav-link" href="/strategy-fitness">策略适配性</a>
        <a class="nav-link" href="/market-data">本地行情</a>
      </nav>
      <div class="topbar-status"><span class="status-dot" aria-hidden="true"></span> 本地数据模式</div>
    </header>

    <main class="content-shell">
      <section class="page-intro">
        <div>
          <p class="eyebrow">FORMULA SCREEN / DAILY</p>
          <h1>公式选股</h1>
          <p class="intro-copy">把通达信条件拆成可组合的信号，扫描已完成的日线 K 线。</p>
        </div>
        <div class="intro-note"><span class="note-label">扫描范围</span><strong>DuckDB 全量行情</strong><small>信号筛选和回测统一读取本地数据仓库</small></div>
      </section>

      <div v-if="message" class="notice" :class="{ error: !message.includes('完成') }" role="alert" data-testid="screen-message">
        <span>{{ message }}</span>
      </div>

      <div class="workspace-grid">
        <form class="config-panel panel" data-testid="screen-config" @submit.prevent="submit">
          <div class="panel-heading">
            <div><span class="section-kicker">01 / CONFIGURE</span><h2>扫描配置</h2></div>
            <span class="required-hint">* 必填</span>
          </div>

          <div class="formula-mode-tabs" data-testid="formula-mode" role="tablist" aria-label="公式来源">
            <button type="button" :class="{ active: form.mode === 'preset' }" data-testid="mode-preset" role="tab" :aria-selected="form.mode === 'preset'" @click="setMode('preset')">预置指标</button>
            <button type="button" :class="{ active: form.mode === 'custom' }" data-testid="mode-custom" role="tab" :aria-selected="form.mode === 'custom'" @click="setMode('custom')">自定义公式</button>
          </div>

          <div class="formula-mode-tabs rule-mode-tabs" data-testid="rule-mode" role="tablist" aria-label="筛选方式">
            <button type="button" :class="{ active: form.ruleMode === 'signals' }" data-testid="rule-mode-signals" role="tab" :aria-selected="form.ruleMode === 'signals'" @click="setRuleMode('signals')">快捷信号</button>
            <button type="button" :class="{ active: form.ruleMode === 'conditions' }" data-testid="rule-mode-conditions" role="tab" :aria-selected="form.ruleMode === 'conditions'" @click="setRuleMode('conditions')">指标条件</button>
          </div>

          <fieldset v-if="form.mode === 'custom'" class="form-section custom-formula-section">
            <legend>粘贴通达信公式</legend>
            <textarea v-model="form.formulaText" data-testid="custom-formula" rows="8" spellcheck="false" placeholder="例如：N:=5; SIGNAL:CROSS(C,REF(C,N));"></textarea>
            <button class="secondary-button" data-testid="parse-formula" type="button" :disabled="customParseLoading" @click="parseCustomFormula">
              {{ customParseLoading ? '解析中…' : '解析公式' }}
            </button>
            <p class="helper">支持常用数组函数：REF、SMA、EMA、MA、LLV、HHV、BARSLAST、COUNT、CROSS、IF、MAX、MIN、ABS。参数需用 <code>名称:=数值</code> 显式声明。</p>
            <p v-if="customParseError" class="field-error" data-testid="formula-parse-error">{{ customParseError }}</p>
            <div v-if="customMetadata" class="custom-formula-meta" data-testid="custom-formula-meta">
              <span>已识别 {{ customMetadata.parameters.length }} 个参数</span>
              <span>{{ customMetadata.signals.length }} 个输出</span>
              <span>至少 {{ customMetadata.minimum_bars }} 根 K 线</span>
            </div>
            <div v-if="customMetadata?.parameters.length" class="parameter-grid">
              <div v-for="parameter in customMetadata.parameters" :key="parameter.name">
                <label :for="`formula-param-${parameter.name}`">{{ parameter.name }}</label>
                <input :id="`formula-param-${parameter.name}`" v-model.number="form.formulaParameters[parameter.name]" :data-testid="`formula-param-${parameter.name}`" type="number" :min="parameter.minimum" :max="parameter.maximum" :step="parameter.step">
              </div>
            </div>
            <p v-if="errors.formulaParameters" class="field-error">{{ errors.formulaParameters }}</p>
          </fieldset>

          <button class="advanced-toggle" data-testid="advanced-toggle" type="button" :aria-expanded="showAdvanced" @click="showAdvanced = !showAdvanced">
            <span>{{ showAdvanced ? '收起高级设置' : '高级设置' }}</span>
            <small>市场范围 · 条件组合 · 并发</small>
          </button>

          <div v-if="showAdvanced" class="advanced-settings" data-testid="advanced-settings">
            <fieldset class="form-section">
              <legend>扫描范围</legend>
              <label for="universe">市场范围</label>
              <select id="universe" v-model="form.universe" data-testid="universe">
                <option v-for="item in metadata?.supported_universe ?? []" :key="item.value" :value="item.value">{{ item.label }}</option>
              </select>
              <template v-if="form.universe === 'custom'">
                <label for="universe-file">股票列表文件 <span class="required">*</span></label>
                <input id="universe-file" v-model="form.universeFile" data-testid="universe-file" type="text" placeholder="每行 SH 600000 或 SZ 000001">
                <p v-if="errors.universeFile" class="field-error">{{ errors.universeFile }}</p>
              </template>
              <div class="scope-config inline-scope-config" data-testid="screen-scope">
                <div class="scope-options"><span>证券类型</span><label v-for="option in INSTRUMENT_TYPE_OPTIONS" :key="option.key"><input type="checkbox" :data-testid="`screen-scope-type-${option.key}`" :checked="form.instrumentTypes.includes(option.key)" @change="toggleInstrumentType(option.key)">{{ option.label }}</label></div>
                <div class="scope-options"><span>板块</span><label v-for="option in BOARD_OPTIONS" :key="option.key"><input type="checkbox" :data-testid="`screen-scope-board-${option.key}`" :checked="form.boards.includes(option.key)" @change="toggleBoard(option.key)">{{ option.label }}</label></div>
                <small class="scope-helper">未勾选表示全部；类型和板块同时选择时取交集。</small>
              </div>
            </fieldset>

            <fieldset class="form-section">
              <legend>条件组合</legend>
              <label for="combine-mode">组合方式</label>
              <select id="combine-mode" v-model="form.combineMode" data-testid="combine-mode">
                <option v-for="mode in metadata?.combine_modes ?? []" :key="mode.value" :value="mode.value">{{ mode.label }}</option>
              </select>
              <template v-if="form.combineMode === 'at_least'">
                <label for="minimum-matches">最少满足条件数 <span class="required">*</span></label>
                <input id="minimum-matches" v-model.number="form.minimumMatches" data-testid="minimum-matches" type="number" min="1" :max="Math.max(form.ruleMode === 'conditions' ? form.conditions.length : form.selectedSignals.length, 1)">
                <p v-if="errors.minimumMatches" class="field-error">{{ errors.minimumMatches }}</p>
              </template>
            </fieldset>

            <fieldset class="form-section compact-fields">
              <div><label for="workers">并发 worker 数</label><input id="workers" v-model.number="form.workers" data-testid="workers" type="number" min="1" max="8"></div>
              <div><label for="period">周期</label><select id="period" v-model="form.period" data-testid="period"><option value="daily">日线</option></select></div>
            </fieldset>
          </div>

          <fieldset v-if="form.ruleMode === 'signals'" class="form-section signal-section">
            <legend>选择信号 <span class="legend-count">{{ form.selectedSignals.length }} selected</span></legend>
            <p v-if="errors.selectedSignals" class="field-error" data-testid="signals-error">{{ errors.selectedSignals }}</p>
            <template v-if="form.mode === 'preset'">
              <div v-if="!metadata" class="skeleton-list" aria-label="正在加载信号"></div>
              <div v-for="indicator in metadata?.indicators ?? []" :key="indicator.id" class="indicator-block">
                <div class="indicator-heading"><strong>{{ indicator.display_name }}</strong><small>建议预热 {{ indicator.recommended_bars }} 根 · 最少 {{ indicator.minimum_bars }} 根</small></div>
                <label v-for="signal in indicator.signals" :key="signal.id" class="signal-option" :for="signal.id">
                  <input :id="signal.id" :data-testid="`signal-${signal.id}`" type="checkbox" :checked="form.selectedSignals.includes(signal.id)" @change="toggleSignal(signal.id, ($event.target as HTMLInputElement).checked)">
                  <span class="fake-checkbox" aria-hidden="true"></span>
                  <span class="signal-copy"><strong>{{ signal.display_name }}</strong><small>{{ signal.description }}</small></span>
                </label>
              </div>
            </template>
            <template v-else>
              <p v-if="!customMetadata" class="helper">先粘贴公式并点击“解析公式”，再选择要参与扫描的输出。</p>
              <div v-for="signal in customMetadata?.signals ?? []" :key="signal.id" class="indicator-block">
                <div class="indicator-heading"><strong>公式输出</strong><small>{{ customMetadata?.minimum_bars }} 根预热</small></div>
                <label class="signal-option" :for="`custom-${signal.id}`">
                  <input :id="`custom-${signal.id}`" :data-testid="`custom-signal-${signal.id}`" type="checkbox" :checked="form.selectedSignals.includes(signal.id)" @change="toggleSignal(signal.id, ($event.target as HTMLInputElement).checked)">
                  <span class="fake-checkbox" aria-hidden="true"></span>
                  <span class="signal-copy"><strong>{{ signal.display_name }}</strong><small>{{ signal.description }}</small></span>
                </label>
              </div>
            </template>
          </fieldset>

          <fieldset v-else class="form-section condition-section" data-testid="condition-builder">
            <legend>指标条件 <span class="legend-count">{{ form.conditions.length }} 条</span></legend>
            <p class="helper">选择指标输出后设置比较关系；上穿/下穿只在最近一根 K 线上判断最近一次穿越。</p>

            <div class="condition-set-toolbar" data-testid="condition-set-toolbar">
              <select v-model="selectedConditionSetId" data-testid="condition-set-select" aria-label="已保存指标组合" @change="applySelectedConditionSet">
                <option value="">选择已保存组合</option>
                <option v-for="conditionSet in savedConditionSets" :key="conditionSet.id" :value="conditionSet.id">{{ conditionSet.name }}</option>
              </select>
              <button class="text-button" type="button" data-testid="delete-condition-set" :disabled="!selectedConditionSetId" @click="deleteSelectedConditionSet">删除组合</button>
            </div>

            <div v-for="(condition, index) in form.conditions" :key="`condition-${index}`" class="condition-row" :data-testid="`condition-row-${index}`">
              <div class="condition-field condition-left">
                <label :for="`condition-left-${index}`">指标输出</label>
                <select :id="`condition-left-${index}`" v-model="condition.left_value" :data-testid="`condition-left-${index}`">
                  <optgroup v-for="group in valueGroups" :key="group.label" :label="group.label">
                    <option v-for="value in group.values" :key="value.id" :value="value.id">{{ valueOptionLabel(value) }}</option>
                  </optgroup>
                </select>
              </div>
              <div class="condition-field condition-operator">
                <label :for="`condition-operator-${index}`">关系</label>
                <select :id="`condition-operator-${index}`" v-model="condition.operator" :data-testid="`condition-operator-${index}`">
                  <option v-for="operator in conditionOperators" :key="operator.value" :value="operator.value">{{ operator.label }}</option>
                </select>
              </div>
              <div class="condition-field condition-right-type">
                <label :for="`condition-right-type-${index}`">右侧类型</label>
                <select :id="`condition-right-type-${index}`" :value="condition.right_value === null ? 'constant' : 'value'" :data-testid="`condition-right-type-${index}`" @change="setConditionRightType(index, ($event.target as HTMLSelectElement).value as 'constant' | 'value')">
                  <option value="constant">固定数值</option>
                  <option value="value">另一个指标</option>
                </select>
              </div>
              <div class="condition-field condition-right">
                <label :for="`condition-right-${index}`">{{ condition.right_value === null ? '数值' : '指标输出' }}</label>
                <input v-if="condition.right_value === null" :id="`condition-constant-${index}`" v-model.number="condition.right_constant" :data-testid="`condition-constant-${index}`" type="number" step="any" placeholder="输入数值">
                <select v-else :id="`condition-right-${index}`" v-model="condition.right_value" :data-testid="`condition-right-value-${index}`">
                  <optgroup v-for="group in valueGroups" :key="group.label" :label="group.label">
                    <option v-for="value in group.values" :key="value.id" :value="value.id">{{ valueOptionLabel(value) }}</option>
                  </optgroup>
                </select>
              </div>
              <button class="condition-remove text-button" type="button" :data-testid="`remove-condition-${index}`" :disabled="form.conditions.length <= 1" :aria-label="`删除第 ${index + 1} 条条件`" @click="removeCondition(index)">删除</button>
              <p v-if="errors[`condition-${index}`]" class="field-error condition-error">{{ errors[`condition-${index}`] }}</p>
            </div>
            <p v-if="errors.conditions" class="field-error" data-testid="conditions-error">{{ errors.conditions }}</p>
            <button class="secondary-button add-condition-button" type="button" data-testid="add-condition" @click="addCondition">＋ 添加条件</button>

            <div class="condition-save-row">
              <input v-model="conditionSetName" data-testid="condition-set-name" type="text" maxlength="64" placeholder="例如：主力进场低位">
              <button class="secondary-button" type="button" data-testid="save-condition-set" @click="saveCurrentConditionSet">保存组合</button>
            </div>
            <p v-if="errors.conditionSetName" class="field-error">{{ errors.conditionSetName }}</p>
          </fieldset>

          <div class="action-stack">
            <button class="primary-button" data-testid="start-scan" type="submit" :disabled="!canSubmit">
              <span v-if="loading" class="spinner" aria-hidden="true"></span>
              {{ loading ? '扫描中…' : '开始扫描' }}
            </button>
            <button class="sync-button" data-testid="sync-market-data" type="button" :disabled="loading || syncLoading" :aria-busy="syncLoading" @click="syncMarketData">
              <span v-if="syncLoading" class="spinner sync-spinner" aria-hidden="true"></span>
              {{ syncLoading ? '同步中…' : '同步最新行情' }}
            </button>
            <p class="sync-helper">先增量导入本地 vipdoc，再从服务器补充最新已完成日线，并按 TDX 证券列表补齐中文名，统一写入本地 DuckDB。</p>
            <div v-if="syncLoading || syncJob" class="sync-progress" data-testid="sync-progress">
              <div class="sync-progress-heading"><span>{{ syncLoading ? '行情同步进度' : '最近一次同步' }}</span><strong>{{ syncProgressPercent }}%</strong></div>
              <div class="progress-track sync-progress-track"><span :style="{ width: `${syncProgressPercent}%` }"></span></div>
              <div class="sync-progress-stats"><span>已处理 <strong data-testid="sync-processed">{{ syncJob?.total_scanned ?? 0 }}</strong> / {{ syncJob?.total_candidates ?? 0 }}</span><span>剩余 <strong data-testid="sync-remaining">{{ syncRemaining }}</strong></span></div>
              <div v-if="syncJob?.result" class="sync-result-stats"><span>写入 {{ syncJob.result.written_bars }} 根</span><span>更新 {{ syncJob.result.updated_files }} 个文件</span><span>无变化 {{ syncJob.result.unchanged_files }} 个文件</span><span :class="{ negative: syncJob.result.errors > 0 }">错误 {{ syncJob.result.errors }}</span></div>
              <p v-if="syncJob?.error" class="field-error">{{ syncJob.error }}</p>
            </div>
          </div>
          <p class="privacy-note">数据在本机读取，页面不会上传行情文件。</p>
        </form>

        <section class="results-panel panel" data-testid="results-panel" aria-live="polite">
          <div class="panel-heading results-heading">
            <div><span class="section-kicker">02 / RESULTS</span><h2>扫描结果</h2></div>
            <div class="export-actions"><button type="button" data-testid="export-json" :disabled="results.length === 0" @click="exportJson">导出 JSON</button><button type="button" data-testid="export-csv" :disabled="results.length === 0" @click="exportCsv">导出 CSV</button></div>
          </div>

          <div class="metrics-strip">
            <div><span>进度</span><strong data-testid="progress-value">{{ progressPercent }}%</strong></div>
            <div><span>已扫描</span><strong data-testid="scanned-count">{{ job?.total_scanned ?? 0 }}<small>/ {{ job?.total_candidates ?? 0 }}</small></strong></div>
            <div><span>命中条件</span><strong class="positive" data-testid="signal-count">{{ resultMeta?.total_signals ?? job?.total_signals ?? 0 }}</strong></div>
            <div><span>失败 / 跳过</span><strong data-testid="error-count">{{ resultMeta?.errors ?? job?.errors ?? 0 }} / {{ resultMeta?.skipped ?? job?.skipped ?? 0 }}</strong></div>
            <div><span>worker</span><strong>{{ job?.worker_count ?? form.workers }}</strong></div>
          </div>
          <div class="progress-track" aria-label="扫描进度"><span :style="{ width: `${progressPercent}%` }"></span></div>

          <div v-if="results.length === 0" class="empty-state" data-testid="empty-results">
            <div class="empty-icon" aria-hidden="true">/</div>
            <h3>{{ loading ? '正在读取本地行情…' : '还没有扫描结果' }}</h3>
            <p>{{ loading ? '完成后会在这里显示命中的股票与指标值。' : '在左侧选择信号并开始一次扫描。' }}</p>
          </div>
          <div v-else class="table-wrap">
            <table data-testid="results-table">
              <caption class="sr-only">公式选股结果</caption>
              <thead><tr><th>市场</th><th>代码</th><th>中文名</th><th>信号日期</th><th>最新收盘</th><th>命中条件</th><th>指标值</th></tr></thead>
              <tbody>
                <tr v-for="result in results" :key="`${result.market}-${result.code}`">
                  <td><span class="market-pill" :class="result.market.toLowerCase()">{{ result.market }}</span></td>
                  <td><strong class="code">{{ result.code }}</strong><small v-if="result.instrument_type" class="muted-code">{{ result.instrument_type }}</small></td>
                  <td class="instrument-name">{{ result.name || '未同步名称' }}</td>
                  <td class="muted-code">{{ result.signal_date }}</td>
                  <td><strong>{{ result.last_close.toFixed(2) }}</strong></td>
                  <td><div class="signal-tags"><span v-for="signal in result.matched_signals" :key="signal" class="signal-tag">{{ resultLabel(signal) }}</span><small>{{ result.match_count }} 条</small></div></td>
                  <td><div class="value-list"><span v-for="(value, name) in result.indicator_values" :key="name"><b>{{ resultValueLabel(name) }}</b> {{ value === null ? '—' : value.toFixed(2) }}</span></div></td>
                </tr>
              </tbody>
            </table>
          </div>
          <div v-if="resultMeta && resultMeta.errors > 0" class="failure-summary" data-testid="failure-summary">
            <strong>失败摘要</strong><span v-for="(count, reason) in resultMeta.failure_reasons" :key="reason">{{ reason }} · {{ count }}</span>
          </div>
        </section>
      </div>
    </main>
  </div>
</template>
