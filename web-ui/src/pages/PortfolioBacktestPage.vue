<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'

import {
  createPortfolioBacktest,
  deletePortfolioBacktestHistory,
  fetchPortfolioBacktestHistory,
  getPortfolioBacktest,
  getPortfolioBacktestHistory,
  getPortfolioBacktestResults,
} from '../api/portfolioBacktest'
import { FormulaScreenApiError, fetchMetadata, parseFormula } from '../api/formulaScreen'
import BacktestHistoryPanel from '../components/BacktestHistoryPanel.vue'
import RuleGroupEditor from '../components/RuleGroupEditor.vue'
import type {
  BacktestHistorySummary,
  BacktestExecution,
  CustomFormulaMetadata,
  FormulaScreenMetadata,
  InstrumentBoard,
  InstrumentType,
  PortfolioBacktestJobState,
  PortfolioBacktestPayload,
  PortfolioBacktestResult,
  PortfolioTrade,
  PortfolioRebalanceFrequency,
  RuleGroupPayload,
  SignalDefinition,
  Universe,
  ValueDefinition,
} from '../types'
import { numericRuleForSignal, signalDisplayName, valueOptionLabel } from '../utils/formulaScreen'

const DISCOVERY_APPLY_KEY = 'indicator-lab.strategy-discovery.apply'
const DISCOVERY_APPLY_META_KEY = 'indicator-lab.strategy-discovery.apply-meta'

interface AppliedDiscoveryCandidate {
  candidate_hash: string
  data_fingerprint: string | null
  validation_metrics: Record<string, number | string | boolean | null> | null
}

interface ReplayComparison {
  candidateHash: string
  tradeRecords: '一致' | '不一致' | '未提供'
  performance: '一致' | '不一致' | '未提供'
  dataFingerprint: '一致' | '不一致' | '未提供'
}

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

interface PortfolioFormState {
  mode: 'preset' | 'custom'
  buyRule: RuleGroupPayload
  universe: Universe
  universeFile: string
  instrumentTypes: InstrumentType[]
  boards: InstrumentBoard[]
  rankingValue: string
  rankOrder: 'asc' | 'desc'
  maxPositions: number
  rebalanceFrequency: PortfolioRebalanceFrequency
  execution: BacktestExecution
  formulaText: string
  formulaParameters: Record<string, number>
  sellRule: RuleGroupPayload
  stopLossEnabled: boolean
  stopLossMode: 'pct' | 'atr'
  stopLossPct: number
  stopLossAtrMultiplier: number
  stopLossCooldownDays: number
  takeProfitEnabled: boolean
  takeProfitMode: 'pct' | 'atr'
  takeProfitPct: number
  takeProfitAtrMultiplier: number
  takeProfitCooldownDays: number
  trailingStopEnabled: boolean
  trailingStopMode: 'pct' | 'atr'
  trailingStopPct: number
  trailingStopAtrMultiplier: number
  trailingStopCooldownDays: number
  atrPeriod: number
  startDate: string
  endDate: string
  initialCash: number
  commission: number
  minCommission: number
  stampTax: number
  slippage: number
  fitnessFilterEnabled: boolean
  fitnessMinScore: number
  fitnessMinTrades: number
  fitnessMaxDrawdown: number
}

