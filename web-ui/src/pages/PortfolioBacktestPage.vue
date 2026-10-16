<script setup lang="ts">
import PageIntro from '../components/PageIntro.vue'
import { computed, onMounted, reactive, ref, watch } from 'vue'

import {
  cancelPortfolioBacktest,
  createPortfolioBacktest,
  getPortfolioBacktest,
  getPortfolioBacktestResults,
} from '../api/portfolioBacktest'
import { fetchMetadata, parseFormula } from '../api/formulaScreen'
import { adoptActiveJob, useActiveJobResume } from '../composables/useActiveJobResume'
import { describeError } from '../api/client'
import { pollUntil } from '../composables/usePolling'
import { usePortfolioBacktestHistory } from '../composables/usePortfolioBacktestHistory'
import { usePortfolioHandoff } from '../composables/usePortfolioHandoff'
import { usePortfolioRiskOptimization } from '../composables/usePortfolioRiskOptimization'
import { usePortfolioStrategyLibrary } from '../composables/usePortfolioStrategyLibrary'
import { consumePortfolioBacktestHandoff } from '../utils/portfolioBacktestHandoff'
import BacktestHistoryPanel from '../components/BacktestHistoryPanel.vue'
import DateRangeSelector from '../components/DateRangeSelector.vue'
import PortfolioResultPanel from '../components/PortfolioResultPanel.vue'
import RiskOptimizationHistoryDetail from '../components/RiskOptimizationHistoryDetail.vue'
import RuleGroupEditor from '../components/RuleGroupEditor.vue'
import type {
  CustomFormulaMetadata,
  DateRangeSelection,
  FormulaScreenMetadata,
  InstrumentBoard,
  InstrumentType,
  AppliedDiscoveryCandidate,
  PortfolioBacktestJobState,
  PortfolioBacktestPayload,
  PortfolioFormState,
  PortfolioBacktestResult,
  PortfolioReplayComparison,
  RuleGroupPayload,
  SignalDefinition,
  ValueDefinition,
} from '../types'
import { numericRuleForSignal, valueOptionLabel } from '../utils/formulaScreen'
import { formatChinaTimestamp } from '../utils/time'
import AppTopbar from '../components/AppTopbar.vue'
import { formatDuration, formatNumber, formatPercent } from '../utils/format'

const DISCOVERY_APPLY_KEY = 'aquest.strategy-discovery.apply'
const DISCOVERY_APPLY_META_KEY = 'aquest.strategy-discovery.apply-meta'
// 条件研究页的桥接，键独立于策略实验室，两者不会互相覆盖。
const RESEARCH_APPLY_KEY = 'aquest.condition-research.apply'
const PORTFOLIO_BACKTEST_LIMITATIONS = [
  '成交、持仓与整手按真实不复权价模拟；指标按分红送转事件校正后的价格计算，除权跳空只影响指标口径、不影响成交价。',
  '仅模拟单一做多组合和固定持仓槽位，不模拟实盘下单、融资融券或多策略资金隔离。',
  '成交按日线价格及配置的费用、滑点模拟；不模拟涨跌停撮合、停牌成交限制或成交量冲击。',
  '这是历史模拟，不代表未来收益，也不是投资建议。',
]

const INSTRUMENT_TYPE_OPTIONS: { key: InstrumentType; label: string }[] = [
  { key: 'stock', label: '股票' },
]

const BOARD_OPTIONS: { key: InstrumentBoard; label: string }[] = [
  { key: 'main', label: '主板' },
  { key: 'star', label: '科创板' },
  { key: 'chinext', label: '创业板' },
]

const metadata = ref<FormulaScreenMetadata | null>(null)
const customMetadata = ref<CustomFormulaMetadata | null>(null)
const parsedFormulaText = ref('')
const form = reactive<PortfolioFormState>({
  mode: 'preset',
  buyRule: { signals: [], conditions: [], combine_mode: 'any', minimum_matches: null },
  universe: 'all',
  universeFile: '',
  codesText: '',
  instrumentTypes: [] as InstrumentType[],
  boards: [] as InstrumentBoard[],
  rankingValue: '',
  rankOrder: 'desc',
  maxPositions: 5,
  rebalanceFrequency: 'daily',
  execution: 'next_open',
  formulaText: '',
  formulaParameters: {},
  sellRule: { signals: [], conditions: [], combine_mode: 'any', minimum_matches: null },
  stopLossEnabled: true,
  stopLossMode: 'pct',
  stopLossPct: 8,
  stopLossAtrMultiplier: 2,
  stopLossCooldownDays: 0,
  takeProfitEnabled: false,
  takeProfitMode: 'pct',
  takeProfitPct: 20,
  takeProfitAtrMultiplier: 3,
  takeProfitCooldownDays: 0,
  trailingStopEnabled: false,
  trailingStopMode: 'pct',
  trailingStopPct: 5,
  trailingStopAtrMultiplier: 2,
  trailingStopCooldownDays: 0,
  maxHoldDaysEnabled: false,
  maxHoldDays: 60,
  atrPeriod: 14,
  dateMode: 'recent5',
  startDate: '',
  endDate: '',
  initialCash: 1_000_000,
  commission: 0.0001,
  minCommission: 0.1,
  stampTax: 0.001,
  slippage: 0,
  fitnessRankWeight: 0,
  fitnessMinTrades: 5,
  fitnessMaxDrawdown: 30,
  positionMode: 'full',
  fixedSize: 1000,
  excludeSt: true,
})
const errors = ref<Record<string, string>>({})
const message = ref('')
const loading = ref(false)
const customParseLoading = ref(false)
const customParseError = ref('')
const showAdvanced = ref(false)
const showPnlAnalysis = ref(false)
const job = ref<PortfolioBacktestJobState | null>(null)
const result = ref<PortfolioBacktestResult | null>(null)
const resultPriceBasis = computed(() => result.value?.price_basis ?? 'raw')
const resultLimitations = computed(() => (
  result.value?.limitations?.length ? result.value.limitations : PORTFOLIO_BACKTEST_LIMITATIONS
))
const appliedDiscoveryCandidate = ref<AppliedDiscoveryCandidate | null>(null)
const replayComparison = ref<PortfolioReplayComparison | null>(null)
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
  && !riskOptimizationLoading.value
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

function firstSignal(suffix: string, fallback = ''): string {
  return availableSignals.value.find((signal) => signal.id.endsWith(suffix))?.id
    ?? availableSignals.value[0]?.id
    ?? fallback
}

function firstValue(fallback = ''): string {
  return availableValues.value[0]?.id ?? fallback
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
  const sellSignal = firstSignal('end_zone')
  form.buyRule = ruleForSignal(buySignal)
  form.sellRule = ruleForSignal(sellSignal)
  form.rankingValue = firstValue()
}

function setMode(mode: PortfolioFormState['mode']): void {
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
      parsed.parameters.map((parameter) => [
        parameter.name,
        previousParameters[parameter.name] ?? parameter.default,
      ]),
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
    customParseError.value = describeError(error, { fallback: '公式解析失败，请检查语法和函数是否受支持。' })
  } finally {
    customParseLoading.value = false
  }
}

/** Split the 指定标的 textarea into individual codes (comma, whitespace or newline). */
function parsedCodes(): string[] {
  return form.codesText
    .split(/[\s,，;；]+/)
    .map((item) => item.trim())
    .filter((item) => item.length > 0)
}

