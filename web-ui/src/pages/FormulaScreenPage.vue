<script setup lang="ts">
import PageGuide from '../components/PageGuide.vue'
import { computed, onMounted, reactive, ref, watch } from 'vue'

import { fetchLocalChart } from '../api/marketData'
import { fetchActiveJobs } from '../api/jobs'
import MarketCandlestickChart from '../components/MarketCandlestickChart.vue'
import TermHint from '../components/TermHint.vue'
import { createJob, createSyncJob, fetchMetadata, fetchScreenHistory, FormulaScreenApiError, getJob, getResults, getScreenHistory, getSyncJob, parseFormula } from '../api/formulaScreen'
import type { BasketCurve, CombineMode, ConditionOperator, CustomFormulaMetadata, FormulaScreenMetadata, InstrumentBoard, InstrumentType, JobState, LocalMarketChart, MarketSyncJobState, ResultsMeta, SavedConditionSet, ScreenCondition, ScreenFormState, ScreenHistorySummary, ScreenPresetSummary, ScreenResult, StockGroup, StockGroupMember, StockGroupPerformance, StockGroupsSummary, StockGroupSummary, RuleMode, Universe, ValueDefinition } from '../types'
import {
  buildPresetPayload,
  conditionSetToPayload,
  presetLoadMessage,
  presetToFormState,
  researchToFormState,
} from '../utils/screenPresets'
import {
  createScreenPreset,
  deleteScreenPreset,
  fetchScreenPresets,
  getScreenPreset,
  updateScreenPreset,
} from '../api/screenPresets'
import {
  buildScanPayload,
  CONDITION_OPERATORS_UI,
  DEFAULT_PRESET_SIGNALS,
  describeScreenForm,
  filterKnownSignals,
  loadSavedConditionSets,
  loadSavedForm,
  resultsToCsv,
  saveForm,
  signalDisplayName,
  validateScreenForm,
  valueOptionLabel,
} from '../utils/formulaScreen'
import { buildChartGeometry, formatChartValue } from '../utils/chart'
import {
  createStockGroup,
  deleteStockGroup,
  fetchStockGroupPerformance,
  fetchStockGroups,
  fetchStockGroupsSummary,
  getStockGroup,
  removeStockGroupMembers,
  renameStockGroup,
} from '../api/stockGroups'
import { formatChinaTimestamp } from '../utils/time'
import { savePortfolioBacktestHandoff } from '../utils/portfolioBacktestHandoff'

const INSTRUMENT_TYPE_OPTIONS: { key: InstrumentType; label: string }[] = [
  { key: 'stock', label: '股票' },
]