const metadata = ref<FormulaScreenMetadata | null>(null)
const customMetadata = ref<CustomFormulaMetadata | null>(null)
const parsedFormulaText = ref('')
const form = reactive<PortfolioFormState>({
  mode: 'preset',
  buyRule: { signals: [], conditions: [], combine_mode: 'any', minimum_matches: null },
  universe: 'all',
  universeFile: '',
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
  atrPeriod: 14,
  startDate: '',
  endDate: '',
  initialCash: 1_000_000,
  commission: 0.0001,
  minCommission: 0.1,
  stampTax: 0.001,
  slippage: 0,
  fitnessFilterEnabled: false,
  fitnessMinScore: 75,
  fitnessMinTrades: 5,
  fitnessMaxDrawdown: 30,
})
const errors = ref<Record<string, string>>({})
const message = ref('')
const loading = ref(false)
const customParseLoading = ref(false)
const customParseError = ref('')
const showAdvanced = ref(false)
const job = ref<PortfolioBacktestJobState | null>(null)
const result = ref<PortfolioBacktestResult | null>(null)
const history = ref<BacktestHistorySummary[]>([])
const historyLoading = ref(false)
const appliedDiscoveryCandidate = ref<AppliedDiscoveryCandidate | null>(null)
const replayComparison = ref<ReplayComparison | null>(null)

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
const latestState = computed(() => result.value?.states.at(-1) ?? null)
const latestRanking = computed(() => result.value?.ranking_events.at(-1) ?? null)
const sampledEquity = computed(() => {
  const curve = result.value?.equity_curve ?? []
  return curve.length > 60 ? curve.slice(-60) : curve
})
const recentTrades = computed(() => (result.value?.trades ?? []).slice(-100).reverse())
const chartPoints = computed(() => {
  const curve = result.value?.equity_curve ?? []
  if (curve.length < 2) return ''
  const sample = curve.length > 120
    ? curve.filter((_, index) => index % Math.ceil(curve.length / 120) === 0)
    : curve
  const values = sample.map((point) => point.total)
  const minimum = Math.min(...values)
  const maximum = Math.max(...values)
  const span = maximum - minimum || 1
  return sample.map((point, index) => {
    const x = (index / Math.max(sample.length - 1, 1)) * 800
    const y = 220 - ((point.total - minimum) / span) * 190
    return `${x.toFixed(1)},${y.toFixed(1)}`
  }).join(' ')
})
const chartSummary = computed(() => {
  const curve = result.value?.equity_curve ?? []
  if (!curve.length) return '暂无净值数据'
  const first = curve[0].total
  const last = curve[curve.length - 1].total
  return `组合净值从 ${formatMoney(first)} 变为 ${formatMoney(last)}，共 ${curve.length} 个交易日`
})
const canSubmit = computed(() => (
  !loading.value
  && !customParseLoading.value
  && metadata.value !== null
  && (form.mode !== 'custom' || customMetadata.value !== null)
))

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
  if (!form.rankingValue) next.rankingValue = '请选择用于排序的指标输出。'
  if (form.universe === 'custom' && !form.universeFile.trim()) next.universeFile = '请输入自定义股票列表文件。'
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
  if (form.startDate && form.endDate && form.startDate > form.endDate) next.endDate = '结束日期不能早于开始日期。'
  if (!Number.isFinite(form.maxPositions) || !Number.isInteger(form.maxPositions) || form.maxPositions < 1 || form.maxPositions > 100) {
    next.maxPositions = '持仓槽位必须是 1 到 100 的整数。'
  }
  if (!sellRule.signals.length && !sellRule.conditions.length && !form.stopLossEnabled && !form.takeProfitEnabled && !form.trailingStopEnabled) {
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
  if (form.fitnessFilterEnabled) {
    if (!Number.isFinite(form.fitnessMinScore) || form.fitnessMinScore < 0 || form.fitnessMinScore > 100) next.fitnessMinScore = '适配分阈值必须在 0 到 100 之间。'
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
    atr_period: form.atrPeriod,
    sell_value: null,
    sell_value_operator: null,
    sell_value_threshold: null,
    compare_left_value: null,
    compare_operator: null,
    compare_right_value: null,
    sell_rule: sellRule.signals.length || sellRule.conditions.length ? sellRule : null,
    start_date: form.startDate || null,
    end_date: form.endDate || null,
    initial_cash: form.initialCash,
    commission: form.commission,
    min_commission: form.minCommission,
    stamp_tax: form.stampTax,
    slippage: form.slippage,
    execution: form.execution,
    fitness_filter_enabled: form.fitnessFilterEnabled,
    fitness_min_score: form.fitnessMinScore,
    fitness_min_trades: form.fitnessMinTrades,
    fitness_max_drawdown: form.fitnessMaxDrawdown / 100,
  }
}

function apiMessage(error: unknown): string {
  if (error instanceof FormulaScreenApiError) return error.message
  return '组合回测失败，请检查本地 DuckDB 数据和配置后重试。'
}

function finiteNumber(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback
}

function enumValue<T extends string>(value: unknown, allowed: readonly T[], fallback: T): T {
  return typeof value === 'string' && allowed.includes(value as T) ? value as T : fallback
}

function storedRule(value: unknown): RuleGroupPayload | null {
  if (!value || typeof value !== 'object') return null
  const raw = value as Record<string, unknown>
  const rawConditions = Array.isArray(raw.conditions) ? raw.conditions : []
  const conditions = rawConditions.flatMap((item) => {
    if (!item || typeof item !== 'object') return []
    const condition = item as Record<string, unknown>
    const leftValue = typeof condition.left_value === 'string' ? condition.left_value.trim() : ''
    const operator = enumValue(condition.operator, ['gt', 'gte', 'lt', 'lte', 'eq', 'neq', 'cross_up', 'cross_down'] as const, 'gt')
    const rightValue = typeof condition.right_value === 'string' && condition.right_value.trim()
      ? condition.right_value.trim()
      : null
    const rightConstant = typeof condition.right_constant === 'number' && Number.isFinite(condition.right_constant)
      ? condition.right_constant
      : null
    if (!leftValue || (rightValue === null && rightConstant === null) || (rightValue !== null && rightConstant !== null)) return []
    return [{ left_value: leftValue, operator, right_value: rightValue, right_constant: rightConstant }]
  })
  const signals = Array.isArray(raw.signals)
    ? raw.signals.filter((item): item is string => typeof item === 'string' && item.trim().length > 0)
    : []
  if (!signals.length && !conditions.length) return null
  const combineMode = enumValue(raw.combine_mode, ['all', 'any', 'at_least'] as const, 'all')
  const minimumMatches = combineMode === 'at_least'
    ? Math.min(Math.max(Math.trunc(finiteNumber(raw.minimum_matches, 1)), 1), signals.length + conditions.length)
    : null
  return { signals, conditions, combine_mode: combineMode, minimum_matches: minimumMatches }
}