function validate(): Record<string, string> {
  const next: Record<string, string> = {}
  const buyRule = form.buyRule
  const sellRule = form.sellRule
  if (!buyRule.signals.length && !buyRule.conditions.length) next.buyRule = '请至少设置一个买入条件。'
  if (!form.rankingValue) next.rankingValue = '请选择用于排序的指标输出。'
  if (form.universe === 'custom' && !form.universeFile.trim()) next.universeFile = '请输入自定义股票列表文件。'
  if (form.universe === 'codes' && parsedCodes().length === 0) next.codesText = '请填写至少一个证券代码。'
  if (form.positionMode === 'fixed') {
    if (!Number.isInteger(form.fixedSize) || form.fixedSize < 100 || form.fixedSize > 100_000_000) {
      next.fixedSize = '固定买入股数必须是 100 到 100000000 的整数。'
    } else if (form.positionMode === 'fixed' && form.fixedSize % 100 !== 0) {
      // 后端会把非整手向下取整，这里先挡住，免得填 150 实际按 100 跑还毫无提示。
      next.fixedSize = '固定买入股数必须是 100 的整数倍（A 股一手 100 股）。'
    }
  }
  if (form.mode === 'custom') {
    if (!form.formulaText.trim()) next.formulaText = '请输入通达信公式。'
    else if (!customMetadata.value) next.formulaText = '请先解析公式，再配置组合回测。'
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
  if (form.dateMode === 'custom') {
    if (!form.startDate) next.startDate = '请选择开始日期。'
    if (!form.endDate) next.endDate = '请选择结束日期。'
  }
  if (form.startDate && form.endDate && form.startDate > form.endDate) next.endDate = '结束日期不能早于开始日期。'
  if (!Number.isFinite(form.maxPositions) || !Number.isInteger(form.maxPositions) || form.maxPositions < 1 || form.maxPositions > 100) {
    next.maxPositions = '持仓槽位必须是 1 到 100 的整数。'
  }
  if (!sellRule.signals.length && !sellRule.conditions.length && !sellRule.required_conditions?.length && !form.stopLossEnabled && !form.takeProfitEnabled && !form.trailingStopEnabled && !form.maxHoldDaysEnabled) {
    next.sellRules = '至少配置一个卖出规则。'
  }
  const riskFields: Array<{
    enabled: boolean
    mode: 'pct' | 'atr'
    pct: number
    atr: number
    cooldown: number
    pctKey: string
    atrKey: string
    cooldownKey: string
    label: string
  }> = [
    { enabled: form.stopLossEnabled, mode: form.stopLossMode, pct: form.stopLossPct, atr: form.stopLossAtrMultiplier, cooldown: form.stopLossCooldownDays, pctKey: 'stopLossPct', atrKey: 'stopLossAtrMultiplier', cooldownKey: 'stopLossCooldownDays', label: '止损' },
    { enabled: form.takeProfitEnabled, mode: form.takeProfitMode, pct: form.takeProfitPct, atr: form.takeProfitAtrMultiplier, cooldown: form.takeProfitCooldownDays, pctKey: 'takeProfitPct', atrKey: 'takeProfitAtrMultiplier', cooldownKey: 'takeProfitCooldownDays', label: '止盈' },
    { enabled: form.trailingStopEnabled, mode: form.trailingStopMode, pct: form.trailingStopPct, atr: form.trailingStopAtrMultiplier, cooldown: form.trailingStopCooldownDays, pctKey: 'trailingStopPct', atrKey: 'trailingStopAtrMultiplier', cooldownKey: 'trailingStopCooldownDays', label: '移动止盈' },
  ]
  for (const field of riskFields) {
    if (!field.enabled) continue
    if (field.mode === 'pct' && (!Number.isFinite(field.pct) || field.pct <= 0)) next[field.pctKey] = `${field.label}比例必须大于 0。`
    if (field.mode === 'atr' && (!Number.isFinite(field.atr) || field.atr <= 0)) next[field.atrKey] = `${field.label} ATR 倍数必须大于 0。`
    if (!Number.isInteger(field.cooldown) || field.cooldown < 0 || field.cooldown > 10000) next[field.cooldownKey] = `${field.label}冷却天数必须是 0 到 10000 的整数。`
  }
  if (!Number.isFinite(form.initialCash) || form.initialCash <= 0) next.initialCash = '初始资金必须大于 0。'
  if (form.fitnessRankWeight > 0) {
    if (!Number.isInteger(form.fitnessMinTrades) || form.fitnessMinTrades < 1) next.fitnessMinTrades = '适配性最少成交笔数必须是正整数。'
    if (!Number.isFinite(form.fitnessMaxDrawdown) || form.fitnessMaxDrawdown < 0 || form.fitnessMaxDrawdown > 100) next.fitnessMaxDrawdown = '适配性最大回撤必须在 0 到 100% 之间。'
  }
  return next
}

function buildPayload(): PortfolioBacktestPayload {
  const buyRule = form.buyRule
  const sellRule = form.sellRule
  return {
    universe: form.universe,
    universe_file: form.universe === 'custom' ? form.universeFile.trim() : null,
    codes: form.universe === 'codes' ? parsedCodes() : null,
    ...(form.instrumentTypes.length > 0 ? { instrument_types: [...form.instrumentTypes] } : {}),
    ...(form.boards.length > 0 ? { boards: [...form.boards] } : {}),
    selected_signals: [],
    combine_mode: 'all',
    minimum_matches: null,
    buy_rule: buyRule,
    ranking_value: form.rankingValue,
    rank_order: form.rankOrder,
    max_positions: form.maxPositions,
    rebalance_frequency: form.rebalanceFrequency,
    formula_text: form.mode === 'custom' ? form.formulaText.trim() : null,
    formula_parameters: form.mode === 'custom' ? { ...form.formulaParameters } : {},
    date_mode: form.dateMode,
    sell_signal: null,
    stop_loss_mode: form.stopLossEnabled ? form.stopLossMode : 'pct',
    stop_loss_pct: form.stopLossEnabled && form.stopLossMode === 'pct' ? form.stopLossPct / 100 : null,
    stop_loss_atr_multiplier: form.stopLossEnabled && form.stopLossMode === 'atr' ? form.stopLossAtrMultiplier : null,
    stop_loss_cooldown_days: form.stopLossEnabled ? form.stopLossCooldownDays : 0,
    take_profit_mode: form.takeProfitEnabled ? form.takeProfitMode : 'pct',
    take_profit_pct: form.takeProfitEnabled && form.takeProfitMode === 'pct' ? form.takeProfitPct / 100 : null,
    take_profit_atr_multiplier: form.takeProfitEnabled && form.takeProfitMode === 'atr' ? form.takeProfitAtrMultiplier : null,
    take_profit_cooldown_days: form.takeProfitEnabled ? form.takeProfitCooldownDays : 0,
    trailing_stop_mode: form.trailingStopEnabled ? form.trailingStopMode : 'pct',
    trailing_stop_pct: form.trailingStopEnabled && form.trailingStopMode === 'pct' ? form.trailingStopPct / 100 : null,
    trailing_stop_atr_multiplier: form.trailingStopEnabled && form.trailingStopMode === 'atr' ? form.trailingStopAtrMultiplier : null,
    trailing_stop_cooldown_days: form.trailingStopEnabled ? form.trailingStopCooldownDays : 0,
    max_hold_days: form.maxHoldDaysEnabled ? form.maxHoldDays : null,
    atr_period: form.atrPeriod,
    sell_value: null,
    sell_value_operator: null,
    sell_value_threshold: null,
    compare_left_value: null,
    compare_operator: null,
    compare_right_value: null,
    sell_rule: sellRule.signals.length || sellRule.conditions.length || sellRule.required_conditions?.length ? sellRule : null,
    start_date: form.startDate || null,
    end_date: form.endDate || null,
    initial_cash: form.initialCash,
    commission: form.commission,
    min_commission: form.minCommission,
    stamp_tax: form.stampTax,
    slippage: form.slippage,
    execution: form.execution,
    fitness_rank_weight: form.fitnessRankWeight,
    fitness_min_trades: form.fitnessMinTrades,
    fitness_max_drawdown: form.fitnessMaxDrawdown / 100,
    position_mode: form.positionMode,
    fixed_size: form.positionMode === 'fixed' ? form.fixedSize : null,
    exclude_st: form.excludeSt,
  }
}

function apiMessage(error: unknown): string {
  return describeError(error, { fallback: '组合回测失败，请检查本地 DuckDB 数据和配置后重试。' })
}

// 跨页桥接与回填（策略实验室/条件研究/波段/股池 + 反向带到公式选股）
// 在 composables/usePortfolioHandoff；每个来源的 payload 翻译规则集中在那里。
const {
  applyStoredCandidate,
  applyStoredResearch,
  applyPortfolioBacktestHandoff,
  readAppliedDiscoveryMeta,
  compareAppliedCandidate,
  applyBuyRuleToFormulaScreen,
} = usePortfolioHandoff({
  form,
  errors,
  message,
  availableValues,
  parseCustomFormula,
  customMetadata,
  appliedDiscoveryCandidate,
  replayComparison,
})

async function submit(): Promise<void> {
  errors.value = validate()
  message.value = ''
  if (Object.values(errors.value).some(Boolean)) return
  loading.value = true
  result.value = null
  try {
    const created = await createPortfolioBacktest(buildPayload())
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
    await pollPortfolioBacktest(created.job_id)
  } catch (error) {
    message.value = apiMessage(error)
  } finally {
    loading.value = false
  }
}

async function pollPortfolioBacktest(jobId: string): Promise<void> {
  const state = await pollUntil(
    async () => {
      const current = await getPortfolioBacktest(jobId)
      job.value = current
      return current
    },
    {
      intervalMs: 220,
      until: (current) =>
        current.status === 'completed' || current.status === 'failed' || current.status === 'cancelled',
    },
  )
  if (state.status === 'completed') {
    result.value = await getPortfolioBacktestResults(jobId)
    await compareAppliedCandidate(result.value)
    message.value = `组合回测完成：${result.value.trades.length} 笔成交，区间 ${result.value.start_date} 至 ${result.value.end_date}。`
    await loadHistory()
    return
  }
  message.value = state.error
    ?? (state.status === 'cancelled' ? '组合回测已取消。' : '组合回测任务失败，请检查配置和服务状态后重试。')
}

async function cancelRunningPortfolioBacktest(): Promise<void> {
  const active = job.value
  if (
    !active
    || active.cancel_requested
    || !['queued', 'running'].includes(active.status)
  ) return
  try {
    const response = await cancelPortfolioBacktest(active.job_id)
    if (response.cancel_requested) {
      job.value = { ...active, cancel_requested: true }
      message.value = '已请求取消组合回测，正在等待当前计算批次结束。'
    } else if (response.status === 'completed') {
      message.value = '组合回测已完成。'
    } else if (response.status === 'cancelled') {
      message.value = '组合回测已取消。'
    }
  } catch (error) {
    message.value = apiMessage(error)
  }
}

function downloadResult(): void {
  if (!result.value) return
  const exportPayload = {
    ...result.value,
    price_basis: resultPriceBasis.value,
    limitations: resultLimitations.value,
  }
  const url = URL.createObjectURL(new Blob([JSON.stringify(exportPayload, null, 2)], { type: 'application/json' }))
  const link = document.createElement('a')
  link.href = url
  link.download = 'portfolio-backtest.json'
  link.click()
  window.setTimeout(() => URL.revokeObjectURL(url), 0)
}

// 策略库（保存/载入/重命名/删除）在 composables/usePortfolioStrategyLibrary；
// 保存要打包当前表单，载入要走表单回填，都在这里接上。
const {
  strategies,
  newStrategyName,
  strategyLoading,
  strategySaving,
  strategyBusyId,
  renamingId,
  renameDraft,
  pendingDeleteId,
  loadStrategies,
  saveCurrentStrategy,
  applyStrategy,
  startRename,
  cancelRename,
  commitRename,
  confirmDelete,
  strategyRange,
} = usePortfolioStrategyLibrary({
  result,
  message,
  describeError: apiMessage,
  buildPayload,
  applyStoredResearch,
})

// 回测历史（列表/还原/详情/删除）在 composables/usePortfolioBacktestHistory；
// 还原要深度写表单并复用通用回填，详情载入后由页面展示结果。
const {
  history,
  historyLoading,
  historyError,
  loadHistory,
  applyHistoryRequest,
  loadHistoryDetail,
  removeHistory,
} = usePortfolioBacktestHistory({
  form,
  message,
  describeError: apiMessage,
  parseCustomFormula,
  applyStoredCandidate,
  onResultRestored: (restored) => {
    result.value = restored
    replayComparison.value = null
  },
})

// 刷新页面时后端可能还在跑上一轮：找到它并接上轮询，而不是让用户重新提交。
const { resumeActiveJob: resumeActivePortfolioBacktest } = useActiveJobResume({
  descriptions: ['组合回测'],
  adopt: (active) => { job.value = adoptActiveJob<PortfolioBacktestJobState>(active) },
  poll: (active) => pollPortfolioBacktest(active.job_id),
  setLoading: (value) => { loading.value = value },
  reportError: (error) => { message.value = apiMessage(error) },
})

// 退出风控寻优（提交/轮询/取消/候选榜/历史）在 composables/usePortfolioRiskOptimization；
// 表单校验与组装、通用回填留在页面，应用候选后的复现标注也由页面持有。
const riskOptimizationResultsSection = ref<HTMLElement | null>(null)
const {
  riskOptimizationJob,
  riskOptimizationLoading,
  riskOptimizationResult,
  riskOptimizationResultsVisible,
  riskOptimizationBoard,
  riskOptimizationMaxTrials,
  riskOptimizationRestoreRequired,
  riskOptimizationStale,
  riskOptimizationApplicationBlocked,
  riskOptimizationCandidates,
  riskOptimizationHistory,
  riskOptimizationHistoryTotal,
  riskOptimizationHistoryLoading,
  riskOptimizationHistoryLoadingId,
  riskOptimizationHistoryError,
  riskOptimizationHistoryDetail,
  riskOptimizationHistoryDetailId,
  riskOptimizationHistoryDetailLoadingId,
  riskOptimizationHistoryDetailError,
  riskMetric,
  riskMetricRatio,
  riskOptimizationPhaseLabel,
  startRiskOptimization,
  cancelRiskOptimization,
  resumeActiveRiskOptimization,
  loadRiskOptimizationHistory,
  toggleRiskOptimizationHistoryDetail,
  closeRiskOptimizationHistoryDetail,
  loadRiskOptimizationHistoryDetail,
  applyRiskOptimizationCandidate,
} = usePortfolioRiskOptimization({
  validate,
  errors,
  buildPayload,
  message,
  describeError: apiMessage,
  form,
  applyStoredCandidate,
  onCandidateApplied: (applied) => {
    appliedDiscoveryCandidate.value = applied
    replayComparison.value = null
  },
  applyHistoryRequest,
  resultsSection: riskOptimizationResultsSection,
})

onMounted(async () => {
  try {
    metadata.value = await fetchMetadata()
    resetPresetDefaults()
    const stored = sessionStorage.getItem(DISCOVERY_APPLY_KEY)
    if (stored) {
      try {
        applyStoredCandidate(JSON.parse(stored) as unknown)
      } finally {
        sessionStorage.removeItem(DISCOVERY_APPLY_KEY)
      }
    }
    const storedMeta = sessionStorage.getItem(DISCOVERY_APPLY_META_KEY)
    if (storedMeta) {
      try {
        appliedDiscoveryCandidate.value = readAppliedDiscoveryMeta(JSON.parse(storedMeta) as unknown)
      } finally {
        sessionStorage.removeItem(DISCOVERY_APPLY_META_KEY)
      }
    }
    // 条件研究的桥接要放在最后：它可能自己解析公式并改写买卖规则。
    const storedResearch = sessionStorage.getItem(RESEARCH_APPLY_KEY)
    if (storedResearch) {
      try {
        await applyStoredResearch(JSON.parse(storedResearch) as unknown)
      } finally {
        sessionStorage.removeItem(RESEARCH_APPLY_KEY)
      }
    }
    const portfolioHandoff = consumePortfolioBacktestHandoff()
    if (portfolioHandoff) await applyPortfolioBacktestHandoff(portfolioHandoff)
    await loadHistory()
    await loadRiskOptimizationHistory()
    await loadStrategies()
    await Promise.all([
      resumeActivePortfolioBacktest(),
      resumeActiveRiskOptimization(),
    ])
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
  <div class="screen-page portfolio-page" data-testid="portfolio-backtest-page">
    <AppTopbar />

    <main class="content-shell">
      <PageIntro
        eyebrow="RANKED PORTFOLIO / DAILY"
        title="动态组合回测"
        copy="按买入条件产生候选并进行指标排名，卖出后自动用排名靠前的候选补回空出的槽位。"
        note-label="默认流程"
        guide-key="portfolio-backtest"
      >
        <strong>收盘信号 · 次日成交</strong>
        <small>固定槽位 · 每槽等额 · 支持止盈止损与指标卖出</small>
      </PageIntro>

      <div v-if="message" class="notice" :class="{ error: !message.includes('完成') }" role="alert" data-testid="portfolio-message">{{ message }}</div>

      <section class="panel strategy-library" data-testid="strategy-library" aria-labelledby="strategy-library-title">
        <div class="panel-heading">
          <div><span class="section-kicker">MY STRATEGIES</span><h2 id="strategy-library-title">策略库</h2></div>
          <span class="library-count" data-testid="strategy-count">{{ strategies.length }} 个策略</span>
        </div>
        <p class="library-hint">存下来的配置可以一键载回表单重跑。保存时日期区间会固化成绝对日期，避免"最近五年"这类相对区间在几个月后悄悄换成另一段样本。</p>
        <p v-if="strategyLoading && !strategies.length" class="library-empty">正在加载策略…</p>
        <p v-else-if="!strategies.length" class="library-empty" data-testid="strategy-empty">还没有保存的策略。跑完一次组合回测后，在「组合结果」面板顶部填写名称并点「保存为策略」。</p>
        <ul v-else class="strategy-list">
          <li v-for="strategy in strategies" :key="strategy.strategy_id" class="strategy-row" :data-testid="`strategy-row-${strategy.strategy_id}`">
            <div class="strategy-main">
              <template v-if="renamingId === strategy.strategy_id">
                <input class="strategy-rename-input" :data-testid="`strategy-rename-input-${strategy.strategy_id}`" v-model="renameDraft" maxlength="60" aria-label="策略名称" @keyup.enter="commitRename(strategy.strategy_id)">
                <button class="secondary-button" type="button" :data-testid="`strategy-rename-save-${strategy.strategy_id}`" :disabled="strategyBusyId === strategy.strategy_id" @click="commitRename(strategy.strategy_id)">保存</button>
                <button class="text-button" type="button" @click="cancelRename">取消</button>
              </template>
              <template v-else>
                <strong class="strategy-name">{{ strategy.name }}</strong>
                <span class="strategy-range" :data-testid="`strategy-range-${strategy.strategy_id}`">{{ strategyRange(strategy) }}</span>
                <span class="strategy-metric">年化 {{ formatPercent(strategy.summary.performance?.annual_return) }}</span>
                <span class="strategy-metric">回撤 {{ formatPercent(strategy.summary.performance?.max_drawdown) }}</span>
              </template>
            </div>
            <div v-if="renamingId !== strategy.strategy_id" class="strategy-actions">
              <button class="secondary-button" type="button" :data-testid="`strategy-load-${strategy.strategy_id}`" :disabled="strategyBusyId === strategy.strategy_id" @click="applyStrategy(strategy.strategy_id)">载入配置</button>
              <button class="text-button" type="button" :data-testid="`strategy-rename-${strategy.strategy_id}`" @click="startRename(strategy)">重命名</button>
              <button v-if="pendingDeleteId === strategy.strategy_id" class="danger-button" type="button" :data-testid="`strategy-confirm-delete-${strategy.strategy_id}`" :disabled="strategyBusyId === strategy.strategy_id" @click="confirmDelete(strategy.strategy_id)">确认删除</button>
              <button v-else class="text-button" type="button" :data-testid="`strategy-delete-${strategy.strategy_id}`" @click="pendingDeleteId = strategy.strategy_id">删除</button>
            </div>
          </li>
        </ul>
      </section>

      <div class="workspace-grid portfolio-grid">
        <form class="config-panel panel portfolio-config" data-testid="portfolio-config" @submit.prevent="submit">
          <div class="panel-heading">
            <div><span class="section-kicker">01 / CONFIGURE</span><h2>组合配置</h2></div>
            <span class="required-hint">* 必填</span>
          </div>

          <div class="formula-mode-tabs" role="tablist" aria-label="公式来源">
            <button type="button" :class="{ active: form.mode === 'preset' }" data-testid="portfolio-mode-preset" role="tab" :aria-selected="form.mode === 'preset'" @click="setMode('preset')">预置指标</button>
            <button type="button" :class="{ active: form.mode === 'custom' }" data-testid="portfolio-mode-custom" role="tab" :aria-selected="form.mode === 'custom'" @click="setMode('custom')">自定义公式</button>
          </div>

          <fieldset v-if="form.mode === 'custom'" class="form-section custom-formula-section">
            <legend>粘贴通达信公式</legend>
            <textarea v-model="form.formulaText" data-testid="portfolio-formula" rows="7" spellcheck="false" placeholder="例如：买入:CROSS(C,MA(C,20)); 卖出:CROSS(MA(C,20),C); 排序:C/MA(C,20);"></textarea>
            <button class="secondary-button" data-testid="portfolio-parse-formula" type="button" :disabled="customParseLoading" @click="parseCustomFormula">{{ customParseLoading ? '解析中…' : '解析公式' }}</button>
            <p class="helper">参数使用 <code>名称:=数值</code>；命名输出可用于买入/卖出指标条件、指标比较和排序。</p>
            <p v-if="customParseError" class="field-error">{{ customParseError }}</p>
            <div v-if="customMetadata" class="custom-formula-meta"><span>{{ customMetadata.parameters.length }} 个参数</span><span>{{ customMetadata.signals.length }} 个买卖输出</span><span>{{ customMetadata.values?.length ?? 0 }} 个指标输出</span></div>
            <div v-if="customMetadata?.parameters.length" class="parameter-grid">
              <div v-for="parameter in customMetadata.parameters" :key="parameter.name"><label :for="`portfolio-param-${parameter.name}`">{{ parameter.name }}</label><input :id="`portfolio-param-${parameter.name}`" v-model.number="form.formulaParameters[parameter.name]" :data-testid="`portfolio-param-${parameter.name}`" type="number" :min="parameter.minimum" :max="parameter.maximum" :step="parameter.step"></div>
            </div>
            <p v-if="errors.formulaText" class="field-error">{{ errors.formulaText }}</p>
            <p v-if="errors.formulaParameters" class="field-error">{{ errors.formulaParameters }}</p>
          </fieldset>

          <fieldset class="form-section">
            <legend>数据范围</legend>
            <p class="helper">读取已经导入本地 DuckDB 的日线，不会上传行情文件。请先在“本地行情”页面导入数据。</p>
            <div class="compact-fields portfolio-fields-top">
              <div><label for="portfolio-universe">品种范围</label><select id="portfolio-universe" v-model="form.universe" data-testid="portfolio-universe"><option value="all">沪深全部品种</option><option value="sh">仅上海</option><option value="sz">仅深圳</option><option value="custom">自定义列表</option><option value="codes">指定标的</option></select></div>
              <div><label for="portfolio-max-positions">持仓槽位</label><input id="portfolio-max-positions" v-model.number="form.maxPositions" data-testid="portfolio-max-positions" type="number" min="1" max="100" step="1"></div>
              <div><label for="portfolio-position-mode">建仓方式</label><select id="portfolio-position-mode" v-model="form.positionMode" data-testid="portfolio-position-mode"><option value="full">每槽等额</option><option value="fixed">固定股数</option></select></div>
            </div>
            <div v-if="form.positionMode === 'fixed'" class="portfolio-inline-field"><label for="portfolio-fixed-size">每槽买入股数</label><input id="portfolio-fixed-size" v-model.number="form.fixedSize" data-testid="portfolio-fixed-size" type="number" min="100" max="100000000" step="100"><small class="scope-helper">每笔建仓都买入相同股数（100 的整数倍），资金不足时会按整手递减或跳过。</small><p v-if="errors.fixedSize" class="field-error">{{ errors.fixedSize }}</p></div>
            <div v-if="form.universe === 'custom'" class="portfolio-inline-field"><label for="portfolio-universe-file">股票列表文件</label><input id="portfolio-universe-file" v-model="form.universeFile" data-testid="portfolio-universe-file" type="text" placeholder="每行 SH 600000 或 SZ 000001"><p v-if="errors.universeFile" class="field-error">{{ errors.universeFile }}</p></div>
            <div v-if="form.universe === 'codes'" class="portfolio-inline-field"><label for="portfolio-codes">证券代码</label><textarea id="portfolio-codes" v-model="form.codesText" data-testid="portfolio-codes" rows="3" placeholder="600519&#10;SH:510300&#10;000001，300750"></textarea><small class="scope-helper">支持逗号或换行分隔；可带 SH/SZ 前缀，不带前缀时按代码规则推断市场。只回测这些标的时把持仓槽位设为 1。</small><p v-if="errors.codesText" class="field-error">{{ errors.codesText }}</p></div>
            <div class="scope-config inline-scope-config" data-testid="portfolio-scope">
              <div class="scope-options"><span>证券类型</span><label v-for="option in INSTRUMENT_TYPE_OPTIONS" :key="option.key"><input type="checkbox" :data-testid="`portfolio-scope-type-${option.key}`" :checked="form.instrumentTypes.includes(option.key)" @change="toggleInstrumentType(option.key)">{{ option.label }}</label></div>
              <div class="scope-options"><span>板块</span><label v-for="option in BOARD_OPTIONS" :key="option.key"><input type="checkbox" :data-testid="`portfolio-scope-board-${option.key}`" :checked="form.boards.includes(option.key)" @change="toggleBoard(option.key)">{{ option.label }}</label></div>
              <div class="scope-options"><span>风险标的</span><label><input type="checkbox" data-testid="portfolio-exclude-st" v-model="form.excludeSt">排除 ST / *ST / 退市</label></div>
              <small class="scope-helper">未勾选表示全部；类型和板块同时选择时取交集。排除 ST 按当前股票名称判定——行情库没有历史名称快照，回测里会把“后来才戴帽”的股票一并排除，样本比实盘更保守。</small>
            </div>
            <p v-if="errors.maxPositions" class="field-error">{{ errors.maxPositions }}</p>
          </fieldset>

          <RuleGroupEditor
            :model-value="form.buyRule"
            :values="availableValues"
            :condition-operators="metadata?.condition_operators"
            title="买入条件组合"
            testid="portfolio-buy-rule"
            @update:model-value="setBuyRule"
          />
          <p v-if="errors.buyRule" class="field-error">{{ errors.buyRule }}</p>

          <fieldset class="form-section">
            <legend>排名与刷新</legend>
            <div><label for="portfolio-ranking-value">排序指标 <span class="required">*</span></label><select id="portfolio-ranking-value" v-model="form.rankingValue" data-testid="portfolio-ranking-value" :disabled="availableValues.length === 0"><option value="" disabled>请选择指标输出</option><option v-for="value in availableValues" :key="value.id" :value="value.id">{{ valueOptionLabel(value) }}</option></select></div>
            <p v-if="availableValues.length === 0" class="helper">当前公式没有可用的数值输出，请在公式中增加如 <code>排序:C/MA(C,20);</code> 的命名表达式。</p>
            <p v-if="errors.rankingValue" class="field-error">{{ errors.rankingValue }}</p>
            <div class="compact-fields portfolio-fields-top">
              <div><label for="portfolio-rank-order">排序方向</label><select id="portfolio-rank-order" v-model="form.rankOrder" data-testid="portfolio-rank-order"><option value="desc">从高到低</option><option value="asc">从低到高</option></select></div>
              <div><label for="portfolio-frequency">候选刷新</label><select id="portfolio-frequency" v-model="form.rebalanceFrequency" data-testid="portfolio-frequency"><option value="daily">每日</option><option value="weekly">每周</option><option value="monthly">每月</option></select></div>
            </div>
            <p class="helper">刷新只负责发现新的候选；已有持仓不会因为排名变化被强制换仓，卖出后才释放槽位。</p>
          </fieldset>

          <fieldset class="form-section">
            <legend>适配性权重（可选）</legend>
            <div class="portfolio-inline-field">
              <label for="portfolio-fitness-weight">排序时参考历史适配分</label>
              <select id="portfolio-fitness-weight" v-model.number="form.fitnessRankWeight" data-testid="portfolio-fitness-weight">
                <option :value="0">不使用</option>
                <option :value="0.25">轻参考 25%</option>
                <option :value="0.5">均衡 50%</option>
                <option :value="0.75">重适配 75%</option>
              </select>
            </div>
            <div v-if="form.fitnessRankWeight > 0" class="compact-fields portfolio-fields-top">
              <div><label for="portfolio-fitness-trades">历史最少成交</label><input id="portfolio-fitness-trades" v-model.number="form.fitnessMinTrades" data-testid="portfolio-fitness-trades" type="number" min="1" step="1"></div>
              <div><label for="portfolio-fitness-drawdown">历史最大回撤上限 (%)</label><input id="portfolio-fitness-drawdown" v-model.number="form.fitnessMaxDrawdown" data-testid="portfolio-fitness-drawdown" type="number" min="0" max="100" step="5"></div>
            </div>
            <p v-if="errors.fitnessMinTrades" class="field-error">{{ errors.fitnessMinTrades }}</p>
            <p v-if="errors.fitnessMaxDrawdown" class="field-error">{{ errors.fitnessMaxDrawdown }}</p>
            <p class="helper">每个调仓日只用更早日期的成交和净值算适配分；历史样本不足时权重自动衰减，既不加分也不扣分。</p>
          </fieldset>

          <fieldset class="form-section">
            <legend>卖出规则 <span class="required">至少一项</span></legend>
            <RuleGroupEditor
              :model-value="form.sellRule"
              :values="availableValues"
              :condition-operators="metadata?.condition_operators"
              title="卖出条件"
              testid="portfolio-sell-rule"
              @update:model-value="setSellRule"
            />
            <div class="risk-control" data-testid="portfolio-stop-loss-control">
              <label class="risk-toggle"><input v-model="form.stopLossEnabled" data-testid="portfolio-stop-loss-enabled" type="checkbox"><span>止损</span></label>
              <select v-model="form.stopLossMode" data-testid="portfolio-stop-loss-mode" :disabled="!form.stopLossEnabled"><option value="pct">百分比</option><option value="atr">ATR ×</option></select>
              <input v-if="form.stopLossMode === 'pct'" v-model.number="form.stopLossPct" data-testid="portfolio-stop-loss" type="number" min="0.01" max="100" step="0.5" aria-label="止损百分比" :disabled="!form.stopLossEnabled"><input v-else v-model.number="form.stopLossAtrMultiplier" data-testid="portfolio-stop-loss-atr" type="number" min="0.01" max="100" step="0.1" aria-label="止损 ATR 倍数" :disabled="!form.stopLossEnabled">
              <span>{{ form.stopLossMode === 'pct' ? '%' : '倍' }}</span>
              <label class="cooldown-field">冷却 <input v-model.number="form.stopLossCooldownDays" data-testid="portfolio-stop-loss-cooldown" type="number" min="0" max="10000" step="1" :disabled="!form.stopLossEnabled"> <span>交易日</span></label>
            </div>
            <div class="risk-control" data-testid="portfolio-take-profit-control">
              <label class="risk-toggle"><input v-model="form.takeProfitEnabled" data-testid="portfolio-take-profit-enabled" type="checkbox"><span>止盈</span></label>
              <select v-model="form.takeProfitMode" data-testid="portfolio-take-profit-mode" :disabled="!form.takeProfitEnabled"><option value="pct">百分比</option><option value="atr">ATR ×</option></select>
              <input v-if="form.takeProfitMode === 'pct'" v-model.number="form.takeProfitPct" data-testid="portfolio-take-profit" type="number" min="0.01" max="1000" step="0.5" aria-label="止盈百分比" :disabled="!form.takeProfitEnabled"><input v-else v-model.number="form.takeProfitAtrMultiplier" data-testid="portfolio-take-profit-atr" type="number" min="0.01" max="100" step="0.1" aria-label="止盈 ATR 倍数" :disabled="!form.takeProfitEnabled">
              <span>{{ form.takeProfitMode === 'pct' ? '%' : '倍' }}</span>
              <label class="cooldown-field">冷却 <input v-model.number="form.takeProfitCooldownDays" data-testid="portfolio-take-profit-cooldown" type="number" min="0" max="10000" step="1" :disabled="!form.takeProfitEnabled"> <span>交易日</span></label>
            </div>
            <div class="risk-control" data-testid="portfolio-trailing-stop-control">
              <label class="risk-toggle"><input v-model="form.trailingStopEnabled" data-testid="portfolio-trailing-stop-enabled" type="checkbox"><span>移动止盈</span></label>
              <select v-model="form.trailingStopMode" data-testid="portfolio-trailing-stop-mode" :disabled="!form.trailingStopEnabled"><option value="pct">百分比</option><option value="atr">ATR ×</option></select>
              <input v-if="form.trailingStopMode === 'pct'" v-model.number="form.trailingStopPct" data-testid="portfolio-trailing-stop" type="number" min="0.01" max="100" step="0.5" aria-label="移动止盈百分比" :disabled="!form.trailingStopEnabled"><input v-else v-model.number="form.trailingStopAtrMultiplier" data-testid="portfolio-trailing-stop-atr" type="number" min="0.01" max="100" step="0.1" aria-label="移动止盈 ATR 倍数" :disabled="!form.trailingStopEnabled">
              <span>{{ form.trailingStopMode === 'pct' ? '%' : '倍' }}</span>
              <label class="cooldown-field">冷却 <input v-model.number="form.trailingStopCooldownDays" data-testid="portfolio-trailing-stop-cooldown" type="number" min="0" max="10000" step="1" :disabled="!form.trailingStopEnabled"> <span>交易日</span></label>
            </div>
            <div class="risk-control" data-testid="portfolio-max-hold-days-control">
              <label class="risk-toggle"><input v-model="form.maxHoldDaysEnabled" data-testid="portfolio-max-hold-days-enabled" type="checkbox"><span>最长持有</span></label>
              <input v-model.number="form.maxHoldDays" data-testid="portfolio-max-hold-days" type="number" min="1" max="10000" step="1" aria-label="最长持有交易日数" :disabled="!form.maxHoldDaysEnabled"> <span>交易日</span>
            </div>
            <p class="helper">满 N 个交易日后按次日开盘卖出。它截断的是时间而不是涨幅，对盈利和亏损的持仓一视同仁，所以也能把套牢票赶出槽位。</p>
            <p v-if="errors.sellRules" class="field-error">{{ errors.sellRules }}</p>
            <p v-if="errors.stopLossPct || errors.stopLossAtrMultiplier || errors.stopLossCooldownDays" class="field-error">{{ errors.stopLossPct || errors.stopLossAtrMultiplier || errors.stopLossCooldownDays }}</p>
            <p v-if="errors.takeProfitPct || errors.takeProfitAtrMultiplier || errors.takeProfitCooldownDays" class="field-error">{{ errors.takeProfitPct || errors.takeProfitAtrMultiplier || errors.takeProfitCooldownDays }}</p>
            <p v-if="errors.trailingStopPct || errors.trailingStopAtrMultiplier || errors.trailingStopCooldownDays" class="field-error">{{ errors.trailingStopPct || errors.trailingStopAtrMultiplier || errors.trailingStopCooldownDays }}</p>
            <p class="helper">指标规则在收盘确认，默认下一根 K 线执行；风险控制按当日最高/最低价触发，ATR 周期固定为 {{ form.atrPeriod }}，各类冷却独立计算。</p>
          </fieldset>

          <DateRangeSelector v-model="dateRange" prefix="portfolio" testid="portfolio-date-range" />
          <p v-if="errors.startDate" class="field-error">{{ errors.startDate }}</p>
          <p v-if="errors.endDate" class="field-error">{{ errors.endDate }}</p>

          <button class="advanced-toggle" data-testid="portfolio-advanced-toggle" type="button" :aria-expanded="showAdvanced" @click="showAdvanced = !showAdvanced"><span>{{ showAdvanced ? '收起高级设置' : '高级设置' }}</span><small>资金 · 执行 · 费用</small></button>
          <div v-if="showAdvanced" class="advanced-settings" data-testid="portfolio-advanced-settings">
            <fieldset class="form-section compact-fields"><div><label for="portfolio-initial-cash">初始资金</label><input id="portfolio-initial-cash" v-model.number="form.initialCash" data-testid="portfolio-initial-cash" type="number" min="1" step="10000"></div><div><label for="portfolio-execution">成交方式</label><select id="portfolio-execution" v-model="form.execution" data-testid="portfolio-execution"><option value="next_open">下一根开盘</option><option value="next_close">下一根收盘</option></select></div></fieldset>
            <p v-if="errors.initialCash" class="field-error">{{ errors.initialCash }}</p>
            <fieldset class="form-section compact-fields"><div><label for="portfolio-commission">佣金费率</label><input id="portfolio-commission" v-model.number="form.commission" data-testid="portfolio-commission" type="number" min="0" max="0.1" step="0.0001"></div><div><label for="portfolio-min-commission">最低佣金</label><input id="portfolio-min-commission" v-model.number="form.minCommission" data-testid="portfolio-min-commission" type="number" min="0" step="0.01"></div></fieldset>
            <fieldset class="form-section compact-fields"><div><label for="portfolio-stamp-tax">印花税率</label><input id="portfolio-stamp-tax" v-model.number="form.stampTax" data-testid="portfolio-stamp-tax" type="number" min="0" max="0.1" step="0.0001"></div><div><label for="portfolio-slippage">每股滑点（元）</label><input id="portfolio-slippage" v-model.number="form.slippage" data-testid="portfolio-slippage" type="number" min="0" step="0.001"></div></fieldset>
          </div>

          <div class="action-stack">
            <button class="primary-button" data-testid="start-portfolio-backtest" type="button" :disabled="!canSubmit" @click="submit"><span v-if="loading" class="spinner" aria-hidden="true"></span>{{ loading ? '组合回测中…' : '开始组合回测' }}</button>
            <button class="secondary-button" data-testid="portfolio-apply-to-formula-screen" type="button" :disabled="loading || customParseLoading" @click="applyBuyRuleToFormulaScreen">把买入条件与排序带到公式选股</button>
            <p class="helper">只带入买入侧条件、标的范围和排序；卖出/风控、槽位数量和 ST 排除不属于一次性公式选股。</p>
            <label class="portfolio-inline-field" for="portfolio-risk-search-level"><span>风险寻优强度</span><select id="portfolio-risk-search-level" v-model.number="riskOptimizationMaxTrials" data-testid="portfolio-risk-search-level"><option :value="1000">低 · 1000 组</option><option :value="2000">中 · 2000 组</option><option :value="3000">高 · 3000 组</option></select></label>
            <button class="secondary-button" data-testid="start-portfolio-risk-optimization" type="button" :disabled="!canSubmit" @click="startRiskOptimization"><span v-if="riskOptimizationLoading" class="spinner" aria-hidden="true"></span>{{ riskOptimizationLoading ? '寻优中…' : '寻优退出风控' }}</button>
          </div>
          <div v-if="riskOptimizationLoading && riskOptimizationJob" class="task-progress task-progress-panel" data-testid="portfolio-risk-optimization-progress">
            <div class="task-progress-heading"><span>{{ riskOptimizationPhaseLabel(riskOptimizationJob) }}</span><strong>{{ Math.round((riskOptimizationJob.progress ?? 0) * 100) }}%</strong></div>
            <div class="progress-track"><span :style="{ width: `${Math.round((riskOptimizationJob.progress ?? 0) * 100)}%` }"></span></div>
            <small>已评估 {{ riskOptimizationJob.total_scanned }} / {{ riskOptimizationJob.total_candidates || riskOptimizationMaxTrials }} 组</small>
            <button class="text-button" data-testid="cancel-portfolio-risk-optimization" type="button" :disabled="riskOptimizationJob.cancel_requested" @click="cancelRiskOptimization">{{ riskOptimizationJob.cancel_requested ? '正在取消…' : '取消寻优' }}</button>
          </div>
          <p class="privacy-note">计算在本机完成；结果基于本地日线，不代表未来收益。</p>
        </form>

        <section class="results-panel panel" data-testid="portfolio-results" aria-live="polite">
          <div class="panel-heading results-heading"><div><span class="section-kicker">02 / RESULTS</span><h2>组合结果</h2></div><div class="results-actions"><button v-if="result" class="secondary-button" type="button" data-testid="portfolio-pnl-analysis-toggle" :aria-expanded="showPnlAnalysis" aria-controls="portfolio-pnl-analysis-panel" @click="showPnlAnalysis = !showPnlAnalysis">{{ showPnlAnalysis ? '收起盈亏分析' : '盈亏分析' }}</button><button v-if="result" class="secondary-button export-backtest" type="button" data-testid="export-portfolio-backtest" @click="downloadResult">导出 JSON</button></div></div>
          <div v-if="result" class="strategy-save-bar" data-testid="strategy-save-bar">
            <input v-model="newStrategyName" class="strategy-name-input" data-testid="strategy-name-input" maxlength="60" placeholder="给这套配置起个名字" aria-label="策略名称" @keyup.enter="saveCurrentStrategy">
            <button class="primary-button" type="button" data-testid="strategy-save" :disabled="strategySaving" @click="saveCurrentStrategy">{{ strategySaving ? '保存中…' : '保存为策略' }}</button>
            <small>存的是当前表单配置 + 这次结果的绩效快照；下次可在「策略库」一键载回。</small>
          </div>
          <section v-if="riskOptimizationResult" v-show="riskOptimizationResultsVisible" id="portfolio-risk-optimization-results-panel" ref="riskOptimizationResultsSection" class="data-section risk-optimization-results" data-testid="portfolio-risk-optimization-results">
            <div class="section-heading"><div><span class="section-kicker">RISK OPTIMIZATION</span><h3>退出风控候选</h3></div><div class="risk-optimization-heading-actions"><span>验证段排序 · 留出段独立复核</span><button class="text-button" data-testid="portfolio-risk-hide-candidates" type="button" :aria-expanded="riskOptimizationResultsVisible" aria-controls="portfolio-risk-optimization-results-panel" @click="riskOptimizationResultsVisible = false">隐藏候选</button></div></div>
            <p class="helper">训练段搜索；验证段至少 5 笔交易且最大回撤不超过 30% 才进入榜单。应用候选保留当前日期区间，榜单指标仍按验证段计算。</p>
            <div v-if="riskOptimizationRestoreRequired" class="notice error" data-testid="portfolio-risk-optimization-restore-required">已接回后台寻优，但当前表单不是寻优时的配置。请从历史列表载入此任务后再应用候选。</div>
            <div v-else-if="riskOptimizationStale" class="notice error" data-testid="portfolio-risk-optimization-stale">买卖规则、排序或回测设置已变化。请重新寻优后再应用候选。</div>
            <div class="formula-mode-tabs" role="tablist" aria-label="退出风控候选榜单">
              <button type="button" data-testid="portfolio-risk-sharpe-board" role="tab" :aria-selected="riskOptimizationBoard === 'sharpe'" :class="{ active: riskOptimizationBoard === 'sharpe' }" @click="riskOptimizationBoard = 'sharpe'">夏普榜前 10</button>
              <button type="button" data-testid="portfolio-risk-return-board" role="tab" :aria-selected="riskOptimizationBoard === 'return'" :class="{ active: riskOptimizationBoard === 'return' }" @click="riskOptimizationBoard = 'return'">收益榜前 10</button>
            </div>
            <div v-if="!riskOptimizationCandidates.length" class="inline-empty">没有候选通过验证段门槛；可扩大日期范围后重试。</div>
            <div v-else class="table-wrap"><table data-testid="portfolio-risk-candidates"><caption class="sr-only">退出风控寻优候选</caption><thead><tr><th># / 参数</th><th>训练<br>年化 / 夏普</th><th>验证<br>收益 / Sharpe / 回撤</th><th>留出<br>收益 / Sharpe / 回撤</th><th>操作</th></tr></thead><tbody>
              <tr v-for="(candidate, index) in riskOptimizationCandidates" :key="candidate.candidate_hash" :data-testid="`portfolio-risk-candidate-${candidate.candidate_hash}`">
                <td><strong>{{ index + 1 }}</strong><small>{{ candidate.human_readable_rules }}</small></td>
                <td>{{ formatPercent(riskMetric(candidate, 'train', 'annual_return')) }} / {{ formatNumber(riskMetric(candidate, 'train', 'sharpe')) }}</td>
                <td>{{ formatPercent(riskMetric(candidate, 'validation', 'annual_return')) }} / {{ formatNumber(riskMetric(candidate, 'validation', 'sharpe')) }} / {{ formatPercent(riskMetric(candidate, 'validation', 'max_drawdown')) }}<small>胜率 {{ formatPercent(riskMetric(candidate, 'validation', 'win_rate')) }} · 盈亏比 {{ riskMetricRatio(candidate, 'validation', 'payoff_ratio') }} · 盈利因子 {{ riskMetricRatio(candidate, 'validation', 'profit_factor') }}</small><small>平仓 {{ formatNumber(riskMetric(candidate, 'validation', 'total_trades'), 0) }} · 买/卖单 {{ formatNumber(riskMetric(candidate, 'validation', 'executed_buy_orders'), 0) }} / {{ formatNumber(riskMetric(candidate, 'validation', 'executed_sell_orders'), 0) }}</small></td>
                <td>{{ formatPercent(riskMetric(candidate, 'holdout', 'annual_return')) }} / {{ formatNumber(riskMetric(candidate, 'holdout', 'sharpe')) }} / {{ formatPercent(riskMetric(candidate, 'holdout', 'max_drawdown')) }}<small>胜率 {{ formatPercent(riskMetric(candidate, 'holdout', 'win_rate')) }} · 盈亏比 {{ riskMetricRatio(candidate, 'holdout', 'payoff_ratio') }} · 盈利因子 {{ riskMetricRatio(candidate, 'holdout', 'profit_factor') }}</small><small>平仓 {{ formatNumber(riskMetric(candidate, 'holdout', 'total_trades'), 0) }} · 买/卖单 {{ formatNumber(riskMetric(candidate, 'holdout', 'executed_buy_orders'), 0) }} / {{ formatNumber(riskMetric(candidate, 'holdout', 'executed_sell_orders'), 0) }}</small></td>
                <td><button class="secondary-button" :data-testid="`portfolio-risk-apply-${candidate.candidate_hash}`" type="button" :disabled="riskOptimizationApplicationBlocked" @click="applyRiskOptimizationCandidate(candidate)">应用</button></td>
              </tr>
            </tbody></table></div>
          </section>
          <div v-if="riskOptimizationResult && !riskOptimizationResultsVisible" class="risk-optimization-collapsed" data-testid="portfolio-risk-optimization-collapsed">
            <span>退出风控候选已载入</span>
            <button class="text-button" data-testid="portfolio-risk-show-candidates" type="button" :aria-expanded="riskOptimizationResultsVisible" aria-controls="portfolio-risk-optimization-results-panel" @click="riskOptimizationResultsVisible = true">显示候选</button>
          </div>
          <div v-if="!result && !riskOptimizationResult" class="empty-state" data-testid="portfolio-empty"><div class="empty-icon" aria-hidden="true">/</div><h3>{{ loading ? '正在运行动态组合回测…' : '还没有组合结果' }}</h3><p>{{ loading ? '正在逐股计算公式并模拟槽位补位，完成后显示净值与候选排名。' : '配置买入条件、排序指标和卖出条件后开始回测。' }}</p></div>
          <PortfolioResultPanel
            :result="result"
            :available-values="availableValues"
            :replay-comparison="replayComparison"
            :price-basis="resultPriceBasis"
            :limitations="resultLimitations"
            :show-pnl-analysis="showPnlAnalysis"
            :job-progress="job?.progress ?? null"
          />
        </section>
      </div>
      <div v-if="loading && job" class="task-progress task-progress-panel" data-testid="portfolio-progress"><div class="task-progress-heading"><span>{{ job.cancel_requested ? '正在取消，等待当前计算批次结束' : '组合回测任务进度' }}</span><strong>{{ Math.round(job.progress * 100) }}%</strong></div><div class="progress-track"><span :style="{ width: `${Math.round(job.progress * 100)}%` }"></span></div><small>已处理 {{ job.total_scanned }} / {{ job.total_candidates || 1 }}</small><button class="text-button" type="button" data-testid="cancel-portfolio-backtest" :disabled="job.cancel_requested || !['queued', 'running'].includes(job.status)" @click="cancelRunningPortfolioBacktest">{{ job.cancel_requested ? '正在取消…' : '取消回测' }}</button></div>

      <section class="panel risk-optimization-history" data-testid="portfolio-risk-optimization-history">
        <div class="section-heading">
          <div><span class="section-kicker">RISK SEARCH HISTORY</span><h2>退出风控寻优历史</h2></div>
          <button class="secondary-button" type="button" data-testid="portfolio-risk-history-refresh" :disabled="riskOptimizationHistoryLoading" @click="loadRiskOptimizationHistory()">{{ riskOptimizationHistoryLoading ? '读取中…' : '刷新' }}</button>
        </div>
        <p class="helper">已完成的寻优结果会保存在本地数据库中。载入后可恢复寻优配置、查看三段表现，并继续应用候选。</p>
        <p v-if="riskOptimizationHistoryError" class="notice error" data-testid="portfolio-risk-history-error">{{ riskOptimizationHistoryError }}</p>
        <div v-if="!riskOptimizationHistory.length && riskOptimizationHistoryLoading" class="inline-empty">正在读取历史寻优结果…</div>
        <div v-else-if="!riskOptimizationHistory.length" class="inline-empty" data-testid="portfolio-risk-history-empty">暂无已完成的退出风控寻优。</div>
        <div v-else class="table-wrap">
          <table data-testid="portfolio-risk-history-table">
            <caption class="sr-only">退出风控寻优历史</caption>
            <thead><tr><th>完成时间</th><th>数据区间</th><th>已评估 / 计划</th><th>验证候选</th><th>验证榜最佳表现</th><th>耗时</th><th>操作</th></tr></thead>
            <tbody>
              <tr v-for="item in riskOptimizationHistory" :key="item.job_id" :data-testid="`portfolio-risk-history-${item.job_id}`">
                <td class="muted-code">{{ formatChinaTimestamp(item.finished_at) }}</td>
                <td class="muted-code">{{ item.start_date ?? '—' }} → {{ item.end_date ?? '—' }}</td>
                <td>{{ item.evaluated_trials ?? '—' }} / {{ item.requested_trials ?? '—' }}</td>
                <td>{{ item.candidate_count }}</td>
                <td>夏普 {{ formatNumber(item.validation_best_sharpe) }}<small class="muted-code"> · 年化 {{ formatPercent(item.validation_best_annual_return) }}</small></td>
                <td>{{ formatDuration(item.duration_seconds) }}</td>
                <td><div class="risk-optimization-history-actions"><button class="table-link" type="button" :data-testid="`portfolio-risk-history-detail-${item.job_id}`" :disabled="riskOptimizationHistoryDetailLoadingId !== null" @click="toggleRiskOptimizationHistoryDetail(item.job_id)">{{ riskOptimizationHistoryDetailLoadingId === item.job_id ? '读取详情…' : riskOptimizationHistoryDetailId === item.job_id ? '收起详情' : '详情' }}</button><button class="table-link" type="button" :data-testid="`portfolio-risk-history-load-${item.job_id}`" :disabled="riskOptimizationHistoryLoadingId !== null" @click="loadRiskOptimizationHistoryDetail(item.job_id)">{{ riskOptimizationHistoryLoadingId === item.job_id ? '载入中…' : '载入候选' }}</button></div></td>
              </tr>
            </tbody>
          </table>
        </div>
        <p v-if="riskOptimizationHistoryDetailError" class="notice error" data-testid="portfolio-risk-history-detail-error">{{ riskOptimizationHistoryDetailError }}</p>
        <RiskOptimizationHistoryDetail
          v-if="riskOptimizationHistoryDetailId && riskOptimizationHistoryDetail"
          :result="riskOptimizationHistoryDetail"
          @close="closeRiskOptimizationHistoryDetail"
        />
        <div v-if="riskOptimizationHistory.length < riskOptimizationHistoryTotal" class="history-load-more">
          <button class="secondary-button" type="button" data-testid="portfolio-risk-history-load-more" :disabled="riskOptimizationHistoryLoading" @click="loadRiskOptimizationHistory(true)">{{ riskOptimizationHistoryLoading ? '读取中…' : `加载更早记录（${riskOptimizationHistory.length} / ${riskOptimizationHistoryTotal}）` }}</button>
        </div>
      </section>
      <p v-if="historyError" class="notice error" data-testid="portfolio-history-error">{{ historyError }}</p>
      <BacktestHistoryPanel
        :items="history"
        :loading="historyLoading"
        testid="portfolio-history"
        title="历史组合回测"
        empty-text="暂无历史组合回测。"
        @refresh="loadHistory"
        @load="loadHistoryDetail"
        @remove="removeHistory"
      />
    </main>
  </div>
</template>