const BOARD_OPTIONS: { key: InstrumentBoard; label: string }[] = [
  { key: 'main', label: '主板' },
  { key: 'star', label: '科创板' },
  { key: 'chinext', label: '创业板' },
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
  rankingValue: savedForm.rankingValue ?? '',
  rankingOrder: savedForm.rankingOrder ?? 'desc',
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
const screenPresets = ref<ScreenPresetSummary[]>([])
const selectedPresetId = ref('')
const presetName = ref('')
const presetBusy = ref(false)
// 旧的「指标组合」存在浏览器本地、只存条件。页面不再提供保存入口，但读出来
// 让用户一键转成方案——数据不能凭空丢掉。
const legacyConditionSets = ref<SavedConditionSet[]>(loadSavedConditionSets())
const historyItems = ref<ScreenHistorySummary[]>([])
const historyLoading = ref(false)
const expandedResultKey = ref<string | null>(null)
const resultCharts = reactive<Record<string, LocalMarketChart>>({})
const resultChartLoading = ref<string | null>(null)
const resultChartErrors = reactive<Record<string, string>>({})
const primaryIndicatorSelections = reactive<Record<string, string[]>>({})

const RESULT_CHART_MA_KEYS = ['ma5', 'ma10', 'ma20', 'ma60']

const progressPercent = computed(() => Math.round((job.value?.progress ?? 0) * 100))
const syncProgressPercent = computed(() => Math.round((syncJob.value?.progress ?? 0) * 100))
const syncRemaining = computed(() => Math.max(
  (syncJob.value?.total_candidates ?? 0) - (syncJob.value?.total_scanned ?? 0),
  0,
))
const presetSignals = computed(() => (
  metadata.value?.indicators.flatMap((indicator) => indicator.signals) ?? []
))
const signalIndicators = computed(() => (
  metadata.value?.indicators.filter((indicator) => indicator.signals.length) ?? []
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
const rankableValueGroups = computed(() => valueGroups.value
  .map((group) => ({
    label: group.label,
    values: group.values.filter((value) => value.rankable === true),
  }))
  .filter((group) => group.values.length > 0))
const rankableValues = computed(() => rankableValueGroups.value.flatMap((group) => group.values))
const displayedRankingValue = computed(() => (
  resultMeta.value
    ? resultMeta.value.ranking_value ?? ''
    : form.rankingValue ?? ''
))
const displayedRankingOrder = computed(() => (
  resultMeta.value?.rank_order ?? (form.rankingOrder === 'asc' ? 'asc' : 'desc')
))
const conditionOperators = computed(() => metadata.value?.condition_operators?.length
  ? metadata.value.condition_operators
  : CONDITION_OPERATORS_UI)
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

function normalizeRankingToAvailableValues(): boolean {
  if (!form.rankingValue) return false
  if (form.mode === 'preset' && !metadata.value) return false
  if (form.mode === 'custom' && customMetadata.value === null) return false
  if (rankableValues.value.some((value) => value.id === form.rankingValue)) return false
  form.rankingValue = ''
  return true
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
  normalizeRankingToAvailableValues()
}

function setRuleMode(ruleMode: RuleMode): void {
  const enteringConditions = ruleMode === 'conditions' && form.ruleMode !== 'conditions'
  form.ruleMode = ruleMode
  if (ruleMode === 'conditions' && form.conditions.length === 0) {
    form.conditions = [emptyCondition(availableValues.value[0]?.id)]
  }
  if (enteringConditions) {
    form.combineMode = 'all'
    form.minimumMatches = null
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

function resultKey(result: ScreenResult): string {
  return `${result.market}-${result.code}`
}

function indicatorOptions(result: ScreenResult): string[] {
  return Object.keys(result.indicator_values)
}

function formulaIdsForResult(result: ScreenResult): string[] {
  void result
  return ['indicator_two', 'indicator_three', 'indicator_one']
}

function selectedIndicatorIds(result: ScreenResult): string[] {
  const key = resultKey(result)
  const options = indicatorOptions(result)
  const selected = primaryIndicatorSelections[key] ?? options.slice(0, 3)
  return selected.filter((id) => options.includes(id)).slice(0, 3)
}

function setPrimaryIndicator(result: ScreenResult, index: number, value: string): void {
  const key = resultKey(result)
  const next = [...selectedIndicatorIds(result)]
  next[index] = value
  primaryIndicatorSelections[key] = next.filter((id, current, all) => id && all.indexOf(id) === current)
}

function formatIndicatorValue(value: number | null | undefined): string {
  return value === null || value === undefined || !Number.isFinite(value) ? '—' : value.toFixed(2)
}

function formatHistoryDate(value: string): string {
  return formatChinaTimestamp(value)
}

function applyResultPayload(nextResults: ScreenResult[], nextMeta: ResultsMeta): void {
  results.value = nextResults
  resultMeta.value = nextMeta
  if (nextMeta.ranking_value !== undefined) {
    form.rankingValue = nextMeta.ranking_value ?? ''
    form.rankingOrder = nextMeta.rank_order === 'asc' ? 'asc' : 'desc'
  }
  expandedResultKey.value = null
  for (const result of nextResults) {
    const key = resultKey(result)
    if (!primaryIndicatorSelections[key]) {
      primaryIndicatorSelections[key] = indicatorOptions(result).slice(0, 3)
    }
  }
}

async function refreshScreenHistory(): Promise<void> {
  historyLoading.value = true
  try {
    historyItems.value = await fetchScreenHistory()
  } catch {
    historyItems.value = []
  } finally {
    historyLoading.value = false
  }
}

function historyJobState(item: ScreenHistorySummary): JobState {
  return {
    job_id: item.history_id,
    status: 'completed',
    progress: 1,
    total_candidates: item.total_candidates,
    total_scanned: item.total_scanned,
    total_signals: item.total_signals,
    errors: item.errors,
    skipped: item.skipped,
    error: null,
    worker_count: null,
  }
}

async function loadScreenHistory(historyId: string): Promise<void> {
  try {
    const record = await getScreenHistory(historyId)
    applyResultPayload(record.results, record.meta)
    job.value = historyJobState(historyItems.value.find((item) => item.history_id === historyId) ?? {
      history_id: record.history_id,
      created_at: record.created_at,
      total_candidates: record.meta.total_candidates,
      total_scanned: record.meta.total_scanned,
      total_signals: record.meta.total_signals,
      errors: record.meta.errors,
      skipped: record.meta.skipped,
      result_count: record.results.length,
    })
    message.value = `已加载历史筛选：${formatHistoryDate(record.created_at)}`
  } catch (error) {
    message.value = apiMessage(error, '历史筛选加载失败')
  }
}

async function toggleResultDetails(result: ScreenResult): Promise<void> {
  const key = resultKey(result)
  if (expandedResultKey.value === key) {
    expandedResultKey.value = null
    return
  }
  expandedResultKey.value = key
  if (resultCharts[key]) return
  resultChartLoading.value = key
  delete resultChartErrors[key]
  try {
    resultCharts[key] = await fetchLocalChart({
      market: result.market as 'SH' | 'SZ',
      code: result.code,
      period: 'daily',
      formula_ids: formulaIdsForResult(result),
    })
  } catch (error) {
    resultChartErrors[key] = apiMessage(error, '标的行情加载失败')
  } finally {
    if (resultChartLoading.value === key) resultChartLoading.value = null
  }
}

async function refreshScreenPresets(): Promise<void> {
  try {
    screenPresets.value = await fetchScreenPresets()
  } catch {
    // 列表拉不到不影响扫描，只在方案区留空即可。
  }
}

/** 加载结果。stale 是「引用了当前不存在的指标 / 信号」的项数，必须往上传。 */
interface PresetLoadOutcome {
  name: string
  stale: number
}

async function loadSelectedPreset(): Promise<PresetLoadOutcome | null> {
  const presetId = selectedPresetId.value
  if (!presetId) return null
  presetBusy.value = true
  try {
    const preset = await getScreenPreset(presetId)
    const result = presetToFormState(preset.payload, {
      valueIds: availableValues.value.map((value) => value.id),
      signalIds: presetSignals.value.map((signal) => signal.id),
    })
    const state = result.state
    form.mode = state.mode
    form.ruleMode = state.ruleMode
    form.selectedSignals = [...state.selectedSignals]
    form.conditions = state.conditions.length > 0 ? state.conditions : [emptyCondition()]
    form.combineMode = state.combineMode
    form.minimumMatches = state.minimumMatches
    form.universe = state.universe
    form.universeFile = state.universeFile
    form.instrumentTypes = [...state.instrumentTypes]
    form.boards = [...state.boards]
    form.workers = state.workers
    form.period = state.period
    form.formulaText = state.formulaText
    form.formulaParameters = { ...state.formulaParameters }
    form.rankingValue = state.rankingValue ?? ''
    form.rankingOrder = state.rankingOrder ?? 'desc'
    const requestedRankingValue = form.rankingValue
    customMetadata.value = null
    parsedFormulaText.value = ''
    errors.value = {}
    if (form.mode === 'custom' && form.formulaText.trim()) await parseCustomFormula()
    else normalizeConditionsToAvailableValues()
    normalizeRankingToAvailableValues()
    const rankingUnavailable = Boolean(requestedRankingValue && !form.rankingValue)
    presetName.value = preset.name
    message.value = presetLoadMessage(preset.name, result)
      + (rankingUnavailable ? ' 排序指标已不可用，已清除。' : '')
    return { name: preset.name, stale: result.staleConditions + result.staleSignals }
  } catch (error) {
    message.value = apiMessage(error, '方案加载失败')
    return null
  } finally {
    presetBusy.value = false
  }
}

/** 一键选股：加载方案后立刻扫描，省掉「加载 → 滚到底部点扫描」两步。 */
async function quickScreen(): Promise<void> {
  const loaded = await loadSelectedPreset()
  if (!loaded) return
  const scanned = await submit()
  if (loaded.stale > 0) {
    // 失效引用比「扫描完成」重要：结果里的条件已经被悄悄换过，不能只报喜。
    message.value = `已按方案「${loaded.name}」扫描，但有 ${loaded.stale} 项引用了当前不存在的指标或信号、已被替换，请先核对条件。`
  } else if (scanned) {
    message.value = `已按方案「${loaded.name}」扫描完成。`
  }
}

async function savePresetAsNew(): Promise<void> {
  const name = presetName.value.trim()
  if (!name) {
    errors.value = { ...errors.value, presetName: '请输入方案名称。' }
    return
  }
  errors.value = { ...errors.value, presetName: '' }
  presetBusy.value = true
  try {
    const created = await createScreenPreset({ name, payload: buildPresetPayload(form) })
    await refreshScreenPresets()
    selectedPresetId.value = created.preset_id
    message.value = created.notes?.length
      ? `已保存方案「${created.name}」，但内容被调整过：${created.notes.join('；')}`
      : `已保存方案「${created.name}」。`
  } catch (error) {
    message.value = apiMessage(error, '方案保存失败')
  } finally {
    presetBusy.value = false
  }
}

async function overwritePreset(): Promise<void> {
  const presetId = selectedPresetId.value
  if (!presetId) return
  presetBusy.value = true
  try {
    const updated = await updateScreenPreset(presetId, { payload: buildPresetPayload(form) })
    await refreshScreenPresets()
    message.value = `已用当前设置覆盖方案「${updated.name}」。`
  } catch (error) {
    message.value = apiMessage(error, '方案覆盖失败')
  } finally {
    presetBusy.value = false
  }
}

async function renamePreset(): Promise<void> {
  const presetId = selectedPresetId.value
  const name = presetName.value.trim()
  if (!presetId) return
  if (!name) {
    errors.value = { ...errors.value, presetName: '请输入新的方案名称。' }
    return
  }
  presetBusy.value = true
  try {
    const updated = await updateScreenPreset(presetId, { name })
    await refreshScreenPresets()
    message.value = `方案已重命名为「${updated.name}」。`
  } catch (error) {
    message.value = apiMessage(error, '方案重命名失败')
  } finally {
    presetBusy.value = false
  }
}

async function deleteSelectedPreset(): Promise<void> {
  const presetId = selectedPresetId.value
  if (!presetId) return
  presetBusy.value = true
  try {
    await deleteScreenPreset(presetId)
    selectedPresetId.value = ''
    presetName.value = ''
    await refreshScreenPresets()
    message.value = '已删除方案。'
  } catch (error) {
    message.value = apiMessage(error, '方案删除失败')
  } finally {
    presetBusy.value = false
  }
}

async function importLegacyConditionSets(): Promise<void> {
  presetBusy.value = true
  let imported = 0
  try {
    for (const set of legacyConditionSets.value) {
      try {
        await createScreenPreset({
          name: set.name.slice(0, 60),
          payload: conditionSetToPayload(set),
        })
        imported += 1
      } catch {
        // 重名或名字非法就跳过这一个，不中断其余的导入。
      }
    }
    await refreshScreenPresets()
    message.value = imported > 0
      ? `已导入 ${imported} 个本地指标组合为筛选方案。`
      : '没有导入任何组合（可能已重名）。'
  } finally {
    presetBusy.value = false
  }
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
    normalizeRankingToAvailableValues()
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
      applyResultPayload(payload.results, payload.meta)
      await refreshScreenHistory()
      message.value = `扫描完成：命中 ${payload.meta.total_signals} 个条件，得到 ${payload.results.length} 只股票。`
      return
    }
    if (state.status === 'failed') {
      throw new FormulaScreenApiError(state.error ?? '扫描任务执行失败。', 500)
    }
    await new Promise((resolve) => window.setTimeout(resolve, 250))
  }
}

async function resumeActiveScreenJob(): Promise<void> {
  try {
    const active = (await fetchActiveJobs()).find((item) => item.description === '公式选股')
    if (!active) return
    job.value = active as unknown as JobState
    loading.value = true
    await pollUntilFinished(active.job_id)
  } catch (error) {
    message.value = apiMessage(error, '扫描任务恢复失败，请检查后端服务和本地行情')
  } finally {
    loading.value = false
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

async function submit(): Promise<boolean> {
  errors.value = validateScreenForm(form, customMetadata.value)
  message.value = ''
  if (Object.values(errors.value).some(Boolean)) return false

  loading.value = true
  results.value = []
  resultMeta.value = null
  expandedResultKey.value = null
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
    return false
  } finally {
    loading.value = false
  }
  return true
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

/**
 * 条件研究搬过来的只有买入侧：选股是「某一天谁满足条件」的一次性扫描，没有
 * 「什么时候卖」这一说，所以卖出规则不带过来——带了也是摆着不用，反而让人以为
 * 它被算进去了。
 */
interface ResearchToScreenPayload {
  signals?: string[]
  conditions?: ScreenCondition[]
  required_conditions?: ScreenCondition[]
  combine_mode?: CombineMode
  minimum_matches?: number | null
  universe?: Universe
  universe_file?: string | null
  boards?: InstrumentBoard[]
  instrument_types?: InstrumentType[]
  mode?: 'preset' | 'custom'
  formula_text?: string | null
  formula_parameters?: Record<string, number>
  ranking_value?: string | null
  rank_order?: 'asc' | 'desc'
  source_label?: '买卖条件研究' | '组合回测'
}

const RESEARCH_TO_SCREEN_KEY = 'indicator-lab.condition-research.to-screen'

async function applyResearchToScreen(): Promise<void> {
  const raw = sessionStorage.getItem(RESEARCH_TO_SCREEN_KEY)
  if (!raw) return
  const notes: string[] = []
  let sourceLabel: '买卖条件研究' | '组合回测' = '买卖条件研究'
  try {
    const payload = JSON.parse(raw) as ResearchToScreenPayload
    if (payload.source_label === '组合回测') sourceLabel = '组合回测'
    const result = researchToFormState(payload, {
      signalIds: presetSignals.value.map((signal) => signal.id),
      valueIds: availableValues.value.map((value) => value.id),
    })
    const state = result.state
    const requestedRankingValue = state.rankingValue ?? ''
    form.mode = state.mode
    form.ruleMode = state.ruleMode
    form.selectedSignals = state.selectedSignals
    form.conditions = state.conditions.length > 0 ? state.conditions : [emptyCondition()]
    form.combineMode = state.combineMode
    form.minimumMatches = state.minimumMatches
    form.universe = state.universe
    form.universeFile = state.universeFile
    form.boards = state.boards
    form.instrumentTypes = state.instrumentTypes
    form.formulaText = state.formulaText
    form.formulaParameters = state.formulaParameters
    form.rankingValue = state.rankingValue ?? ''
    form.rankingOrder = state.rankingOrder ?? 'desc'
    customMetadata.value = null
    parsedFormulaText.value = ''
    errors.value = {}
    normalizeConditionsToAvailableValues()
    if (form.mode === 'custom' && form.formulaText.trim()) await parseCustomFormula()
    normalizeRankingToAvailableValues()
    if (requestedRankingValue && !form.rankingValue) {
      notes.push(`排序指标 ${requestedRankingValue} 当前不可用于跨标的排序，已清除。`)
    } else if (form.rankingValue) {
      const rankingName = rankableValues.value.find(
        (value) => value.id === form.rankingValue,
      )?.display_name ?? form.rankingValue
      notes.push(`排序：${rankingName} · ${form.rankingOrder === 'asc' ? '从小到大' : '从大到小'}`)
    }
    notes.push(...result.notes)
    if (sourceLabel === '组合回测') {
      notes.push('卖出条件、止盈止损、持仓槽位和 ST 过滤不会用于公式选股；这里只带入买入筛选与排序。')
    }
  } catch {
    notes.push('带过来的条件没能还原，已保留原有设置。')
  } finally {
    // 读完立即删：刷新时不该再被套用一次，否则用户改完表单一刷新就被覆盖回去。
    sessionStorage.removeItem(RESEARCH_TO_SCREEN_KEY)
  }
  message.value = notes.length
    ? `已带入${sourceLabel}的买入条件。${notes.join('；')}`
    : `已带入${sourceLabel}的买入条件，确认后直接扫描即可。`
}

/* ---------------- 股票组：把扫出来的票留下来观察 ---------------- */

const stockGroups = ref<StockGroupSummary[]>([])
const selectedGroupId = ref('')
const groupName = ref('')
const groupBusy = ref(false)
const groupError = ref('')
// 展开态的「入组至今」按组缓存：算一次要扫一遍全市场，切走再点回来不该重算。
const groupDetails = ref<Record<string, StockGroupPerformance>>({})
const groupMembers = ref<Record<string, StockGroup>>({})
const handoffGroupId = ref('')
const expandedGroupIds = reactive(new Set<string>())
const loadingGroupId = ref('')
// 折叠态的「近 N 个交易日」：所有组共用同一窗口，一次请求全部拿回。
const groupSummary = ref<StockGroupsSummary | null>(null)
const showAllGroups = ref(false)
// 结果行的勾选。用 Set 而不是数组：一次扫描可能选出几百只，includes 是线性的。
const selectedResultKeys = reactive(new Set<string>())

/** 组数不限，但列表默认只铺最近 20 条，剩下的要显式点开——否则面板会被顶下去。 */
const VISIBLE_GROUP_LIMIT = 20

const visibleGroups = computed(() => (
  showAllGroups.value ? stockGroups.value : stockGroups.value.slice(0, VISIBLE_GROUP_LIMIT)
))

const selectedResultCount = computed(() => (
  results.value.filter((item) => selectedResultKeys.has(resultKey(item))).length
))

interface FoldedStat {
  label: string
  value: number | null
  excess: number | null
  trading_days: number
  complete: boolean
}

/** 折叠态那一行的「近 1 月 / 近 3 月」。窗口标签由后端下发，两边不各写一份。 */
function foldedStats(group: StockGroupSummary): FoldedStat[] {
  const entry = groupSummary.value?.groups.find((item) => item.group_id === group.group_id)
  if (!entry) return []
  return entry.windows.map((item) => ({
    label: groupSummary.value?.labels[String(item.window)] ?? `近 ${item.window} 个交易日`,
    value: item.group_return,
    excess: item.excess_return,
    trading_days: item.trading_days,
    complete: item.complete,
  }))
}

function detailOf(group: StockGroupSummary): StockGroupPerformance | null {
  return groupDetails.value[group.group_id] ?? null
}

/** 详情可能是 null，模板里没法用 v-if 顺便拿到非空引用——用单元素数组起个名字。 */
function detailList(group: StockGroupSummary): StockGroupPerformance[] {
  const detail = detailOf(group)
  return detail ? [detail] : []
}

function memberList(group: StockGroupSummary): StockGroup[] {
  const members = groupMembers.value[group.group_id]
  return members ? [members] : []
}

/** 两条曲线共用同一纵轴：各画各的会让「跑赢了多少」看起来比实际多。 */
function chartOf(group: StockGroupSummary) {
  const payload = detailOf(group)
  if (!payload?.group_curve || !payload.market_curve) return null
  const toPoints = (curve: BasketCurve) => curve.totals.map((total, index) => ({
    x: index,
    y: (total - 1) * 100,
  }))
  return buildChartGeometry(
    [toPoints(payload.group_curve), toPoints(payload.market_curve)],
    { includeZero: true, height: 200 },
  )
}

/** 同上：给「这一组的两条曲线」起个模板里能直接用的名字。 */
function chartList(group: StockGroupSummary) {
  const chart = chartOf(group)
  return chart ? [chart] : []
}
/** 入组日期是这组唯一的锚点，按东八区显示——后端存的是 UTC。 */
function groupDay(createdAt: string): string {
  return formatChinaTimestamp(createdAt).slice(0, 10)
}

function toggleResultPick(result: ScreenResult, checked: boolean): void {
  const key = resultKey(result)
  if (checked) selectedResultKeys.add(key)
  else selectedResultKeys.delete(key)
}

async function refreshStockGroups(): Promise<void> {
  try {
    stockGroups.value = await fetchStockGroups()
  } catch {
    // 列表拉不到不影响扫描，只在面板里留空。
  }
}

async function refreshGroupSummaries(): Promise<void> {
  try {
    groupSummary.value = await fetchStockGroupsSummary()
  } catch {
    // 折叠态的阶段收益读不到就空着，组名/只数这些静态信息照样能看。
    groupSummary.value = null
  }
}

async function saveResultsAsGroup(useAll: boolean): Promise<void> {
  const picked = useAll
    ? results.value
    : results.value.filter((item) => selectedResultKeys.has(resultKey(item)))
  if (picked.length === 0) {
    groupError.value = useAll ? '还没有扫描结果可以保存。' : '请先勾选要保存的标的。'
    return
  }
  groupBusy.value = true
  groupError.value = ''
  try {
    const created = await createStockGroup({
      name: groupName.value.trim() || defaultGroupName(),
      items: picked.map((item) => ({
        market: item.market,
        code: item.code,
        name: item.name ?? null,
      })),
      source: 'screen',
      // 存快照而不是记 history_id：回测/扫描历史每种类型只留最近 20 条，等组还在、
      // 历史已被淘汰时，只有组里这句人话还能回答「这批票是按什么选出来的」。
      criteria: {
        summary: describeScreenForm(form, availableValues.value, metadata.value, customMetadata.value),
        payload: buildScanPayload(form) as unknown as Record<string, unknown>,
      },
    })
    groupName.value = ''
    selectedResultKeys.clear()
    await Promise.all([refreshStockGroups(), refreshGroupSummaries()])
    selectedGroupId.value = created.group_id
    expandedGroupIds.add(created.group_id)
    await loadGroupPerformance(created.group_id)
  } catch (error) {
    groupError.value = apiMessage(error, '保存失败')
  } finally {
    groupBusy.value = false
  }
}

function validFormulaParameters(value: unknown): Record<string, number> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {}
  return Object.fromEntries(
    Object.entries(value).filter((entry): entry is [string, number] => (
      typeof entry[1] === 'number' && Number.isFinite(entry[1])
    )),
  )
}

function handoffStockPool(
  codes: string[],
  sourceLabel: string,
  input: Record<string, unknown> = {},
  signalDate: number | null = null,
): void {
  const normalizedCodes = [...new Set(codes.map((code) => code.trim()).filter(Boolean))]
  if (!normalizedCodes.length) {
    message.value = '这组没有可回测的标的。'
    return
  }

  const formulaText = typeof input.formula_text === 'string'
    ? input.formula_text.trim()
    : ''
  const rankingValue = typeof input.ranking_value === 'string' && input.ranking_value.trim()
    ? input.ranking_value.trim()
    : null
  const handoff = savePortfolioBacktestHandoff({
    kind: 'stock-pool',
    source: 'formula-screen',
    source_label: sourceLabel,
    codes: normalizedCodes,
    ranking_value: rankingValue,
    rank_order: input.rank_order === 'asc' ? 'asc' : 'desc',
    mode: formulaText ? 'custom' : 'preset',
    formula_text: formulaText || null,
    formula_parameters: validFormulaParameters(input.formula_parameters),
    signal_date: signalDate,
  })
  if (!handoff) {
    message.value = '浏览器暂时无法保存这次跨页配置，请检查隐私模式或存储权限。'
    return
  }
  if (import.meta.env.MODE !== 'test') window.location.assign('/portfolio-backtest')
}

function handoffScreenResults(useSelected: boolean): void {
  const picked = useSelected
    ? results.value.filter((item) => selectedResultKeys.has(resultKey(item)))
    : results.value
  const label = useSelected ? `公式选股勾选结果（${picked.length} 只）` : `公式选股整批结果（${picked.length} 只）`
  const scanConfig: Record<string, unknown> = {
    ranking_value: resultMeta.value?.ranking_value ?? null,
    rank_order: resultMeta.value?.rank_order ?? displayedRankingOrder.value,
  }
  if (form.mode === 'custom' && form.formulaText.trim() === parsedFormulaText.value) {
    scanConfig.formula_text = form.formulaText.trim()
    scanConfig.formula_parameters = { ...form.formulaParameters }
  }
  const signalDates = [...new Set(picked.map((item) => item.signal_date))]
  handoffStockPool(
    picked.map((item) => `${item.market.toUpperCase()}:${item.code}`),
    label,
    scanConfig,
    signalDates.length === 1 ? signalDates[0] : null,
  )
}

async function handoffStockGroup(group: StockGroupSummary): Promise<void> {
  if (handoffGroupId.value) return
  handoffGroupId.value = group.group_id
  groupError.value = ''
  try {
    const detail = groupMembers.value[group.group_id] ?? await getStockGroup(group.group_id)
    groupMembers.value = { ...groupMembers.value, [group.group_id]: detail }
    const criteria = group.criteria?.payload ?? {}
    handoffStockPool(
      detail.items.map((item) => `${item.market.toUpperCase()}:${item.code}`),
      `股票组「${group.name}」`,
      criteria,
    )
  } catch (error) {
    groupError.value = apiMessage(error, '读取股票组失败')
  } finally {
    handoffGroupId.value = ''
  }
}

function defaultGroupName(): string {
  const stamp = new Date().toISOString().slice(0, 10)
  return `${stamp} 选股结果`
}

/**
 * 展开才去算「入组至今」。
 *
 * 这一份每组都要单独扫一遍全市场（几秒），而折叠态要的那两个数已经在
 * ``groupSummary`` 里了——先铺开信息、按需再算重活，是这页唯一能扛住「存很多组」
 * 的做法。
 */
async function toggleGroup(group: StockGroupSummary): Promise<void> {
  const groupId = group.group_id
  if (expandedGroupIds.has(groupId)) {
    expandedGroupIds.delete(groupId)
    return
  }
  expandedGroupIds.add(groupId)
  selectedGroupId.value = groupId
  if (groupDetails.value[groupId]) return
  await loadGroupPerformance(groupId)
}

async function loadGroupPerformance(groupId: string): Promise<void> {
  selectedGroupId.value = groupId
  groupError.value = ''
  if (!groupId) return
  loadingGroupId.value = groupId
  try {
    // 表现和成员分两次取：成员随时可编辑，而表现要扫全市场（几秒），不该被一次
    // 编辑来回拖着重算。
    const [performance, detail] = await Promise.all([
      fetchStockGroupPerformance(groupId),
      getStockGroup(groupId),
    ])
    groupDetails.value = { ...groupDetails.value, [groupId]: performance }
    groupMembers.value = { ...groupMembers.value, [groupId]: detail }
  } catch (error) {
    groupError.value = apiMessage(error, '表现读取失败')
  } finally {
    loadingGroupId.value = ''
  }
}

async function renameGroup(group: StockGroupSummary): Promise<void> {
  const next = window.prompt('新的分组名称', group.name)?.trim()
  if (!next || next === group.name) return
  groupBusy.value = true
  try {
    await renameStockGroup(group.group_id, next)
    await refreshStockGroups()
    const cached = groupMembers.value[group.group_id]
    if (cached) groupMembers.value[group.group_id] = { ...cached, name: next }
  } catch (error) {
    groupError.value = apiMessage(error, '改名失败')
  } finally {
    groupBusy.value = false
  }
}

async function removeGroupMember(group: StockGroupSummary, member: StockGroupMember): Promise<void> {
  const groupId = group.group_id
  groupBusy.value = true
  try {
    await removeStockGroupMembers(groupId, [{ market: member.market, code: member.code }])
    // 折叠态那两个窗口收益来自 groupSummary，不是 stockGroups——只刷列表的话，
    // 移出之后这一行还是按「包括被移走那只」算出来的老数字。
    await Promise.all([refreshStockGroups(), refreshGroupSummaries()])
    await loadGroupPerformance(groupId)
  } catch (error) {
    groupError.value = apiMessage(error, '移除失败')
  } finally {
    groupBusy.value = false
  }
}

async function deleteGroup(group: StockGroupSummary): Promise<void> {
  if (!window.confirm(`删除分组「${group.name}」？它包含的 ${group.item_count} 只标的会一起丢掉。`)) {
    return
  }
  groupBusy.value = true
  try {
    await deleteStockGroup(group.group_id)
    expandedGroupIds.delete(group.group_id)
    if (selectedGroupId.value === group.group_id) selectedGroupId.value = ''
    const details = { ...groupDetails.value }
    delete details[group.group_id]
    groupDetails.value = details
    await refreshStockGroups()
  } catch (error) {
    groupError.value = apiMessage(error, '删除失败')
  } finally {
    groupBusy.value = false
  }
}

/** 涨红跌绿（A 股习惯）。零附近不着色——0.00% 标成红色会让人以为赚了。 */
function toneClass(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value) || value === 0) return ''
  return value > 0 ? 'tone-up' : 'tone-down'
}

function formatPercent(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  return `${value >= 0 ? '+' : ''}${(value * 100).toFixed(2)}%`
}

function formatPoints(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  return `${value >= 0 ? '+' : ''}${(value * 100).toFixed(2)} 个百分点`
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
    normalizeRankingToAvailableValues()
    await refreshScreenHistory()
    await resumeActiveScreenJob()
    await refreshScreenPresets()
    // 条件研究的桥接放在最后：它可能解析自定义公式并改写信号，而前面几步要把
    // 元数据先加载完才能判断哪些信号还存在。
    // 组列表（轻量）和折叠态的阶段收益（一次请求算完全部组）同时发：折叠态要
    // 显示的东西这才齐，而两者都不会去算「入组至今」那份重活。
    await Promise.all([refreshStockGroups(), refreshGroupSummaries()])
    await applyResearchToScreen()
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
                <a class="nav-link" href="/portfolio-backtest">组合回测</a>
        <a class="nav-link" href="/strategy-lab">策略实验室</a>
        <a class="nav-link" href="/condition-research">买卖条件研究</a>
        <a class="nav-link" href="/individual-research">个股研究</a>
        <a class="nav-link" href="/strategy-pool">全市场策略池</a>
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
        <div>
          <p class="eyebrow">FORMULA SCREEN / DAILY</p>
          <h1>公式选股</h1>
          <p class="intro-copy">把通达信条件拆成可组合的信号，扫描已完成的日线 K 线。</p>
        </div>
        <div class="intro-note"><span class="note-label">扫描范围</span><strong>DuckDB 全量行情</strong><small>信号筛选和回测统一读取本地数据仓库</small></div>
      </section>

      <PageGuide page-key="formula-screen" />

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

          <fieldset class="form-section preset-section" data-testid="screen-preset">
            <legend>筛选方案 <span class="legend-count">{{ screenPresets.length }} 个</span></legend>
            <div class="preset-toolbar">
              <select v-model="selectedPresetId" data-testid="preset-select" aria-label="已保存筛选方案">
                <option value="">选择已保存方案</option>
                <option v-for="preset in screenPresets" :key="preset.preset_id" :value="preset.preset_id">{{ preset.name }}</option>
              </select>
              <button class="secondary-button" type="button" data-testid="scan-preset" :disabled="!selectedPresetId || presetBusy || loading || !canSubmit" @click="quickScreen">
                <span v-if="loading" class="spinner" aria-hidden="true"></span>
                一键选股
              </button>
              <button class="text-button" type="button" data-testid="load-preset" :disabled="!selectedPresetId || presetBusy" @click="loadSelectedPreset">只加载设置</button>
              <button class="text-button" type="button" data-testid="delete-preset" :disabled="!selectedPresetId || presetBusy" @click="deleteSelectedPreset">删除</button>
            </div>
            <div class="preset-save-row">
              <input v-model="presetName" data-testid="preset-name" type="text" maxlength="60" placeholder="方案名称，例如：主板主力进场">
              <button class="secondary-button" type="button" data-testid="save-preset" :disabled="presetBusy" @click="savePresetAsNew">保存为新方案</button>
              <button class="secondary-button" type="button" data-testid="overwrite-preset" :disabled="!selectedPresetId || presetBusy" @click="overwritePreset">覆盖当前</button>
              <button class="secondary-button" type="button" data-testid="rename-preset" :disabled="!selectedPresetId || presetBusy" @click="renamePreset">重命名</button>
            </div>
            <p v-if="errors.presetName" class="field-error" data-testid="preset-name-error">{{ errors.presetName }}</p>
            <p class="helper">方案存的是<b>整套设置</b>（信号 / 条件 / 市场范围 / 板块 / 并发），存在服务端数据库里，换浏览器也在。「一键选股」= 载入这套设置并立刻扫描；「只加载设置」只还原表单，不扫描。</p>
            <p v-if="legacyConditionSets.length" class="preset-import-row" data-testid="preset-import">
              还有 {{ legacyConditionSets.length }} 个保存在浏览器本地的旧「指标组合」（只存条件、清缓存会丢）。
              <button class="text-button" type="button" data-testid="import-legacy-sets" :disabled="presetBusy" @click="importLegacyConditionSets">导入为筛选方案</button>
            </p>
          </fieldset>

          <fieldset v-if="form.mode === 'custom'" class="form-section custom-formula-section">
            <legend>粘贴通达信公式</legend>
            <textarea v-model="form.formulaText" data-testid="custom-formula" rows="8" spellcheck="false" placeholder="例如：N:=5; SIGNAL:CROSS(C,REF(C,N));"></textarea>
            <button class="secondary-button" data-testid="parse-formula" type="button" :disabled="customParseLoading" @click="parseCustomFormula">
              {{ customParseLoading ? '解析中…' : '解析公式' }}
            </button>
            <p class="helper">支持常用数组函数：REF、SMA、EMA、MA、STD、AVEDEV、SQRT、LLV、HHV、BARSLAST、COUNT、CROSS、IF、MAX、MIN、ABS。参数需用 <code>名称:=数值</code> 显式声明。</p>
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

            <fieldset v-if="form.ruleMode === 'signals'" class="form-section">
              <legend>条件组合</legend>
              <label for="combine-mode">组合方式</label>
              <select id="combine-mode" v-model="form.combineMode" data-testid="combine-mode">
                <option v-for="mode in metadata?.combine_modes ?? []" :key="mode.value" :value="mode.value">{{ mode.label }}</option>
              </select>
              <template v-if="form.combineMode === 'at_least'">
                <label for="minimum-matches">最少满足条件数 <span class="required">*</span></label>
                <input id="minimum-matches" v-model.number="form.minimumMatches" data-testid="minimum-matches" type="number" min="1" :max="Math.max(form.selectedSignals.length, 1)">
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
              <div v-for="indicator in signalIndicators" :key="indicator.id" class="indicator-block">
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

            <div class="condition-combine-control" data-testid="condition-combine">
              <div class="condition-combine-field">
                <label for="condition-combine-mode">条件关系</label>
                <select id="condition-combine-mode" v-model="form.combineMode" data-testid="condition-combine-mode">
                  <option value="all">全部满足（AND）</option>
                  <option value="any">任一满足（OR）</option>
                  <option value="at_least">至少满足 N 个</option>
                </select>
              </div>
              <div v-if="form.combineMode === 'at_least'" class="condition-combine-field">
                <label for="condition-minimum-matches">至少满足数量</label>
                <input id="condition-minimum-matches" v-model.number="form.minimumMatches" data-testid="condition-minimum-matches" type="number" min="1" :max="Math.max(form.conditions.length, 1)">
              </div>
              <small class="condition-combine-helper">当前条件需要{{ form.combineMode === 'all' ? '全部同时满足' : form.combineMode === 'any' ? '任一满足即可' : `至少满足 ${form.minimumMatches ?? 1} 条` }}。</small>
            </div>
            <p v-if="errors.minimumMatches" class="field-error">{{ errors.minimumMatches }}</p>

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

          </fieldset>

          <fieldset class="form-section screen-ranking" data-testid="screen-ranking">
            <legend>结果排序（可选）</legend>
            <div class="compact-fields">
              <div>
                <label for="screen-ranking-value">排序指标</label>
                <select id="screen-ranking-value" v-model="form.rankingValue" data-testid="screen-ranking-value">
                  <option value="">不排序（保持扫描顺序）</option>
                  <optgroup v-for="group in rankableValueGroups" :key="group.label" :label="group.label">
                    <option v-for="value in group.values" :key="value.id" :value="value.id">{{ value.display_name }}</option>
                  </optgroup>
                </select>
              </div>
              <div>
                <label for="screen-ranking-order">排序方向</label>
                <select id="screen-ranking-order" v-model="form.rankingOrder" data-testid="screen-ranking-order" :disabled="!form.rankingValue">
                  <option value="desc">从高到低</option>
                  <option value="asc">从低到高</option>
                </select>
              </div>
            </div>
            <p class="helper">排序只改变命中结果的先后顺序，不改变筛选条件；按每只股票最新可用日线的指标值排列。设置对下一次扫描生效，历史记录会保留当次顺序；买卖条件研究带过来的排序建议也可直接使用。</p>
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
            <p class="sync-helper">先在「本地行情」页面刷新证券列表并补充最新日线；沪深股票统一写入本地 DuckDB。</p>
            <div v-if="syncLoading || syncJob" class="sync-progress" data-testid="sync-progress">
              <div class="sync-progress-heading"><span>{{ syncLoading ? '行情同步进度' : '最近一次同步' }}</span><strong>{{ syncProgressPercent }}%</strong></div>
              <div class="progress-track sync-progress-track"><span :style="{ width: `${syncProgressPercent}%` }"></span></div>
              <div class="sync-progress-stats"><span>已处理 <strong data-testid="sync-processed">{{ syncJob?.total_scanned ?? 0 }}</strong> / {{ syncJob?.total_candidates ?? 0 }}</span><span>剩余 <strong data-testid="sync-remaining">{{ syncRemaining }}</strong></span></div>
              <div v-if="syncJob?.result" class="sync-result-stats"><span>写入 {{ syncJob.result.written_bars }} 根</span><span>更新 {{ syncJob.result.updated_files }} 个文件</span><span>无变化 {{ syncJob.result.unchanged_files }} 个文件</span><span :class="{ negative: (syncJob.result.errors ?? 0) > 0 }">错误 {{ syncJob.result.errors ?? 0 }}</span></div>
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

          <div class="screen-history" data-testid="screen-history">
            <div class="screen-history-heading"><div><strong>历史筛选</strong><small>自动保留最近 10 次</small></div><span v-if="historyLoading">读取中…</span></div>
            <div v-if="historyItems.length" class="screen-history-list">
              <button v-for="item in historyItems" :key="item.history_id" class="screen-history-item" :class="{ active: job?.job_id === item.history_id }" :data-testid="`screen-history-item-${item.history_id}`" type="button" @click="loadScreenHistory(item.history_id)">
                <span>{{ formatHistoryDate(item.created_at) }}</span><small>{{ item.result_count }} 条结果 · 命中 {{ item.total_signals }}</small>
              </button>
            </div>
            <p v-else class="screen-history-empty">暂无历史筛选记录</p>
          </div>

          <div class="metrics-strip">
            <div><span>进度</span><strong data-testid="progress-value">{{ progressPercent }}%</strong></div>
            <div><span>已扫描</span><strong data-testid="scanned-count">{{ job?.total_scanned ?? 0 }}<small>/ {{ job?.total_candidates ?? 0 }}</small></strong></div>
            <div><span>命中条件</span><strong class="positive" data-testid="signal-count">{{ resultMeta?.total_signals ?? job?.total_signals ?? 0 }}</strong></div>
            <div><span>失败 / 跳过</span><strong data-testid="error-count">{{ resultMeta?.errors ?? job?.errors ?? 0 }} / {{ resultMeta?.skipped ?? job?.skipped ?? 0 }}</strong></div>
            <div><span>worker</span><strong>{{ job?.worker_count ?? form.workers }}</strong></div>
          </div>
          <div class="progress-track" aria-label="扫描进度"><span :style="{ width: `${progressPercent}%` }"></span></div>
          <p v-if="displayedRankingValue" class="helper" data-testid="screen-results-order">
            结果按 {{ resultValueLabel(displayedRankingValue) }} {{ displayedRankingOrder === 'asc' ? '从低到高' : '从高到低' }} 排列。
          </p>
          <div v-if="results.length" class="screen-transfer-actions" data-testid="screen-backtest-actions">
            <button
              class="secondary-button"
              type="button"
              data-testid="screen-selected-to-backtest"
              :disabled="selectedResultCount === 0"
              @click="handoffScreenResults(true)"
            >把勾选的 {{ selectedResultCount }} 只带入组合回测</button>
            <button
              class="secondary-button"
              type="button"
              data-testid="screen-all-to-backtest"
              @click="handoffScreenResults(false)"
            >把全部 {{ results.length }} 只带入组合回测</button>
          </div>
          <p v-if="results.length" class="helper">
            带入的是这次选股的固定代码清单，回测区间内都会作为候选范围；它不会重建历史上每天的选股名单。
          </p>

          <div v-if="results.length === 0" class="empty-state" data-testid="empty-results">
            <div class="empty-icon" aria-hidden="true">/</div>
            <h3>{{ loading ? '正在读取本地行情…' : '还没有扫描结果' }}</h3>
            <p>{{ loading ? '完成后会在这里显示命中的股票与指标值。' : '在左侧选择信号并开始一次扫描。' }}</p>
          </div>
          <div v-else class="table-wrap">
              <table data-testid="results-table">
                <caption class="sr-only">公式选股结果</caption>
              <thead><tr><th class="pick-column">存组</th><th v-if="displayedRankingValue">排名</th><th>市场</th><th>代码</th><th>中文名</th><th>信号日期</th><th>最新收盘</th><th>命中条件</th><th>指标值</th></tr></thead>
              <tbody>
                <template v-for="(result, resultIndex) in results" :key="`${result.market}-${result.code}`">
                <tr class="screen-result-row" :class="{ expanded: expandedResultKey === resultKey(result), picked: selectedResultKeys.has(resultKey(result)) }" role="button" tabindex="0" :aria-expanded="expandedResultKey === resultKey(result)" @click="toggleResultDetails(result)" @keydown.enter.prevent="toggleResultDetails(result)">
                  <td class="pick-column">
                    <input
                      type="checkbox"
                      :checked="selectedResultKeys.has(resultKey(result))"
                      :data-testid="`result-pick-${result.market}-${result.code}`"
                      :aria-label="`把 ${result.code} 加入待保存的分组`"
                      @click.stop
                      @change="toggleResultPick(result, ($event.target as HTMLInputElement).checked)"
                    >
                  </td>
                  <td v-if="displayedRankingValue" class="muted-code">{{ resultIndex + 1 }}</td>
                  <td><span class="market-pill" :class="result.market.toLowerCase()">{{ result.market }}</span></td>
                  <td><strong class="code">{{ result.code }}</strong><small v-if="result.instrument_type" class="muted-code">{{ result.instrument_type }}</small></td>
                  <td class="instrument-name">{{ result.name || '未同步名称' }}</td>
                  <td class="muted-code">{{ result.signal_date }}</td>
                  <td><strong>{{ result.last_close.toFixed(2) }}</strong></td>
                  <td><div class="signal-tags"><span v-for="signal in result.matched_signals" :key="signal" class="signal-tag">{{ resultLabel(signal) }}</span><small>{{ result.match_count }} 条</small></div></td>
                  <td><div class="value-list"><span v-for="(value, name) in result.indicator_values" :key="name"><b>{{ resultValueLabel(name) }}</b> {{ value === null ? '—' : value.toFixed(2) }}</span></div></td>
                </tr>
                <tr v-if="expandedResultKey === resultKey(result)" :key="`${result.market}-${result.code}-detail`" class="screen-result-detail-row">
                  <td :colspan="displayedRankingValue ? 9 : 8">
                    <div class="screen-result-detail" :data-testid="`result-detail-${result.market}-${result.code}`">
                      <div class="screen-result-detail-heading"><div><strong>{{ result.name || `${result.market} ${result.code}` }}</strong><small>点击结果行收起 · 本地日线行情</small></div><span>{{ result.signal_date }}</span></div>
                      <div v-if="resultChartLoading === resultKey(result)" class="chart-loading">正在读取 K 线…</div>
                      <div v-else-if="resultChartErrors[resultKey(result)]" class="inline-empty">{{ resultChartErrors[resultKey(result)] }}</div>
                      <MarketCandlestickChart v-else-if="resultCharts[resultKey(result)]" :candles="resultCharts[resultKey(result)].candles" :ma-keys="RESULT_CHART_MA_KEYS" :formula-panels="resultCharts[resultKey(result)].formula_panels ?? []" :show-volume="false" period="daily" :show-macd="false" :show-rsi="false" />
                      <div v-if="indicatorOptions(result).length" class="result-primary-indicators">
                        <div class="section-heading"><div><span class="section-kicker">MAIN INDICATORS</span><h3>主要指标</h3></div><small>可自定义最多 3 个</small></div>
                        <div class="result-primary-grid">
                          <div v-for="(indicatorId, index) in selectedIndicatorIds(result)" :key="`${resultKey(result)}-${index}`" class="result-primary-card">
                            <select :value="indicatorId" :data-testid="`result-indicator-${result.market}-${result.code}-${index}`" @click.stop @change="setPrimaryIndicator(result, index, ($event.target as HTMLSelectElement).value)">
                              <option v-for="option in indicatorOptions(result)" :key="option" :value="option">{{ resultValueLabel(option) }}</option>
                            </select>
                            <strong>{{ formatIndicatorValue(result.indicator_values[indicatorId]) }}</strong>
                          </div>
                        </div>
                      </div>
                    </div>
                  </td>
                </tr>
                </template>
              </tbody>
            </table>
          </div>
          <div v-if="resultMeta && resultMeta.errors > 0" class="failure-summary" data-testid="failure-summary">
            <strong>失败摘要</strong><span v-for="(count, reason) in resultMeta.failure_reasons" :key="reason">{{ reason }} · {{ count }}</span>
          </div>
        </section>

        <section class="stock-groups-panel panel" data-testid="stock-groups-panel">
          <div class="panel-heading">
            <div>
              <span class="section-kicker">KEEP &amp; WATCH</span>
              <h2>股票组</h2>
            </div>
            <span class="required-hint">{{ stockGroups.length }} 组</span>
          </div>
          <p class="helper">
            把扫出来的票留成一组，之后能回来看它们从入组那天算起涨了多少、有没有跑赢全市场。
            组只是观察，不模拟买卖。
          </p>

          <div class="group-save-row">
            <input
              v-model="groupName"
              class="group-name-input"
              type="text"
              maxlength="60"
              placeholder="组名（留空用默认名）"
              data-testid="group-name"
            >
            <button
              class="primary-button"
              type="button"
              data-testid="save-picked-group"
              :disabled="groupBusy || selectedResultCount === 0"
              @click="saveResultsAsGroup(false)"
            >保存勾选的 {{ selectedResultCount }} 只</button>
            <button
              class="secondary-button"
              type="button"
              data-testid="save-all-group"
              :disabled="groupBusy || results.length === 0"
              @click="saveResultsAsGroup(true)"
            >整批保存（{{ results.length }} 只）</button>
          </div>

          <p v-if="groupError" class="inline-empty" data-testid="group-error">{{ groupError }}</p>

          <ul v-if="visibleGroups.length" class="group-list" data-testid="group-list">
            <li
              v-for="group in visibleGroups"
              :key="group.group_id"
              class="group-card"
              :class="{ expanded: expandedGroupIds.has(group.group_id) }"
              :data-testid="`group-item-${group.group_id}`"
            >
              <div class="group-head">
                <button
                  type="button"
                  class="group-pick"
                  :class="{ active: expandedGroupIds.has(group.group_id) }"
                  :data-testid="`group-pick-${group.group_id}`"
                  @click="toggleGroup(group)"
                >
                  <span class="group-title">
                    <strong>{{ group.name }}</strong>
                    <small>
                      {{ group.item_count }} 只 · 入组 {{ groupDay(group.created_at) }}
                      · {{ group.source_label }}
                    </small>
                  </span>
                  <span v-if="foldedStats(group).length" class="group-fold-stats">
                    <span v-for="stat in foldedStats(group)" :key="stat.label" class="group-fold-stat">
                      <em>{{ stat.label }}</em>
                      <b :class="toneClass(stat.value)">{{ formatPercent(stat.value) }}</b>
                      <i :class="toneClass(stat.excess)">{{ formatPoints(stat.excess) }}</i>
                      <small v-if="!stat.complete">仅 {{ stat.trading_days }} 个交易日</small>
                    </span>
                  </span>
                </button>
                <button
                  type="button"
                  class="text-button"
                  :data-testid="`group-backtest-${group.group_id}`"
                  :disabled="handoffGroupId !== ''"
                  @click="handoffStockGroup(group)"
                >{{ handoffGroupId === group.group_id ? '载入中…' : '带入组合回测' }}</button>
                <button
                  type="button"
                  class="text-button"
                  :data-testid="`group-rename-${group.group_id}`"
                  :disabled="groupBusy"
                  @click="renameGroup(group)"
                >改名</button>
                <button
                  type="button"
                  class="text-button"
                  :data-testid="`group-delete-${group.group_id}`"
                  :disabled="groupBusy"
                  @click="deleteGroup(group)"
                >删除</button>
              </div>

              <p
                v-if="group.criteria"
                class="group-criteria"
                :data-testid="`group-criteria-${group.group_id}`"
              >筛选条件：{{ group.criteria.summary }}</p>

              <div v-if="expandedGroupIds.has(group.group_id)" class="group-detail">
                <div v-if="loadingGroupId === group.group_id" class="chart-loading" data-testid="group-loading">
                  正在读取这一组的表现…
                </div>
                <!-- v-for 只是给「这一组的详情」起个名字：详情可能是 null，模板里
                     没法用 v-if 顺便拿到非空变量的引用。 -->
                <template v-for="detail in detailList(group)" :key="`detail-${group.group_id}`">
                  <div data-testid="group-performance">
                    <p class="group-note">
                      下面是<strong>入组至今</strong>的口径（每组起点不同，组之间不可比）；
                      上面折叠行里的「近 1 月 / 近 3 月」是所有组同一窗口，那个才可以横向比。
                    </p>
                    <div class="group-stat-row">
                      <div class="group-stat">
                        <small>本组</small>
                        <strong :class="toneClass(detail.group_curve?.performance.total_return ?? null)" data-testid="group-return">
                          {{ formatPercent(detail.group_curve?.performance.total_return ?? null) }}
                        </strong>
                        <span v-if="detail.group_curve" class="group-stat-note">
                          {{ detail.group_curve.instrument_count }} 只计入
                        </span>
                      </div>
                      <div class="group-stat">
                        <small>全市场同期</small>
                        <strong :class="toneClass(detail.market_curve?.performance.total_return ?? null)" data-testid="market-return">
                          {{ formatPercent(detail.market_curve?.performance.total_return ?? null) }}
                        </strong>
                        <span v-if="detail.market_curve" class="group-stat-note">
                          {{ detail.market_curve.instrument_count }} 只计入
                        </span>
                      </div>
                      <div class="group-stat">
                        <small>超额<TermHint term="excessReturn" /></small>
                        <strong :class="toneClass(detail.excess_return)" data-testid="excess-return">
                          {{ formatPoints(detail.excess_return) }}
                        </strong>
                        <span class="group-stat-note">差值，不是倍数</span>
                      </div>
                      <div class="group-stat">
                        <small>观察区间</small>
                        <strong>{{ detail.start_date ?? '—' }} ~ {{ detail.end_date ?? '—' }}</strong>
                        <span class="group-stat-note">{{ detail.trading_days }} 个交易日</span>
                      </div>
                    </div>

                    <p v-for="note in detail.notes" :key="note" class="group-note">{{ note }}</p>

                    <template v-for="chart in chartList(group)" :key="`chart-${group.group_id}`">
                      <svg
                        class="group-chart"
                        :viewBox="`0 0 ${chart.width} ${chart.height}`"
                        role="img"
                        aria-label="本组与全市场的累计涨跌幅曲线"
                        data-testid="group-chart"
                      >
                        <line
                          v-if="chart.zeroY !== null"
                          :x1="chart.ticks.length ? 46 : 46"
                          :x2="chart.width - 12"
                          :y1="chart.zeroY"
                          :y2="chart.zeroY"
                          class="group-chart-zero"
                        />
                        <polyline
                          v-for="(line, index) in chart.lines"
                          :key="index"
                          :points="line"
                          :class="index === 0 ? 'group-chart-own' : 'group-chart-market'"
                        />
                        <text
                          v-for="tick in chart.ticks"
                          :key="tick.y"
                          x="4"
                          :y="tick.y + 3"
                          class="group-chart-tick"
                        >{{ formatChartValue(tick.value, 1) }}%</text>
                      </svg>
                    </template>
                    <div class="group-legend">
                      <span class="group-legend-own">本组等权买入持有</span>
                      <span class="group-legend-market">全市场等权买入持有</span>
                    </div>

                    <div v-if="detail.spread" class="group-spread">
                      组内分布：{{ detail.spread.counted }} 只计入，
                      涨 {{ detail.spread.advanced }} 只 / 跌 {{ detail.spread.declined }} 只，
                      中位数 {{ formatPercent(detail.spread.median_return) }}。
                      <template v-if="detail.spread.best">
                        最好 {{ detail.spread.best.code }}（{{ formatPercent(detail.spread.best.return) }}），
                        最差 {{ detail.spread.worst?.code }}（{{ formatPercent(detail.spread.worst?.return) }}）。
                      </template>
                    </div>

                    <div v-if="detail.excluded.length" class="group-excluded">
                      <strong>未计入 {{ detail.excluded.length }} 只</strong>
                      <span v-for="item in detail.excluded" :key="`${item.market}-${item.code}`">
                        {{ item.code }}<template v-if="item.name"> · {{ item.name }}</template>
                      </span>
                    </div>
                  </div>
                </template>

                <details
                  v-for="members in memberList(group)"
                  :key="`members-${group.group_id}`"
                  class="group-members"
                >
                  <summary>组内标的（{{ members.items.length }} 只）</summary>
                  <div class="table-wrap">
                    <table data-testid="group-members-table">
                      <thead><tr><th>市场</th><th>代码</th><th>中文名</th><th></th></tr></thead>
                      <tbody>
                        <tr v-for="member in members.items" :key="`${member.market}-${member.code}`">
                          <td>{{ member.market }}</td>
                          <td><strong class="code">{{ member.code }}</strong></td>
                          <td>{{ member.name || '—' }}</td>
                          <td>
                            <button
                              class="text-button"
                              type="button"
                              :data-testid="`group-member-remove-${member.code}`"
                              :disabled="groupBusy"
                              @click="removeGroupMember(group, member)"
                            >移出</button>
                          </td>
                        </tr>
                      </tbody>
                    </table>
                  </div>
                </details>
              </div>
            </li>
          </ul>
          <p v-else class="group-empty" data-testid="group-empty">还没有分组。勾选结果行或直接整批保存，就能留一组下来观察。</p>

          <div v-if="stockGroups.length > VISIBLE_GROUP_LIMIT" class="group-more">
            <button
              class="text-button"
              type="button"
              data-testid="group-toggle-all"
              @click="showAllGroups = !showAllGroups"
            >{{ showAllGroups ? `只看最近 ${VISIBLE_GROUP_LIMIT} 组` : `显示全部 ${stockGroups.length} 组` }}</button>
            <span class="group-stat-note">折叠着也能改组名、看筛选条件</span>
          </div>
        </section>
      </div>
    </main>
  </div>
</template>