function applyStoredCandidate(raw: unknown): void {
  if (!raw || typeof raw !== 'object') return
  const config = raw as Record<string, unknown>
  const universe = enumValue(config.universe, ['all', 'sh', 'sz', 'custom'] as const, form.universe)
  form.universe = universe
  form.universeFile = typeof config.universe_file === 'string' ? config.universe_file : ''
  form.instrumentTypes = Array.isArray(config.instrument_types)
    ? config.instrument_types.filter((item): item is InstrumentType => INSTRUMENT_TYPE_OPTIONS.some((option) => option.key === item))
    : []
  form.boards = Array.isArray(config.boards)
    ? config.boards.filter((item): item is InstrumentBoard => BOARD_OPTIONS.some((option) => option.key === item))
    : []
  form.startDate = typeof config.start_date === 'string' ? config.start_date : form.startDate
  form.endDate = typeof config.end_date === 'string' ? config.end_date : form.endDate
  const maxPositions = finiteNumber(config.max_positions, form.maxPositions)
  if (Number.isInteger(maxPositions) && maxPositions >= 1 && maxPositions <= 100) form.maxPositions = maxPositions
  if (typeof config.ranking_value === 'string' && config.ranking_value.trim()) form.rankingValue = config.ranking_value
  form.rankOrder = enumValue(config.rank_order, ['asc', 'desc'] as const, form.rankOrder)
  form.rebalanceFrequency = enumValue(config.rebalance_frequency, ['daily', 'weekly', 'monthly'] as const, form.rebalanceFrequency)
  const buyRule = storedRule(config.buy_rule)
  const sellRule = storedRule(config.sell_rule)
  if (buyRule) form.buyRule = buyRule
  if (sellRule) form.sellRule = sellRule

  const applyRisk = (prefix: 'stop_loss' | 'take_profit' | 'trailing_stop', enabledKey: 'stopLossEnabled' | 'takeProfitEnabled' | 'trailingStopEnabled', modeKey: 'stopLossMode' | 'takeProfitMode' | 'trailingStopMode', pctKey: 'stopLossPct' | 'takeProfitPct' | 'trailingStopPct', atrKey: 'stopLossAtrMultiplier' | 'takeProfitAtrMultiplier' | 'trailingStopAtrMultiplier', cooldownKey: 'stopLossCooldownDays' | 'takeProfitCooldownDays' | 'trailingStopCooldownDays'): void => {
    const pct = finiteNumber(config[`${prefix}_pct`], NaN)
    const atr = finiteNumber(config[`${prefix}_atr_multiplier`], NaN)
    const mode = enumValue(config[`${prefix}_mode`], ['pct', 'atr'] as const, Number.isFinite(atr) ? 'atr' : 'pct')
    form[modeKey] = mode
    form[pctKey] = Number.isFinite(pct) ? pct * 100 : form[pctKey]
    form[atrKey] = Number.isFinite(atr) ? atr : form[atrKey]
    form[cooldownKey] = Math.max(0, Math.trunc(finiteNumber(config[`${prefix}_cooldown_days`], 0)))
    form[enabledKey] = Number.isFinite(pct) || Number.isFinite(atr)
  }
  applyRisk('stop_loss', 'stopLossEnabled', 'stopLossMode', 'stopLossPct', 'stopLossAtrMultiplier', 'stopLossCooldownDays')
  applyRisk('take_profit', 'takeProfitEnabled', 'takeProfitMode', 'takeProfitPct', 'takeProfitAtrMultiplier', 'takeProfitCooldownDays')
  applyRisk('trailing_stop', 'trailingStopEnabled', 'trailingStopMode', 'trailingStopPct', 'trailingStopAtrMultiplier', 'trailingStopCooldownDays')
  form.atrPeriod = 14
  form.initialCash = finiteNumber(config.initial_cash, form.initialCash)
  form.commission = finiteNumber(config.commission, form.commission)
  form.minCommission = finiteNumber(config.min_commission, form.minCommission)
  form.stampTax = finiteNumber(config.stamp_tax, form.stampTax)
  form.slippage = finiteNumber(config.slippage, form.slippage)
  form.execution = enumValue(config.execution, ['next_open', 'next_close'] as const, form.execution)
  errors.value = {}
  message.value = '已载入策略实验室候选配置，请确认后开始普通组合回测。'
}

function readAppliedDiscoveryMeta(raw: unknown): AppliedDiscoveryCandidate | null {
  if (!raw || typeof raw !== 'object') return null
  const value = raw as Record<string, unknown>
  if (typeof value.candidate_hash !== 'string' || !value.candidate_hash) return null
  const metrics = value.validation_metrics
  return {
    candidate_hash: value.candidate_hash,
    data_fingerprint: typeof value.data_fingerprint === 'string' ? value.data_fingerprint : null,
    validation_metrics: metrics && typeof metrics === 'object'
      ? metrics as Record<string, number | string | boolean | null>
      : null,
  }
}

const TRADE_FINGERPRINT_FIELDS = [
  'board', 'code', 'commission', 'cooldown_days', 'cost_basis', 'date', 'direction', 'instrument_type', 'market', 'pnl', 'price',
  'reason', 'risk_trigger', 'signal_date', 'size', 'slippage', 'stamp_tax',
] as const

async function tradeFingerprint(trades: PortfolioTrade[]): Promise<string | null> {
  if (!globalThis.crypto?.subtle) return null
  const rows = trades.map((trade) => {
    const source = trade as unknown as Record<string, unknown>
    const row: Record<string, string | null> = {}
    for (const field of TRADE_FINGERPRINT_FIELDS) {
      const value = source[field]
      row[field] = value === null || value === undefined
        ? null
        : typeof value === 'number'
          ? Number.isFinite(value) ? String(value) : null
          : String(value)
    }
    return row
  })
  const bytes = new TextEncoder().encode(JSON.stringify(rows))
  const digest = await globalThis.crypto.subtle.digest('SHA-256', bytes)
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('')
}

async function compareAppliedCandidate(backtest: PortfolioBacktestResult): Promise<void> {
  const applied = appliedDiscoveryCandidate.value
  if (!applied) return
  const expected = applied.validation_metrics
  const metricKeys = ['total_return', 'annual_return', 'sharpe', 'max_drawdown', 'total_trades', 'win_rate', 'turnover']
  const comparable = metricKeys.filter((key) => typeof expected?.[key] === 'number' && typeof backtest.performance[key] === 'number')
  const performanceMatches = comparable.length > 0 && comparable.every((key) => {
    const expectedValue = expected?.[key]
    const actualValue = backtest.performance[key]
    return typeof expectedValue === 'number' && typeof actualValue === 'number'
      && Math.abs(expectedValue - actualValue) <= 1e-9
  })
  const actualTradeFingerprint = await tradeFingerprint(backtest.trades)
  replayComparison.value = {
    candidateHash: applied.candidate_hash,
    tradeRecords: applied.validation_metrics?.trade_fingerprint && actualTradeFingerprint
      ? applied.validation_metrics.trade_fingerprint === actualTradeFingerprint ? '一致' : '不一致'
      : '未提供',
    performance: performanceMatches ? '一致' : comparable.length ? '不一致' : '未提供',
    dataFingerprint: applied.data_fingerprint && backtest.data_fingerprint
      ? applied.data_fingerprint === backtest.data_fingerprint ? '一致' : '不一致'
      : '未提供',
  }
}

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
    for (;;) {
      const state = await getPortfolioBacktest(created.job_id)
      job.value = state
      if (state.status === 'completed') {
        result.value = await getPortfolioBacktestResults(created.job_id)
        await compareAppliedCandidate(result.value)
        message.value = `组合回测完成：${result.value.trades.length} 笔成交，区间 ${result.value.start_date} 至 ${result.value.end_date}。`
        await loadHistory()
        return
      }
      if (state.status === 'failed') {
        message.value = state.error ?? '组合回测任务失败，请检查配置和服务状态后重试。'
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
    history.value = await fetchPortfolioBacktestHistory()
  } catch (error) {
    message.value = apiMessage(error)
  } finally {
    historyLoading.value = false
  }
}

async function loadHistoryDetail(historyId: string): Promise<void> {
  try {
    const loaded = await getPortfolioBacktestHistory(historyId)
    result.value = loaded.result
    replayComparison.value = null
    message.value = `已加载历史组合回测 ${historyId}。`
  } catch (error) {
    message.value = apiMessage(error)
  }
}

async function removeHistory(historyId: string): Promise<void> {
  try {
    await deletePortfolioBacktestHistory(historyId)
    await loadHistory()
    message.value = '历史组合回测已删除。'
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

function valueName(id: string): string {
  return availableValues.value.find((value) => value.id === id)?.display_name ?? id
}

function performanceValue(key: string): number | null {
  return result.value?.performance[key] ?? null
}

function downloadResult(): void {
  if (!result.value) return
  const url = URL.createObjectURL(new Blob([JSON.stringify(result.value, null, 2)], { type: 'application/json' }))
  const link = document.createElement('a')
  link.href = url
  link.download = 'portfolio-backtest.json'
  link.click()
  window.setTimeout(() => URL.revokeObjectURL(url), 0)
}

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
  <div class="screen-page portfolio-page" data-testid="portfolio-backtest-page">
    <header class="topbar">
      <a class="brand" href="/indicator-lab" aria-label="指标实验室首页">
        <img class="brand-mark" src="/indicator-lab-mark.png" alt="" aria-hidden="true" width="34" height="34">
        <span><strong>Indicator Lab</strong><small>指标实验室</small></span>
      </a>
      <nav aria-label="主导航">
        <a class="nav-link" href="/indicator-lab">公式选股</a>
        <a class="nav-link" href="/backtest">单股回测</a>
        <a class="nav-link active" href="/portfolio-backtest" aria-current="page">组合回测</a>
        <a class="nav-link" href="/strategy-lab">策略实验室</a>
        <a class="nav-link" href="/strategy-fitness">策略适配性</a>
        <a class="nav-link" href="/market-data">本地行情</a>
      </nav>
      <div class="topbar-status"><span class="status-dot" aria-hidden="true"></span> 本地数据模式</div>
    </header>

    <main class="content-shell">
      <section class="page-intro">
        <div>
          <p class="eyebrow">RANKED PORTFOLIO / DAILY</p>
          <h1>动态组合回测</h1>
          <p class="intro-copy">按买入条件产生候选并进行指标排名，卖出后自动用排名靠前的候选补回空出的槽位。</p>
        </div>
        <div class="intro-note"><span class="note-label">默认流程</span><strong>收盘信号 · 次日成交</strong><small>固定槽位 · 每槽等额 · 支持止盈止损与指标卖出</small></div>
      </section>

      <div v-if="message" class="notice" :class="{ error: !message.includes('完成') }" role="alert" data-testid="portfolio-message">{{ message }}</div>

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
              <div><label for="portfolio-universe">品种范围</label><select id="portfolio-universe" v-model="form.universe" data-testid="portfolio-universe"><option value="all">沪深全部品种</option><option value="sh">仅上海</option><option value="sz">仅深圳</option><option value="custom">自定义列表</option></select></div>
              <div><label for="portfolio-max-positions">持仓槽位</label><input id="portfolio-max-positions" v-model.number="form.maxPositions" data-testid="portfolio-max-positions" type="number" min="1" max="100" step="1"></div>
            </div>
            <div v-if="form.universe === 'custom'" class="portfolio-inline-field"><label for="portfolio-universe-file">股票列表文件</label><input id="portfolio-universe-file" v-model="form.universeFile" data-testid="portfolio-universe-file" type="text" placeholder="每行 SH 600000 或 SZ 000001"><p v-if="errors.universeFile" class="field-error">{{ errors.universeFile }}</p></div>
            <div class="scope-config inline-scope-config" data-testid="portfolio-scope">
              <div class="scope-options"><span>证券类型</span><label v-for="option in INSTRUMENT_TYPE_OPTIONS" :key="option.key"><input type="checkbox" :data-testid="`portfolio-scope-type-${option.key}`" :checked="form.instrumentTypes.includes(option.key)" @change="toggleInstrumentType(option.key)">{{ option.label }}</label></div>
              <div class="scope-options"><span>板块</span><label v-for="option in BOARD_OPTIONS" :key="option.key"><input type="checkbox" :data-testid="`portfolio-scope-board-${option.key}`" :checked="form.boards.includes(option.key)" @change="toggleBoard(option.key)">{{ option.label }}</label></div>
              <small class="scope-helper">未勾选表示全部；类型和板块同时选择时取交集。</small>
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
            <legend>适配性过滤（可选）</legend>
            <label class="filter-toggle"><input v-model="form.fitnessFilterEnabled" data-testid="portfolio-fitness-enabled" type="checkbox"><span>只使用历史适配分达标的标的</span></label>
            <div v-if="form.fitnessFilterEnabled" class="compact-fields portfolio-fields-top">
              <div><label for="portfolio-fitness-score">最低适配分</label><input id="portfolio-fitness-score" v-model.number="form.fitnessMinScore" data-testid="portfolio-fitness-score" type="number" min="0" max="100" step="5"></div>
              <div><label for="portfolio-fitness-trades">历史最少成交</label><input id="portfolio-fitness-trades" v-model.number="form.fitnessMinTrades" data-testid="portfolio-fitness-trades" type="number" min="1" step="1"></div>
            </div>
            <div v-if="form.fitnessFilterEnabled" class="portfolio-inline-field"><label for="portfolio-fitness-drawdown">历史最大回撤上限 (%)</label><input id="portfolio-fitness-drawdown" v-model.number="form.fitnessMaxDrawdown" data-testid="portfolio-fitness-drawdown" type="number" min="0" max="100" step="5"></div>
            <p v-if="errors.fitnessMinScore" class="field-error">{{ errors.fitnessMinScore }}</p>
            <p v-if="errors.fitnessMinTrades" class="field-error">{{ errors.fitnessMinTrades }}</p>
            <p v-if="errors.fitnessMaxDrawdown" class="field-error">{{ errors.fitnessMaxDrawdown }}</p>
            <p class="helper">启用后，历史每个调仓日只使用更早日期的成交和净值计算适配分；评估样本不足的标的不参与补位。</p>
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
            <p v-if="errors.sellRules" class="field-error">{{ errors.sellRules }}</p>
            <p v-if="errors.stopLossPct || errors.stopLossAtrMultiplier || errors.stopLossCooldownDays" class="field-error">{{ errors.stopLossPct || errors.stopLossAtrMultiplier || errors.stopLossCooldownDays }}</p>
            <p v-if="errors.takeProfitPct || errors.takeProfitAtrMultiplier || errors.takeProfitCooldownDays" class="field-error">{{ errors.takeProfitPct || errors.takeProfitAtrMultiplier || errors.takeProfitCooldownDays }}</p>
            <p v-if="errors.trailingStopPct || errors.trailingStopAtrMultiplier || errors.trailingStopCooldownDays" class="field-error">{{ errors.trailingStopPct || errors.trailingStopAtrMultiplier || errors.trailingStopCooldownDays }}</p>
            <p class="helper">指标规则在收盘确认，默认下一根 K 线执行；风险控制按当日最高/最低价触发，ATR 周期固定为 {{ form.atrPeriod }}，各类冷却独立计算。</p>
          </fieldset>

          <button class="advanced-toggle" data-testid="portfolio-advanced-toggle" type="button" :aria-expanded="showAdvanced" @click="showAdvanced = !showAdvanced"><span>{{ showAdvanced ? '收起高级设置' : '高级设置' }}</span><small>日期 · 资金 · 费用</small></button>
          <div v-if="showAdvanced" class="advanced-settings" data-testid="portfolio-advanced-settings">
            <fieldset class="form-section compact-fields"><div><label for="portfolio-start">开始日期</label><input id="portfolio-start" v-model="form.startDate" data-testid="portfolio-start" type="date"></div><div><label for="portfolio-end">结束日期</label><input id="portfolio-end" v-model="form.endDate" data-testid="portfolio-end" type="date"></div></fieldset>
            <p v-if="errors.endDate" class="field-error">{{ errors.endDate }}</p>
            <fieldset class="form-section compact-fields"><div><label for="portfolio-initial-cash">初始资金</label><input id="portfolio-initial-cash" v-model.number="form.initialCash" data-testid="portfolio-initial-cash" type="number" min="1" step="10000"></div><div><label for="portfolio-execution">成交方式</label><select id="portfolio-execution" v-model="form.execution" data-testid="portfolio-execution"><option value="next_open">下一根开盘</option><option value="next_close">下一根收盘</option></select></div></fieldset>
            <p v-if="errors.initialCash" class="field-error">{{ errors.initialCash }}</p>
            <fieldset class="form-section compact-fields"><div><label for="portfolio-commission">佣金费率</label><input id="portfolio-commission" v-model.number="form.commission" data-testid="portfolio-commission" type="number" min="0" max="0.1" step="0.0001"></div><div><label for="portfolio-min-commission">最低佣金</label><input id="portfolio-min-commission" v-model.number="form.minCommission" data-testid="portfolio-min-commission" type="number" min="0" step="0.01"></div></fieldset>
            <fieldset class="form-section compact-fields"><div><label for="portfolio-stamp-tax">印花税率</label><input id="portfolio-stamp-tax" v-model.number="form.stampTax" data-testid="portfolio-stamp-tax" type="number" min="0" max="0.1" step="0.0001"></div><div><label for="portfolio-slippage">每股滑点（元）</label><input id="portfolio-slippage" v-model.number="form.slippage" data-testid="portfolio-slippage" type="number" min="0" step="0.001"></div></fieldset>
          </div>

          <p class="helper">日期留空（默认）表示使用股票池全部本地历史数据，区间边界包含当天；展开高级设置后可以手动填写起止日期。</p>

          <div class="action-stack"><button class="primary-button" data-testid="start-portfolio-backtest" type="button" :disabled="!canSubmit" @click="submit"><span v-if="loading" class="spinner" aria-hidden="true"></span>{{ loading ? '组合回测中…' : '开始组合回测' }}</button></div>
          <p class="privacy-note">计算在本机完成；结果基于本地日线，不代表未来收益。</p>
        </form>

        <section class="results-panel panel" data-testid="portfolio-results" aria-live="polite">
          <div class="panel-heading results-heading"><div><span class="section-kicker">02 / RESULTS</span><h2>组合结果</h2></div><button v-if="result" class="secondary-button export-backtest" type="button" data-testid="export-portfolio-backtest" @click="downloadResult">导出 JSON</button></div>
          <div v-if="!result" class="empty-state" data-testid="portfolio-empty"><div class="empty-icon" aria-hidden="true">/</div><h3>{{ loading ? '正在运行动态组合回测…' : '还没有组合结果' }}</h3><p>{{ loading ? '正在逐股计算公式并模拟槽位补位，完成后显示净值与候选排名。' : '配置买入条件、排序指标和卖出条件后开始回测。' }}</p></div>
          <template v-else>
            <div class="backtest-summary"><span class="result-badge">{{ result.universe === 'all' ? '沪深全部品种' : result.universe.toUpperCase() }}</span><span>{{ result.start_date }} → {{ result.end_date }}</span><span>{{ result.bars }} 个交易日</span><span>{{ result.processed }} / {{ result.total_candidates }} 个标的已处理</span><span>槽位 {{ result.max_positions }}</span><span>排序：{{ valueName(result.ranking_value) }} · {{ result.rank_order === 'desc' ? '高到低' : '低到高' }}</span><span v-if="result.fitness_filter_enabled" class="result-badge">适配分 ≥ {{ result.fitness_min_score }}</span></div>
            <div v-if="replayComparison" class="replay-comparison" data-testid="portfolio-replay-comparison"><strong>候选复核 {{ replayComparison.candidateHash.slice(0, 12) }}</strong><span>成交记录：{{ replayComparison.tradeRecords }}</span><span>绩效摘要：{{ replayComparison.performance }}</span><span>数据指纹：{{ replayComparison.dataFingerprint }}</span></div>
            <div class="metrics-strip portfolio-metrics">
              <div><span>总收益</span><strong :class="{ positive: (performanceValue('total_return') ?? 0) >= 0, negative: (performanceValue('total_return') ?? 0) < 0 }" data-testid="portfolio-total-return">{{ formatPercent(performanceValue('total_return')) }}</strong></div>
              <div><span>最大回撤</span><strong class="negative">{{ formatPercent(performanceValue('max_drawdown')) }}</strong></div>
              <div><span>夏普</span><strong>{{ formatNumber(performanceValue('sharpe')) }}</strong></div>
              <div><span>成交笔数</span><strong>{{ formatNumber(performanceValue('total_trades'), 0) }}</strong></div>
              <div><span>胜率</span><strong>{{ formatPercent(performanceValue('win_rate')) }}</strong></div>
              <div><span>期末资产</span><strong>{{ formatMoney(performanceValue('end_value')) }}</strong></div>
            </div>
            <div class="progress-track portfolio-progress" aria-label="组合数据处理进度"><span :style="{ width: `${Math.round((job?.progress ?? 1) * 100)}%` }"></span></div>

            <section class="chart-card" aria-labelledby="portfolio-equity-title"><div class="chart-heading"><div><span class="section-kicker">EQUITY CURVE</span><h3 id="portfolio-equity-title">组合资金曲线</h3></div><span class="chart-legend"><i aria-hidden="true"></i>总资产</span></div><svg class="equity-chart" data-testid="portfolio-equity-chart" viewBox="0 0 800 240" role="img" :aria-label="chartSummary"><polyline :points="chartPoints" fill="none" stroke="var(--accent)" stroke-linecap="round" stroke-linejoin="round" stroke-width="3"></polyline></svg><p class="chart-summary">{{ chartSummary }}</p></section>

            <section class="data-section"><div class="section-heading"><div><span class="section-kicker">CURRENT HOLDINGS</span><h3>最新持仓</h3></div><span>{{ latestState?.positions_count ?? 0 }} / {{ result.max_positions }} 个槽位</span></div><div v-if="!latestState?.holdings.length" class="inline-empty">回测结束时没有持仓。</div><div v-else class="table-wrap"><table data-testid="portfolio-holdings"><caption class="sr-only">组合最新持仓</caption><thead><tr><th>市场</th><th>代码</th><th>中文名</th><th>数量</th><th>成本</th><th>收盘</th><th>浮动盈亏</th></tr></thead><tbody><tr v-for="holding in latestState.holdings" :key="`${holding.market}-${holding.code}`"><td><span class="market-pill" :class="holding.market.toLowerCase()">{{ holding.market }}</span></td><td><strong class="code">{{ holding.code }}</strong></td><td>{{ holding.name || '—' }}</td><td>{{ formatNumber(holding.size, 0) }}</td><td>{{ formatMoney(holding.entry_price) }}</td><td>{{ formatMoney(holding.close) }}</td><td :class="{ positive: holding.unrealized_pnl >= 0, negative: holding.unrealized_pnl < 0 }">{{ formatMoney(holding.unrealized_pnl) }}</td></tr></tbody></table></div></section>

            <section class="data-section"><div class="section-heading"><div><span class="section-kicker">TRADES</span><h3>成交记录</h3></div><span>最近 {{ recentTrades.length }} 笔</span></div><div v-if="!recentTrades.length" class="inline-empty">区间内没有形成可执行成交。</div><div v-else class="table-wrap"><table data-testid="portfolio-trades"><caption class="sr-only">组合成交记录</caption><thead><tr><th>成交日期</th><th>标的</th><th>方向</th><th>数量</th><th>价格</th><th>盈亏</th><th>原因</th></tr></thead><tbody><tr v-for="(trade, index) in recentTrades" :key="`${trade.date}-${trade.code}-${index}`"><td class="muted-code">{{ trade.date }}</td><td><span v-if="trade.name">{{ trade.name }} · </span><span class="market-pill" :class="trade.market.toLowerCase()">{{ trade.market }}</span> <strong class="code">{{ trade.code }}</strong></td><td><span class="direction" :class="trade.direction.toLowerCase()">{{ trade.direction === 'BUY' ? '买入' : '卖出' }}</span></td><td>{{ formatNumber(trade.size, 0) }}</td><td>{{ formatMoney(trade.price) }}</td><td :class="{ positive: trade.pnl >= 0, negative: trade.pnl < 0 }">{{ formatMoney(trade.pnl) }}</td><td>{{ trade.reason }}</td></tr></tbody></table></div></section>

            <section class="data-section"><div class="section-heading"><div><span class="section-kicker">LATEST RANKING</span><h3>最近候选排名</h3></div><span>{{ latestRanking?.date ?? '—' }}</span></div><div v-if="!latestRanking?.candidates.length" class="inline-empty">没有可展示的排名候选。</div><div v-else class="table-wrap"><table data-testid="portfolio-ranking"><caption class="sr-only">最近候选排名</caption><thead><tr><th>排名</th><th>标的</th><th>当前指标</th><th>适配性</th><th>动作</th></tr></thead><tbody><tr v-for="candidate in latestRanking.candidates" :key="`${candidate.market}-${candidate.code}`"><td class="muted-code">{{ candidate.rank }}</td><td><span v-if="candidate.name">{{ candidate.name }} · </span><span class="market-pill" :class="candidate.market.toLowerCase()">{{ candidate.market }}</span> <strong class="code">{{ candidate.code }}</strong></td><td>{{ formatNumber(candidate.score, 4) }}</td><td v-if="candidate.fitness_score !== undefined"><strong>{{ formatNumber(candidate.fitness_score, 2) }}</strong><small class="muted-code"> · {{ candidate.fitness_trades ?? 0 }} 笔</small></td><td v-else class="muted-code">未启用</td><td><span v-if="candidate.excluded_reason" class="negative">{{ candidate.excluded_reason }}</span><span class="result-badge" v-else-if="candidate.selected">进入候选槽位</span><span class="muted-code" v-else>等待补位</span></td></tr></tbody></table></div></section>

            <section class="data-section"><div class="section-heading"><div><span class="section-kicker">RECENT EQUITY</span><h3>最近净值</h3></div><span>最近 {{ sampledEquity.length }} 日</span></div><div class="table-wrap"><table data-testid="portfolio-equity-table"><caption class="sr-only">组合最近资金曲线数据</caption><thead><tr><th>日期</th><th>现金</th><th>持仓市值</th><th>总资产</th><th>持仓数</th><th>回撤</th></tr></thead><tbody><tr v-for="point in sampledEquity" :key="point.date"><td class="muted-code">{{ point.date }}</td><td>{{ formatMoney(point.cash) }}</td><td>{{ formatMoney(point.position_value) }}</td><td><strong>{{ formatMoney(point.total) }}</strong></td><td>{{ point.positions_count }}</td><td>{{ formatPercent(point.drawdown_pct) }}</td></tr></tbody></table></div></section>
            <div v-if="result.failure_reasons && Object.keys(result.failure_reasons).length" class="failure-summary"><strong>失败摘要</strong><span v-for="(count, reason) in result.failure_reasons" :key="reason">{{ reason }} · {{ count }}</span></div>
            <p v-if="result.diagnostic" class="diagnostic" data-testid="portfolio-diagnostic">{{ result.diagnostic }}</p>
          </template>
        </section>
      </div>

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
