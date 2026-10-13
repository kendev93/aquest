<script setup lang="ts">
import PageGuide from '../components/PageGuide.vue'
import { computed, nextTick, onActivated, onMounted, reactive, ref } from 'vue'

import {
  cancelConditionResearch,
  createConditionResearch,
  deleteConditionResearchHistory,
  fetchConditionResearchHistory,
  getConditionResearch,
  getConditionResearchHistory,
  getConditionResearchResults,
} from '../api/conditionResearch'
import { fetchMetadata, parseFormula } from '../api/formulaScreen'
import { ApiError } from '../api/client'
import { fetchActiveJobs } from '../api/jobs'
import DateRangeSelector from '../components/DateRangeSelector.vue'
import RuleGroupEditor from '../components/RuleGroupEditor.vue'
import TermHint from '../components/TermHint.vue'
import ConditionHistoryPanel from '../components/ConditionHistoryPanel.vue'
import ConditionRankingAdvice from '../components/ConditionRankingAdvice.vue'
import { useResearchPresets } from '../composables/useResearchPresets'
import { useConditionResearchJob } from '../composables/useConditionResearchJob'
import ConditionRollingTable from '../components/ConditionRollingTable.vue'
import ConditionSensitivityTable from '../components/ConditionSensitivityTable.vue'
import ConditionStockDetail from '../components/ConditionStockDetail.vue'
import type {
  BacktestExecution,
  ConditionResearchCandidate,
  ConditionResearchJobState,
  ConditionResearchPayload,
  ConditionResearchRanking,
  ConditionResearchReport,
  CustomFormulaMetadata,
  DateRangeMode,
  DateRangeSelection,
  FormulaScreenMetadata,
  InstrumentBoard,
  InstrumentType,
  RankOrder,
  RuleGroupPayload,
  ScreenCondition,
  ValueDefinition,
} from '../types'
import { useCandidateSelection } from '../composables/useCandidateSelection'
import { parseStoredRuleGroup } from '../utils/formulaScreen'
import { consumeSwingPlan } from '../utils/swingHandoff'
import {
  STOCK_BOARD_OPTIONS,
  conditionHitRate as formatConditionHitRate,
  constraintText,
  duplicateSummaryText,
  duplicateTitle as formatDuplicateTitle,
  formatConditionText,
  formatNumber,
  formatPercent,
  hitRateTitle,
  mergedDuplicates,
  phaseCount,
  significanceLabel,
  significanceTitle as formatSignificanceTitle,
  significanceSummaryText,
  topNTitle as formatTopNTitle,
  topNWinRate,
  trialSummaryText,
  validationTrades as formatValidationTrades,
} from '../utils/conditionResearch'
import type { ResearchHistorySummary } from '../utils/conditionResearch'
import AppTopbar from '../components/AppTopbar.vue'

function emptyRule(): RuleGroupPayload {
  return { signals: [], conditions: [], required_conditions: [], combine_mode: 'all', minimum_matches: null }
}

const metadata = ref<FormulaScreenMetadata | null>(null)
const customMetadata = ref<CustomFormulaMetadata | null>(null)
const parsedFormulaText = ref('')
const mode = ref<'preset' | 'custom'>('preset')
const form = reactive({
  universe: 'all' as 'all' | 'sh' | 'sz',
  instrumentType: 'stock' as InstrumentType,
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
  // 同义条件合并门槛：0.6 会合并同一指标相邻分位的阈值（实测相关性 0.61~0.67），
  // 相隔一个分位的（0.41）保留为独立候选。
  duplicateCorrelation: 0.6,
  holdoutCandidates: 1,
  walkForwardWindows: 1,
  // 0 = 不做检验。默认 200 次：20 次时名义 5% 的门槛实测误报 11.7%，
  // 200 次回落到 3.3%（后端默认值已同步为 200，前端不能把它覆盖回 20）。
  significancePermutations: 200,
  initialCash: 100_000,
  commission: 0.0001,
  minCommission: 0.1,
  stampTax: 0.001,
  slippage: 0,
  execution: 'next_open' as BacktestExecution,
})
const errors = ref<Record<string, string>>({})
const message = ref('')
// loading 由 useConditionResearchJob 持有（任务执行期间为真）。
const saving = ref(false)
const customParseLoading = ref(false)
const customParseError = ref('')
const showAdvanced = ref(false)

/** 历史列表：后端已按完成时间由近到远排好，这里只管整体刷新。 */
async function loadHistory(): Promise<void> {
  historyLoading.value = true
  try {
    history.value = await fetchConditionResearchHistory() as ResearchHistorySummary[]
  } catch (error) {
    message.value = apiMessage(error)
  } finally {
    historyLoading.value = false
  }
}
const history = ref<ResearchHistorySummary[]>([])
const historyLoading = ref(false)
// 判断方案里的条件是否失效要看当前元数据里到底有哪些输出/信号。元数据还没到
// 的时候一律不判——那时没有依据，宁可什么都不说。
const presetAvailability = computed(() => ({
  valueIds: availableValues.value.map((value) => value.id),
  // 自定义公式的买卖输出只存在于 customMetadata，预置元数据里没有。只按预置
  // 元数据判失效的话，一个自定义方案的每个信号都会被报成「引用了不存在的
  // 信号」——它明明在，只是还没解析出来。
  signalIds: mode.value === 'custom'
    ? (customMetadata.value?.signals ?? []).map((signal) => signal.id)
    : (metadata.value?.indicators ?? []).flatMap(
      (indicator) => (indicator.signals ?? []).map((signal) => signal.id),
    ),
}))

// 任务执行（提交/轮询/取消/恢复）在 composables/useConditionResearchJob；
// 完成后的收尾（重置结果视图、清旧勾选、刷新历史并定位新记录）写的是页面状态，在这里注入。
const { job, report, loading, submit, cancel, resumeActiveJob } = useConditionResearchJob({
  validate,
  errors,
  buildPayload,
  message,
  describeError: apiMessage,
  onReportReady: async (jobId) => {
    selectedStockView.value = 'baseline:full'
    // 旧候选 id 已经不属于这份结果，勾选状态一并作废。
    clearCandidateSelection()
    await loadHistory()
    // 新记录排在最前面。回到第一页并把它标成当前，否则页面还停在第二页，
    // 看起来就像是这次研究没被保存。
    historyPage.value = 1
    if (history.value.some((item) => item.history_id === jobId)) {
      activeHistoryId.value = jobId
    }
  },
})

// 研究预设的状态与动作（保存/加载/覆盖/重命名/删除）在 composables/useResearchPresets。
const {
  researchPresets,
  selectedPresetId,
  presetName,
  presetBusy,
  refreshResearchPresets,
  loadSelectedPreset,
  quickResearch,
  savePresetAsNew,
  overwritePreset,
  renamePreset,
  deleteSelectedPreset,
} = useResearchPresets({
  buildPayload,
  applyStoredRequest,
  submit,
  message,
  customParseError,
  presetAvailability,
  describeError: apiMessage,
})
const selectedStockView = ref('baseline:full')
// 历史列表：后端已按完成时间由近到远排好，这里只管切片分页。
const activeHistoryId = ref('')
const historyPage = ref(1)
// 勾选集合由 useCandidateSelection 持有，见下面的接线。
// 排序规则：研究不建组合，但结论要落到组合回测的「候选池 + 有限槽位」模型上。
const rankingValue = ref('')
const rankingOrder = ref<RankOrder>('desc')
const rankingTopN = ref(5)
// 条件研究页 → 组合回测页的桥接，走独立的键，避免和策略实验室的回填互相覆盖。
const RESEARCH_APPLY_KEY = 'aquest.condition-research.apply'
// 候选库 → 条件研究页：「载入复测」把来源研究的整份输入搬过来还原表单。
const RESTORE_KEY = 'aquest.condition-research.restore'
// 条件研究页 → 公式选股页：只搬买入侧的条件（选股是一次性扫描，没有卖出这一说）。
const RESEARCH_TO_SCREEN_KEY = 'aquest.condition-research.to-screen'
// 「持有到期末」：不设基础卖出条件，让研究以「一直持有」为基准去搜索卖点。
const holdToEnd = ref(false)
// 「持续满仓」：持有到期末的对偶。不设基础买入条件，让研究以「天天买」为基准去搜索买点。
const alwaysInMarket = ref(false)
// 正在把历史记录回填进表单：此时不要让它触发的 watch 改写费用或板块。
const restoring = ref(false)
const availableValues = computed<ValueDefinition[]>(() => (
  mode.value === 'custom'
    ? customMetadata.value?.values ?? []
    : metadata.value?.indicators.flatMap((indicator) => indicator.values ?? []) ?? []
))
// 排序下拉只列能跨股票比较的输出：按收盘价从高到低排序只是「谁股价贵谁排前面」。
const rankableValues = computed<ValueDefinition[]>(() => (
  availableValues.value.filter((value) => value.rankable === true)
))
// 展示口径的实现在 utils/conditionResearch.ts（纯函数），这里只补上页面上下文：
// 候选名要看当前可选输出，命中率和门槛类要看当前报告、检验次数看表单。
function conditionText(candidate: ConditionResearchCandidate): string {
  return formatConditionText(availableValues.value, candidate)
}

function duplicateTitle(candidate: ConditionResearchCandidate): string {
  return formatDuplicateTitle(availableValues.value, candidate)
}

function conditionHitRate(candidate: ConditionResearchCandidate): string {
  return formatConditionHitRate(report.value, candidate)
}

function topNTitle(candidate: ConditionResearchCandidate): string {
  return formatTopNTitle(report.value, candidate)
}

function significanceTitle(candidate: ConditionResearchCandidate): string {
  return formatSignificanceTitle(candidate, form.significancePermutations, report.value?.trials)
}

function validationTrades(candidate: ConditionResearchCandidate): string {
  return formatValidationTrades(report.value, candidate)
}

const rankingReport = computed<ConditionResearchRanking | null>(() => (
  report.value?.ranking ?? null
))
const rankingRecommendation = computed(() => rankingReport.value?.recommended ?? null)
/** 用户没指定排序指标时，「应用到组合回测」就用研究推荐的那一个。 */
const effectiveRankingValue = computed(() => (
  rankingValue.value || (rankingRecommendation.value?.value_id ?? '')
))
const effectiveRankOrder = computed<RankOrder>(() => (
  rankingValue.value ? rankingOrder.value : (rankingRecommendation.value?.order ?? rankingOrder.value)
))
/** 日均命中股票数 vs 槽位数：判断这个条件在组合里到底还有没有话语权。 */
const slotHint = computed<string | null>(() => {
  const totals = report.value?.sample_totals?.buy
  const daily = totals?.daily_hits
  if (daily === null || daily === undefined) return null
  const slots = rankingTopN.value
  const hits = daily.toFixed(1)
  if (daily <= slots) {
    return `日均命中 ${hits} 只，不超过 ${slots} 个槽位：买谁基本由这个条件说了算，排序只是补位时的兜底。`
  }
  if (daily >= slots * 10) {
    const ratio = Math.round(daily / slots)
    return `日均命中 ${hits} 只，是 ${slots} 个槽位的约 ${ratio} 倍：真正决定买谁的是排序规则，这个条件只负责筛掉不合格的。`
  }
  return `日均命中 ${hits} 只、槽位 ${slots} 个：条件和排序共同决定结果，两边都值得调。`
})
const dateRange = computed<DateRangeSelection>({
  get: () => ({ mode: form.dateMode, startDate: form.startDate, endDate: form.endDate }),
  set: (value) => {
    form.dateMode = value.mode
    form.startDate = value.startDate
    form.endDate = value.endDate
  },
})
const scopeNoun = computed(() => '股票')
const scopeHelper = computed(() => (
  '每只股票独立运行同一套规则，不设置跨股票资金分配或持仓数量上限。'
))

// A bare `> 0` / `< 0` pair is only meaningful for an output that crosses zero.
// Most catalog outputs are one-sided (indicator_one.wash is 0 or positive), and
// the previous default of `availableValues[0] < 0` could never fire at all.
const BIDIRECTIONAL_PREFERENCE = [
  'technical.macd_histogram',
  'technical.macd',
  'technical.macd_signal',
]

function defaultSellOperand(): ValueDefinition | undefined {
  const values = availableValues.value
  for (const id of BIDIRECTIONAL_PREFERENCE) {
    const preferred = values.find((value) => value.id === id)
    if (preferred) return preferred
  }
  return values.find((value) => value.bidirectional === true)
}

const noBidirectionalValue = computed(() => availableValues.value.length > 0 && !defaultSellOperand())

// 自定义公式里保持价格量纲的输出会被后端排除在挖掘特征之外，这里同步说明。
const priceScaleOutputs = computed(() => (
  mode.value === 'custom'
    ? (customMetadata.value?.values ?? []).filter((value) => value.price_scale === true)
    : []
))

function condition(
  leftValue: string,
  operator: 'gt' | 'lt' | 'gte' | 'lte',
): RuleGroupPayload['conditions'][number] {
  return { left_value: leftValue, operator, right_value: null, right_constant: 0 }
}

function initialRule(side: 'buy' | 'sell'): RuleGroupPayload {
  const bidirectional = defaultSellOperand()
  const fallback = availableValues.value[0]?.id ?? ''
  const operand = bidirectional?.id ?? fallback
  if (!operand) return emptyRule()
  if (bidirectional) {
    // Zero-centred operand: buy above zero, sell below zero, both reachable.
    return { ...emptyRule(), conditions: [condition(operand, side === 'buy' ? 'gt' : 'lt')] }
  }
  // One-sided operand: a strict mirror is unsatisfiable, so sell uses the
  // complementary operator to keep both sides able to produce trades.
  return { ...emptyRule(), conditions: [condition(operand, side === 'buy' ? 'gt' : 'lte')] }
}

function resetRules(): void {
  form.buyRule = initialRule('buy')
  form.sellRule = initialRule('sell')
  holdToEnd.value = false
  alwaysInMarket.value = false
}

function setHoldToEnd(next: boolean): void {
  holdToEnd.value = next
  // 关掉开关后必须有卖出条件可填，否则提交会被拦下来；回到默认写法最省事。
  if (!next && !form.sellRule.conditions.length) form.sellRule = initialRule('sell')
}

function setAlwaysInMarket(next: boolean): void {
  alwaysInMarket.value = next
  // 同理：关掉开关后买入条件不能是空的。
  if (!next && !form.buyRule.conditions.length) form.buyRule = initialRule('buy')
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
    form.buyRule = initialRule('buy')
    form.sellRule = initialRule('sell')
    errors.value = {}
  } catch (error) {
    customMetadata.value = null
    form.buyRule = emptyRule()
    form.sellRule = emptyRule()
    customParseError.value = error instanceof ApiError
      ? error.message
      : '公式解析失败，请检查语法。'
  } finally {
    customParseLoading.value = false
  }
}

function validate(): Record<string, string> {
  const next: Record<string, string> = {}
  if (!alwaysInMarket.value && !form.buyRule.conditions.length) {
    next.buyRule = '请至少设置一个买入指标条件，或勾选「持续满仓」。'
  }
  if (!holdToEnd.value && !form.sellRule.conditions.length) {
    next.sellRule = '请至少设置一个卖出指标条件，或勾选「持有到期末」。'
  }
  if (alwaysInMarket.value && holdToEnd.value) {
    next.buyRule = '买入和卖出不能同时留空，至少要保留一侧条件作为研究基准。'
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
  if (!Number.isFinite(form.duplicateCorrelation) || form.duplicateCorrelation < 0.5 || form.duplicateCorrelation > 1) {
    next.duplicateCorrelation = '同义条件合并门槛必须在 0.5 到 1 之间。'
  }
  if (!Number.isInteger(form.holdoutCandidates) || form.holdoutCandidates < 1 || form.holdoutCandidates > 3) {
    next.holdoutCandidates = '最终留出候选数必须在 1 到 3 之间。'
  }
  if (!Number.isInteger(form.walkForwardWindows) || form.walkForwardWindows < 1 || form.walkForwardWindows > 5) {
    next.walkForwardWindows = '滚动复验窗口数必须在 1 到 5 之间。'
  }
  if (
    !Number.isInteger(form.significancePermutations)
    || form.significancePermutations < 0
    || form.significancePermutations > 500
  ) {
    next.significancePermutations = '噪声门槛检验次数必须在 0 到 500 之间（0 表示不检验）。'
  }
  return next
}

function buildPayload(): ConditionResearchPayload {
  return {
    universe: form.universe,
    instrument_types: [form.instrumentType] as ConditionResearchPayload['instrument_types'],
    ...(form.boards.length ? { boards: [...form.boards] as ConditionResearchPayload['boards'] } : {}),
    // null 表示「不设基础买入条件」，后端据此按持续满仓处理。
    buy_rule: alwaysInMarket.value ? null : form.buyRule,
    // null 表示「不设基础卖出条件」，后端据此按持有到期末处理。
    sell_rule: holdToEnd.value ? null : form.sellRule,
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
    duplicate_correlation: form.duplicateCorrelation,
    holdout_candidates: form.holdoutCandidates,
    walk_forward_windows: form.walkForwardWindows,
    significance_permutations: form.significancePermutations,
    ranking_value: rankingValue.value || null,
    rank_order: rankingOrder.value,
    ranking_top_n: rankingTopN.value,
  }
}

function applyRecommendedRanking(): void {
  const recommended = rankingRecommendation.value
  if (!recommended) return
  rankingValue.value = recommended.value_id
  rankingOrder.value = recommended.order
  message.value = `已采用推荐的排序规则：${recommended.display_name ?? recommended.value_id}。`
}

/** 把研究结果整包搬到组合回测：规则 + 排序 + 槽位，省掉手动重填一遍。 */
function applyToPortfolio(): void {
  sessionStorage.setItem(RESEARCH_APPLY_KEY, JSON.stringify({
    universe: form.universe,
    instrument_types: [form.instrumentType],
    boards: [...form.boards],
    // date_mode 必须一起带过去：研究用「全历史/最近五年」时只传绝对日期会让
    // 组合页的下拉仍显示最近五年、实际却按研究区间跑（或反过来被收窄）。
    date_mode: form.dateMode,
    ...(form.dateMode === 'custom' && form.startDate && form.endDate
      ? { start_date: form.startDate, end_date: form.endDate }
      : {}),
    buy_rule: alwaysInMarket.value ? null : form.buyRule,
    sell_rule: holdToEnd.value ? null : form.sellRule,
    ranking_value: effectiveRankingValue.value || null,
    rank_order: effectiveRankOrder.value,
    max_positions: rankingTopN.value,
    formula_text: mode.value === 'custom' ? form.formulaText.trim() : null,
    formula_parameters: mode.value === 'custom' ? { ...form.formulaParameters } : {},
  }))
  if (import.meta.env.MODE !== 'test') window.location.assign('/portfolio-backtest')
}

/** 把买入规则搬到公式选股：一次性的「今天谁满足条件」扫描。 */
function applyToFormulaScreen(): void {
  const rule = alwaysInMarket.value ? null : form.buyRule
  sessionStorage.setItem(RESEARCH_TO_SCREEN_KEY, JSON.stringify({
    signals: rule?.signals ?? [],
    conditions: rule?.conditions ?? [],
    // 强制条件在选股里没有对应概念（选股只有「条件」一种），带过去让目标页决定
    // 怎么合并并说明，不带的话用户会以为它跟着一起过去了。
    required_conditions: rule?.required_conditions ?? [],
    combine_mode: rule?.combine_mode ?? 'all',
    minimum_matches: rule?.minimum_matches ?? null,
    universe: form.universe,
    boards: [...form.boards],
    instrument_types: [form.instrumentType],
    mode: mode.value === 'custom' ? 'custom' : 'preset',
    formula_text: mode.value === 'custom' ? form.formulaText.trim() : null,
    formula_parameters: mode.value === 'custom' ? { ...form.formulaParameters } : {},
    ...(effectiveRankingValue.value
      ? {
          ranking_value: effectiveRankingValue.value,
          rank_order: effectiveRankOrder.value,
        }
      : {}),
  }))
  if (import.meta.env.MODE !== 'test') window.location.assign('/aquest')
}

/**
 * 把勾选的候选存进候选库、按方向并入基础规则的动作
 * 在 composables/useCandidateSelection；这里接上表单与任务执行。
 */
const {
  selectedCandidateIds,
  toggleCandidate,
  clearCandidateSelection,
  saveSelectedCandidates,
  applySelectedCandidates,
} = useCandidateSelection({
  report,
  activeHistoryId,
  message,
  saving,
  holdToEnd,
  alwaysInMarket,
  emptyRule,
  readRule: (side) => (side === 'sell' ? form.sellRule : form.buyRule),
  setBuyRule,
  setSellRule,
  submit,
  describeError: apiMessage,
})

function finiteNumber(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback
}

function storedBoards(value: unknown): InstrumentBoard[] {
  if (!Array.isArray(value)) return []
  const allowed = new Set<string>(STOCK_BOARD_OPTIONS.map((option) => option.key))
  return value.filter(
    (item): item is InstrumentBoard => typeof item === 'string' && allowed.has(item),
  )
}

/**
 * 把历史记录里保存的请求回填进表单。
 *
 * 后端存的是解析后的请求：日期已经是具体区间，所以这里还原的就是"当时真正跑的
 * 那套配置"，而不是用户填的原始模式。回填期间置位 ``restoring``，避免证券范围
 * 的 watch 顺手改写费用和板块。
 */
async function applyStoredRequest(raw: unknown): Promise<void> {
  if (!raw || typeof raw !== 'object') return
  const request = raw as Record<string, unknown>
  restoring.value = true
  try {
    const types = Array.isArray(request.instrument_types) ? request.instrument_types : []
    const type = types.find(
      (item): item is InstrumentType => item === 'stock',
    )
    if (type) form.instrumentType = type
    if (request.universe === 'all' || request.universe === 'sh' || request.universe === 'sz') {
      form.universe = request.universe
    }
    form.boards = storedBoards(request.boards)

    const start = typeof request.start_date === 'string' ? request.start_date : ''
    const end = typeof request.end_date === 'string' ? request.end_date : ''
    if (start && end) {
      form.dateMode = 'custom'
      form.startDate = start
      form.endDate = end
    } else if (
      request.date_mode === 'recent5'
      || request.date_mode === 'full'
      || request.date_mode === 'custom'
    ) {
      form.dateMode = request.date_mode
      form.startDate = ''
      form.endDate = ''
    }

    if (
      request.search_sides === 'buy'
      || request.search_sides === 'sell'
      || request.search_sides === 'both'
    ) {
      form.searchSides = request.search_sides
    }
    form.minTrades = finiteNumber(request.min_trades, form.minTrades)
    form.minInstruments = finiteNumber(request.min_instruments, form.minInstruments)
    form.candidateLimit = finiteNumber(request.candidate_limit, form.candidateLimit)
    form.duplicateCorrelation = finiteNumber(
      request.duplicate_correlation,
      form.duplicateCorrelation,
    )
    form.holdoutCandidates = finiteNumber(request.holdout_candidates, form.holdoutCandidates)
    form.walkForwardWindows = finiteNumber(
      request.walk_forward_windows,
      form.walkForwardWindows,
    )
    form.significancePermutations = finiteNumber(
      request.significance_permutations,
      form.significancePermutations,
    )
    // 排序规则：存的时候是 null 表示「当年没指定」，别把它变成一个空字符串。
    rankingValue.value = typeof request.ranking_value === 'string' ? request.ranking_value : ''
    rankingOrder.value = request.rank_order === 'asc' ? 'asc' : 'desc'
    rankingTopN.value = finiteNumber(request.ranking_top_n, rankingTopN.value)
    form.initialCash = finiteNumber(request.initial_cash, form.initialCash)
    form.commission = finiteNumber(request.commission, form.commission)
    form.minCommission = finiteNumber(request.min_commission, form.minCommission)
    form.stampTax = finiteNumber(request.stamp_tax, form.stampTax)
    form.slippage = finiteNumber(request.slippage, form.slippage)
    if (request.execution === 'next_open' || request.execution === 'next_close') {
      form.execution = request.execution
    }

    const formulaText = typeof request.formula_text === 'string' ? request.formula_text.trim() : ''
    if (formulaText) {
      form.formulaText = formulaText
      if (mode.value !== 'custom') {
        mode.value = 'custom'
        customMetadata.value = null
        customParseError.value = ''
      }
      // 解析成功后公式输出才会进 availableValues，规则才能还原出可读标签。
      await parseCustomFormula()
      const parameters = request.formula_parameters
      if (parameters && typeof parameters === 'object') {
        for (const [name, value] of Object.entries(parameters as Record<string, unknown>)) {
          if (typeof value === 'number' && Number.isFinite(value)) {
            form.formulaParameters[name] = value
          }
        }
      }
    } else if (mode.value !== 'preset') {
      mode.value = 'preset'
      customMetadata.value = null
      parsedFormulaText.value = ''
    }

    // 规则放在最后：解析公式会把规则重置成默认值。
    // 历史里 buy_rule / sell_rule 为 null 就表示当年用的是「持续满仓」/「持有到期末」。
    const buyRule = parseStoredRuleGroup(request.buy_rule)
    alwaysInMarket.value = request.buy_rule === null
    if (buyRule) {
      setBuyRule(buyRule)
      alwaysInMarket.value = false
    }
    const sellRule = parseStoredRuleGroup(request.sell_rule)
    holdToEnd.value = request.sell_rule === null
    if (sellRule) {
      setSellRule(sellRule)
      holdToEnd.value = false
    }
    errors.value = {}
  } finally {
    // watch 是 pre-flush，同步赋值结束后才跑，所以等一个 tick 再解除屏蔽。
    await nextTick()
    restoring.value = false
  }
}

/**
 * 候选库「载入复测」的接收端。
 *
 * 拿到的是来源研究那次请求的完整输入，所以用 `applyStoredRequest`（和历史「加载」同一条路）。
 * 做完立刻清除 key —— 刷新页面不该再还原一次，否则用户手动改过的东西会被悄悄覆盖。
 */
async function restoreFromCandidateLibrary(): Promise<void> {
  const stored = sessionStorage.getItem(RESTORE_KEY)
  if (!stored) return
  try {
    await applyStoredRequest(JSON.parse(stored) as unknown)
    message.value = '已从候选库载入来源研究的配置，请确认后重新运行。'
  } catch {
    message.value = '候选库传来的研究配置无法解析，已忽略。'
  } finally {
    sessionStorage.removeItem(RESTORE_KEY)
  }
}

async function loadHistoryDetail(historyId: string): Promise<void> {
  if (!historyId) return
  try {
    const saved = await getConditionResearchHistory(historyId)
    await applyStoredRequest(saved.request)
    report.value = saved.result
    selectedStockView.value = 'baseline:full'
    clearCandidateSelection()
    activeHistoryId.value = historyId
    message.value = '已加载历史买卖条件研究，并还原当时的研究配置。'
  } catch (error) {
    message.value = apiMessage(error)
  }
}

/**
 * 删除一条历史。
 *
 * 页面上的结果刻意保留：删的是存档，不是这次已经算出来的结论。把结果一起
 * 清空会让人以为点错了，还得重新加载一遍才看得见。
 */
async function removeHistory(historyId: string): Promise<void> {
  try {
    const deleted = await deleteConditionResearchHistory(historyId)
    if (!deleted) {
      message.value = '这条历史记录不存在，或已经删掉了。'
      await loadHistory()
      return
    }
    if (activeHistoryId.value === historyId) activeHistoryId.value = ''
    await loadHistory()
    message.value = '历史记录已删除；页面上的结果保持不变，可以直接另存或继续调整。'
  } catch (error) {
    message.value = apiMessage(error)
  }
}

// 三句摘要的生成逻辑在 utils/conditionResearch（纯函数），页面只负责接上报告。
const significanceSummary = computed(() => significanceSummaryText(report.value))
const duplicateSummary = computed(() => duplicateSummaryText(report.value))
const trialSummary = computed(() => trialSummaryText(report.value))

function apiMessage(error: unknown): string {
  return error instanceof ApiError
    ? error.message
    : '买卖条件研究失败，请检查行情数据和配置后重试。'
}

/**
 * 从波段研究页带过来的结论：只把买卖规则和排序建议落到表单里，
 * 阈值本身不算数 —— 本页会在训练/验证/留出三段上重新搜索。
 */
function applySwingPlan(): void {
  const plan = consumeSwingPlan()
  if (!plan) return
  if (plan.buy_rule) {
    form.buyRule = plan.buy_rule
    alwaysInMarket.value = false
  }
  if (plan.sell_rule) {
    form.sellRule = plan.sell_rule
    holdToEnd.value = false
  }
  if (plan.ranking) {
    rankingValue.value = plan.ranking.value
    rankingOrder.value = plan.ranking.order
  }
  message.value =
    '已带入波段研究的结论。阈值只是起点，研究会在自己的三段数据上重新搜索；' +
    '排序建议同样以本页按实际收益率的评估为准。'
}

/**
 * 进入页面时要跑的一次性交接，候选库「载入复测」先走：它还原的是一次完整研究的
 * 配置，要压在波段计划之上。
 *
 * App.vue 用 KeepAlive 缓存页面实例，站内链接又是软导航：从别处跳过来时页面不会
 * 重建，onMounted 不跑，只有 onActivated 会触发。交接放在任何一边都会漏一半，
 * 所以两处都要接得住。
 */
async function consumeHandoffs(): Promise<void> {
  await restoreFromCandidateLibrary()
  applySwingPlan()
}

// KeepAlive 下 activated 与 mounted 同批次触发，届时元数据还没到、表单还没准备好，
// 首次挂载交给 onMounted 自己跑。
let booted = false

onMounted(async () => {
  try {
    metadata.value = await fetchMetadata()
    resetRules()
    await consumeHandoffs()
    await refreshResearchPresets()
    await loadHistory()
    await resumeActiveJob()
  } catch (error) {
    message.value = apiMessage(error)
  } finally {
    booted = true
  }
})

onActivated(() => {
  if (!booted) return
  consumeHandoffs().catch((error: unknown) => {
    message.value = apiMessage(error)
  })
})
</script>

<template>
  <div class="screen-page condition-research-page" data-testid="condition-research-page">
    <AppTopbar />

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

      <PageGuide page-key="condition-research" />

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

          <fieldset class="form-section preset-section" data-testid="research-preset">
            <legend>研究方案 <span class="legend-count">{{ researchPresets.length }} 个</span></legend>
            <div class="preset-toolbar">
              <select v-model="selectedPresetId" data-testid="research-preset-select" aria-label="已保存研究方案">
                <option value="">选择已保存方案</option>
                <option v-for="preset in researchPresets" :key="preset.preset_id" :value="preset.preset_id">{{ preset.name }}</option>
              </select>
              <button class="secondary-button" type="button" data-testid="run-research-preset" :disabled="!selectedPresetId || presetBusy || loading" @click="quickResearch">
                <span v-if="loading" class="spinner" aria-hidden="true"></span>
                一键研究
              </button>
              <button class="text-button" type="button" data-testid="load-research-preset" :disabled="!selectedPresetId || presetBusy" @click="loadSelectedPreset">只加载设置</button>
              <button class="text-button" type="button" data-testid="delete-research-preset" :disabled="!selectedPresetId || presetBusy" @click="deleteSelectedPreset">删除</button>
            </div>
            <div class="preset-save-row">
              <input v-model="presetName" data-testid="research-preset-name" type="text" maxlength="60" placeholder="方案名称，例如：主板低位进场 + 持有到期末">
              <button class="secondary-button" type="button" data-testid="save-research-preset" :disabled="presetBusy" @click="savePresetAsNew">保存为新方案</button>
              <button class="secondary-button" type="button" data-testid="overwrite-research-preset" :disabled="!selectedPresetId || presetBusy" @click="overwritePreset">覆盖当前</button>
              <button class="secondary-button" type="button" data-testid="rename-research-preset" :disabled="!selectedPresetId || presetBusy" @click="renamePreset">重命名</button>
            </div>
            <p class="helper">方案存的是<b>整套研究设置</b>（买卖基础条件 / 标的范围 / 区间 / 搜索与验证参数 / 费用），存在服务端数据库里，换浏览器也在。「一键研究」= 载入这套设置并立刻开始；「只加载设置」只还原表单，不运行。</p>
          </fieldset>

          <fieldset v-if="mode === 'custom'" class="form-section custom-formula-section">
            <legend>通达信公式</legend>
            <textarea v-model="form.formulaText" data-testid="condition-formula" rows="6" spellcheck="false" placeholder="例如：买入:CROSS(C,MA(C,20)); 卖出:CROSS(MA(C,20),C); 强度:C/MA(C,20);"></textarea>
            <button class="secondary-button" data-testid="condition-parse-formula" type="button" :disabled="customParseLoading" @click="parseCustomFormula">{{ customParseLoading ? '解析中…' : '解析公式' }}</button>
            <p class="helper">自动搜索会使用公式里的数值输出作为候选特征。</p>
            <p v-if="priceScaleOutputs.length" class="field-error" data-testid="condition-price-scale-warning">
              以下输出仍是原始价格/成交量量纲，挖掘出的绝对阈值无法跨股票比较，已排除：
              {{ priceScaleOutputs.map((value) => value.display_name).join('、') }}。
              请改用比值类输出，例如 <code>C/MA(C,20)</code>。
            </p>
            <p v-if="customParseError" class="field-error">{{ customParseError }}</p>
            <div v-if="customMetadata?.parameters.length" class="parameter-grid">
              <div v-for="parameter in customMetadata.parameters" :key="parameter.name">
                <label :for="'condition-param-' + parameter.name">{{ parameter.name }}</label>
                <input :id="'condition-param-' + parameter.name" v-model.number="form.formulaParameters[parameter.name]" :data-testid="'condition-param-' + parameter.name" type="number" :min="parameter.minimum" :max="parameter.maximum" :step="parameter.step">
              </div>
            </div>
          </fieldset>

          <fieldset class="form-section">
            <legend>标的范围</legend>
            <div class="compact-fields">
              <div>
                <label for="condition-instrument-type">证券类型</label>
                <select id="condition-instrument-type" v-model="form.instrumentType" data-testid="condition-instrument-type">
                  <option value="stock">股票</option>
                </select>
              </div>
              <div>
                <label for="condition-universe">市场</label>
                <select id="condition-universe" v-model="form.universe" data-testid="condition-universe">
                  <option value="all">沪深全部{{ scopeNoun }}</option>
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
            <p class="helper">{{ scopeHelper }}</p>
          </fieldset>

          <RuleGroupEditor
            v-if="!alwaysInMarket"
            :model-value="form.buyRule"
            :values="availableValues"
            :condition-operators="metadata?.condition_operators"
            title="买入基础条件"
            testid="condition-buy-rule"
            :max-conditions="3"
            @update:model-value="setBuyRule"
          />
          <p v-if="errors.buyRule" class="field-error" data-testid="condition-buy-error">{{ errors.buyRule }}</p>
          <div class="condition-hold-to-end">
            <label>
              <input
                type="checkbox"
                data-testid="condition-always-in-market"
                :checked="alwaysInMarket"
                @change="setAlwaysInMarket(($event.target as HTMLInputElement).checked)"
              >
              不设基础买入条件（持续满仓，让研究自己找买点）
            </label>
            <small v-if="alwaysInMarket" data-testid="condition-always-in-market-hint">
              每次离场后下一个交易日立即再买入，全程持有。研究以这条「一直持有」的基线为参照挖掘买入条件 ——
              挖出来的候选就是完整买入规则，可以直接拿去用。
            </small>
          </div>
          <RuleGroupEditor
            v-if="!holdToEnd"
            :model-value="form.sellRule"
            :values="availableValues"
            :condition-operators="metadata?.condition_operators"
            title="卖出基础条件"
            testid="condition-sell-rule"
            :max-conditions="3"
            @update:model-value="setSellRule"
          />
          <p v-if="errors.sellRule" class="field-error">{{ errors.sellRule }}</p>
          <div class="condition-hold-to-end">
            <label>
              <input
                type="checkbox"
                data-testid="condition-hold-to-end"
                :checked="holdToEnd"
                @change="setHoldToEnd(($event.target as HTMLInputElement).checked)"
              >
              不设基础卖出条件（持有到期末，让研究自己找卖点）
            </label>
            <small v-if="holdToEnd" data-testid="condition-hold-to-end-hint">
              买入后一直持有到回测区间最后一天按收盘价平仓。研究以这条「一直持有」的基线为参照挖掘卖出条件 ——
              挖出来的候选就是完整卖出规则，可以直接拿去用。
            </small>
          </div>
          <p v-if="noBidirectionalValue" class="helper" data-testid="condition-sell-default-hint">
            当前可用输出里没有零轴震荡指标（如 MACD 柱），阈值 0 的两侧并不都成立；
            卖出默认条件已改为买入条件的互补写法以避免恒不成立，请按研究目标自行调整。
          </p>

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
                <label for="condition-duplicate-correlation">同义条件合并门槛<TermHint term="duplicateCorrelation" /></label>
                <input id="condition-duplicate-correlation" v-model.number="form.duplicateCorrelation" data-testid="condition-duplicate-correlation" type="number" min="0.5" max="1" step="0.05">
              </div>
              <div>
                <label for="condition-holdout-candidates">最终留出候选/侧</label>
                <input id="condition-holdout-candidates" v-model.number="form.holdoutCandidates" data-testid="condition-holdout-candidates" type="number" min="1" max="3" step="1">
              </div>
              <div>
                <label for="condition-permutations">噪声门槛检验次数<TermHint term="噪声门槛检验次数" /></label>
                <input id="condition-permutations" v-model.number="form.significancePermutations" data-testid="condition-permutations" type="number" min="0" max="500" step="10">
              </div>
            </div>
            <div class="compact-fields condition-fields-top">
              <div>
                <label for="condition-walk-forward">滚动复验窗口数<TermHint term="rollingWindows" /></label>
                <select id="condition-walk-forward" v-model.number="form.walkForwardWindows" data-testid="condition-walk-forward">
                  <option :value="1">不拆（只复验一次）</option>
                  <option :value="2">2 个窗口</option>
                  <option :value="3">3 个窗口</option>
                  <option :value="4">4 个窗口</option>
                  <option :value="5">5 个窗口</option>
                </select>
              </div>
            </div>
            <p class="helper">把最终留出期再切成几段，逐个回放同一批候选，看这条条件在不同时间段是否都成立。窗口太短会自动减少，并在结果里说明。</p>
            <p v-if="errors.significancePermutations" class="field-error">{{ errors.significancePermutations }}</p>
            <div class="compact-fields condition-fields-top">
              <div><label>主目标</label><p class="field-static">提高胜率、夏普和收益</p></div>
            </div>
            <p v-if="errors.holdoutCandidates" class="field-error">{{ errors.holdoutCandidates }}</p>
            <p v-if="errors.minInstruments" class="field-error">{{ errors.minInstruments }}</p>
            <p v-if="errors.candidateLimit" class="field-error">{{ errors.candidateLimit }}</p>
            <p v-if="errors.duplicateCorrelation" class="field-error">{{ errors.duplicateCorrelation }}</p>
            <p class="helper">两条候选的命中向量相关性达到门槛就算同一个发现，只留排序最好的那条，其余列在它下面。0.6 会合并同一指标相邻分位的阈值；调到 1 等于基本不合并。</p>
            <p class="helper">候选需通过噪声、样本、胜率、盈亏比和平均单笔收益门槛；逐股总收益低于基础规则时只提示，不再单独淘汰。通过候选按验证胜率、逐股中位夏普和年化收益排序。</p>
          </fieldset>

          <fieldset class="form-section">
            <legend>排序规则（与组合回测联动）</legend>
            <div class="compact-fields condition-fields-top">
              <div>
                <label for="condition-ranking-value">排序指标</label>
                <select id="condition-ranking-value" v-model="rankingValue" data-testid="condition-ranking-value">
                  <option value="">不指定，但仍给出排序建议</option>
                  <option v-for="value in rankableValues" :key="value.id" :value="value.id">{{ value.display_name }}</option>
                </select>
              </div>
              <div>
                <label for="condition-rank-order">排序方向<TermHint term="rankOrder" /></label>
                <select id="condition-rank-order" v-model="rankingOrder" data-testid="condition-rank-order">
                  <option value="desc">指标值越大越优先</option>
                  <option value="asc">指标值越小越优先</option>
                </select>
              </div>
            </div>
            <div class="compact-fields condition-fields-top">
              <div>
                <label for="condition-ranking-top-n">持仓槽位<TermHint term="slot" /></label>
                <input id="condition-ranking-top-n" v-model.number="rankingTopN" data-testid="condition-ranking-top-n" type="number" min="1" max="50" step="1">
              </div>
              <div><label>用途</label><p class="field-static">判断条件在有限槽位下是否还有意义</p></div>
            </div>
            <p class="helper">
              研究本身不建组合、不抢槽位；带上排序规则后，结果会额外给出「该按什么排序」的推荐，
              并告诉你这个买入条件在 {{ rankingTopN }} 个槽位里到底还有没有话语权。
              排序下拉只列出能跨股票比较的输出（价格、均线这类按绝对值排序没有意义）。
            </p>
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
            <div
              v-if="report.disclosure"
              class="condition-disclosure"
              data-testid="condition-survivorship-notice"
            >
              <strong>数据覆盖与偏差提示</strong>
              <p v-for="note in report.disclosure.notes" :key="note">{{ note }}</p>
              <span class="condition-disclosure-meta">
                标的范围 {{ report.disclosure.universe }} ·
                已覆盖 {{ report.disclosure.processed_instruments }} /
                {{ report.disclosure.requested_instruments }} 只
                （{{ formatPercent(report.disclosure.coverage_ratio) }}）
              </span>
            </div>
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
              <div><span>基础验证逐股中位夏普 / 年化 / 总收益</span><strong>{{ formatNumber(report.baseline.validation?.per_stock.median_sharpe) }} / {{ formatPercent(report.baseline.validation?.per_stock.median_annual_return) }} / {{ formatPercent(report.baseline.validation?.per_stock.median_total_return) }}</strong></div>
            </div>
            <ConditionRankingAdvice :ranking="rankingReport" @apply="applyRecommendedRanking" />
            <p v-if="slotHint" class="ranking-hint" data-testid="condition-slot-hint">
              <TermHint term="dailyHits" />{{ slotHint }}
            </p>
            <div class="condition-bridge-actions">
              <button
                class="primary-button"
                data-testid="condition-apply-to-portfolio"
                type="button"
                @click="applyToPortfolio"
              >用这套规则去跑组合回测</button>
              <small>会把买卖规则、排序规则（没指定就用推荐值）和槽位数一起带到组合回测页。</small>
            </div>
            <div class="condition-bridge-actions">
              <button
                class="secondary-button"
                data-testid="condition-apply-to-screen"
                type="button"
                :disabled="alwaysInMarket"
                @click="applyToFormulaScreen"
              >用这套条件去选股</button>
              <small>
                把买入侧的条件带到公式选股，扫一遍「今天谁满足条件」。
                <template v-if="alwaysInMarket">持续满仓没有买入条件可带，请先取消持续满仓。</template>
                <template v-else>注意两件事不一样：这里评的是「什么时候进、什么时候出」，选股只回答「某一天是否满足」，不带卖出条件。</template>
              </small>
            </div>
            <div v-if="loading && job" class="progress-track condition-progress" aria-label="条件研究进度">
              <span :style="{ width: Math.round(job.progress * 100) + '%' }"></span>
            </div>
            <div v-if="!report.candidates.length" class="inline-empty">
              {{ report.diagnostic ?? '没有候选条件同时满足样本数和股票覆盖要求。' }}
            </div>
            <div v-else>
              <div class="condition-candidate-actions" data-testid="condition-candidate-actions">
                <span>已选 {{ selectedCandidateIds.length }} / {{ report.candidates.length }} 条</span>
                <button
                  class="primary-button"
                  data-testid="save-selected-conditions"
                  type="button"
                  :disabled="saving || !selectedCandidateIds.length || !activeHistoryId"
                  @click="saveSelectedCandidates"
                >加入候选库</button>
                <button
                  class="secondary-button"
                  data-testid="apply-selected-conditions"
                  type="button"
                  :disabled="loading || !selectedCandidateIds.length"
                  @click="applySelectedCandidates"
                >加入基础规则并重测</button>
                <button
                  class="text-button"
                  data-testid="clear-selected-conditions"
                  type="button"
                  :disabled="!selectedCandidateIds.length"
                  @click="clearCandidateSelection"
                >清空选择</button>
              </div>
              <p class="helper" data-testid="condition-merge-hint">
                买入候选以「同时满足」并入基础买入规则（收窄入场）；卖出候选以「任一满足」并入（多一个离场触发，与研究评估口径一致）。
              </p>
              <p class="helper">
                勾选后按方向追加：买入候选进「买入基础条件」，卖出候选进「卖出基础条件」，
                然后立即用新规则重跑一次研究。
              </p>
              <p v-if="significanceSummary" class="scope-helper" data-testid="condition-research-significance-summary">{{ significanceSummary }}</p>
              <p v-if="duplicateSummary" class="scope-helper" data-testid="condition-duplicate-summary">{{ duplicateSummary }}</p>
              <p v-if="trialSummary" class="scope-helper" data-testid="condition-trial-summary">{{ trialSummary }}</p>
              <p class="scope-helper" data-testid="condition-validation-objective-help">通过验证门槛的候选按验证胜率、逐股中位夏普和逐股中位年化收益的综合名次进入留出复验。这里的夏普和收益是逐股统计，不等同于有限槽位组合回测的整体绩效；应用后仍需回到组合回测核对。</p>
              <div class="table-wrap">
                <table>
                  <caption class="sr-only">买卖条件候选验证结果</caption>
                  <thead><tr><th>选择</th><th>方向</th><th>新增条件</th><th>样本 / 股票</th><th>训练 lift</th><th>前 N 名胜率<TermHint term="topNWinRate" /></th><th title="买入按基础规则入场日计，卖出按持仓期提前离场决策点计（全市场合并，分母见结果字段 sample_totals）">条件命中率</th><th title="验证期合并成交笔数 / 你设置的最少交易样本">验证成交 / 门槛</th><th>验证胜率</th><th>全区间胜率</th><th title="逐股胜率改善比例，以及各股验证期夏普、年化收益和总收益的中位数">逐股提升率 / 夏普 / 年化 / 总收益</th><th>盈亏比</th><th>留出胜率 / 复验状态</th><th>状态</th></tr></thead>
                  <tbody>
                    <tr
                      v-for="candidate in report.candidates"
                      :key="candidate.candidate_id"
                      :data-testid="'condition-research-candidate-' + candidate.candidate_id"
                      :class="{ 'condition-candidate-selected': selectedStockView === candidate.candidate_id }"
                    >
                      <td>
                        <input
                          type="checkbox"
                          :data-testid="'select-condition-' + candidate.candidate_id"
                          :checked="selectedCandidateIds.includes(candidate.candidate_id)"
                          :aria-label="'选择候选条件 ' + conditionText(candidate)"
                          @change="toggleCandidate(candidate.candidate_id)"
                        >
                      </td>
                      <td class="condition-side">{{ candidate.side === 'buy' ? '买入过滤' : '卖出触发' }}</td>
                      <td class="condition-rule">
                        {{ conditionText(candidate) }}
                        <small v-if="candidate.validation_objective_score !== null && candidate.validation_objective_score !== undefined" :title="'按验证胜率、逐股中位夏普和逐股中位年化收益综合排序；0 到 1 表示同侧候选中的相对名次'">验证综合名次分 {{ formatNumber(candidate.validation_objective_score, 3) }}</small>
                        <span
                          v-if="mergedDuplicates(candidate).length"
                          class="condition-merged"
                          :title="duplicateTitle(candidate)"
                          :data-testid="'merged-duplicates-' + candidate.candidate_id"
                        >已合并 {{ mergedDuplicates(candidate).length }} 条</span>
                      </td>
                      <td>{{ candidate.mining.support_count }} / {{ candidate.mining.instrument_count }}</td>
                      <td :data-testid="'mining-lift-' + candidate.candidate_id">
                        {{ formatNumber(candidate.mining.lift) }}
                        <span
                          v-if="significanceLabel(candidate)"
                          :class="candidate.mining.significant ? 'positive' : 'muted-code'"
                          :title="significanceTitle(candidate)"
                          :data-testid="'significance-' + candidate.candidate_id"
                        >{{ significanceLabel(candidate) }}</span>
                      </td>
                      <td
                        :title="topNTitle(candidate)"
                        :data-testid="'top-n-rate-' + candidate.candidate_id"
                      >{{ topNWinRate(candidate) }}</td>
                      <td :title="hitRateTitle(candidate)">{{ conditionHitRate(candidate) }}</td>
                      <td>{{ validationTrades(candidate) }}</td>
                      <td>{{ formatPercent(candidate.validation.pooled_trades.win_rate) }}</td>
                      <td>{{ formatPercent(candidate.full_period?.pooled_trades.win_rate) }}</td>
                      <td>
                        {{ formatPercent(candidate.validation.per_stock.improved_stock_ratio) }}
                        <small class="condition-objective-metrics">{{ formatNumber(candidate.validation.per_stock.median_sharpe) }} / {{ formatPercent(candidate.validation.per_stock.median_annual_return) }} / {{ formatPercent(candidate.validation.per_stock.median_total_return) }}</small>
                      </td>
                      <td>{{ formatNumber(candidate.validation.pooled_trades.payoff_ratio) }}</td>
                      <td>
                        <span v-if="candidate.holdout_evaluation?.status === 'evaluated'">{{ formatPercent(candidate.holdout?.pooled_trades.win_rate) }}</span>
                        <span v-else class="muted-code">—</span>
                        <small
                          v-if="candidate.holdout_evaluation?.status === 'evaluated'"
                          :class="candidate.holdout_evaluation.passed_constraints ? 'positive' : 'muted-code'"
                          :data-testid="'holdout-evaluation-' + candidate.candidate_id"
                        >留出已复验：{{ candidate.holdout_evaluation.passed_constraints ? '通过' : '未通过' }}</small>
                        <small
                          v-else-if="candidate.holdout_evaluation?.status === 'insufficient_samples'"
                          class="muted-code"
                          :data-testid="'holdout-evaluation-' + candidate.candidate_id"
                        >留出样本不足：{{ candidate.holdout_evaluation.reason }}</small>
                        <small
                          v-else-if="candidate.holdout_evaluation?.status === 'unevaluable'"
                          class="muted-code"
                          :data-testid="'holdout-evaluation-' + candidate.candidate_id"
                        >留出无法评估：{{ candidate.holdout_evaluation.reason }}</small>
                        <small v-else class="muted-code" :data-testid="'holdout-evaluation-' + candidate.candidate_id">
                          {{ candidate.holdout ? '留出复验状态缺失' : '未入留出名额' }}
                        </small>
                      </td>
                      <td class="condition-status">
                        <span :class="candidate.passed_constraints ? 'positive' : 'muted-code'">{{ constraintText(candidate) }}</span>
                        <small v-if="candidate.performance_warnings?.length" class="condition-performance-warning">提示：{{ candidate.performance_warnings.join('；') }}</small>
                        <button
                          v-if="candidate.full_period"
                          class="text-button"
                          :data-testid="'show-condition-stock-' + candidate.candidate_id"
                          type="button"
                          @click="selectedStockView = candidate.candidate_id + ':full'"
                        >个股</button>
                        <span v-else-if="candidate.passed_constraints" class="muted-code">验证通过，未入留出名额</span>
                        <span v-else class="muted-code">未进留出期</span>
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>
            <ConditionSensitivityTable :candidates="report.candidates" :values="availableValues" />
            <ConditionRollingTable :report="report" :values="availableValues" />
            <ConditionStockDetail
              :report="report"
              :values="availableValues"
              v-model:view="selectedStockView"
            />
          </template>
        </section>
      </div>

      <div v-if="loading && job" class="task-progress task-progress-panel" data-testid="condition-research-progress">
        <div class="task-progress-heading"><span>{{ job.phase ?? '买卖条件研究进度' }}</span><strong>{{ Math.round(job.progress * 100) }}%</strong></div>
        <div class="progress-track"><span :style="{ width: Math.round(job.progress * 100) + '%' }"></span></div>
        <small>本阶段已处理 {{ phaseCount(job, 'phase_current') }} / {{ phaseCount(job, 'phase_total') }} 只股票</small>
      </div>

      <ConditionHistoryPanel
        v-model:page="historyPage"
        :history="history"
        :loading="historyLoading"
        :active-id="activeHistoryId"
        :values="availableValues"
        @refresh="loadHistory"
        @load="loadHistoryDetail"
        @remove="removeHistory"
      />
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

.condition-candidate-actions {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  margin-bottom: 0.35rem;
  color: var(--text-muted);
  font-size: 0.85rem;
}

.condition-candidate-actions button:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}

.condition-hold-to-end {
  margin: 0.6rem 0 0.8rem;
}

.condition-hold-to-end label {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  font-size: 0.9rem;
}

.condition-hold-to-end small {
  display: block;
  margin-top: 0.35rem;
  color: var(--text-muted, #6b7280);
  line-height: 1.5;
}

:deep(.condition-rule) {
  min-width: 180px;
  font-family: var(--font-mono, monospace);
  font-size: 0.85rem;
}

.condition-merged {
  display: inline-block;
  margin-left: 0.4rem;
  padding: 0 0.35rem;
  border: 1px solid var(--border, #d1d5db);
  border-radius: 999px;
  font-family: var(--font-body, inherit);
  font-size: 0.7rem;
  color: var(--text-muted, #6b7280);
  cursor: help;
  white-space: nowrap;
}

/* 候选表 14 列，全局 table 的 min-width=720px 远远不够：中文可以在任意
   字符间断行，auto 布局会把「状态」「方向」压到一字宽、每字一行。
   给这两列明确的最小宽度，放不下的部分交给 .table-wrap 横向滚动。 */
.condition-side {
  white-space: nowrap;
}

.condition-status {
  min-width: 240px;
}

.condition-objective-metrics,
.condition-performance-warning {
  display: block;
  margin-top: 0.25rem;
  font-size: 0.75rem;
}

.condition-objective-metrics {
  color: var(--subtle);
}

.condition-performance-warning {
  color: var(--warning, #f59e0b);
  line-height: 1.4;
}

.condition-sensitivity {
  margin-top: 1.4rem;
}

:deep(.sensitivity-cell) {
  min-width: 84px;
  text-align: center;
  font-variant-numeric: tabular-nums;
  cursor: help;
}

:deep(.sensitivity-cell.sens-good) {
  background: rgba(34, 197, 94, 0.26);
  color: #d1fae5;
}

:deep(.sensitivity-cell.sens-mid) {
  background: rgba(34, 197, 94, 0.13);
  color: #bbf7d0;
}

:deep(.sensitivity-cell.sens-weak) {
  background: rgba(148, 163, 184, 0.16);
  color: var(--text-muted, #94a3b8);
}

/* 优势打反：邻域比基线还差，是最需要看见的一格。 */
:deep(.sensitivity-cell.sens-flipped) {
  background: rgba(248, 113, 113, 0.2);
  color: #fecaca;
}

:deep(.sensitivity-cell.sens-excluded) {
  color: var(--text-subtle, #64748b);
}

.condition-stock-details {
  margin-top: 1.4rem;
}

:deep(.condition-stock-pagination) {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
  color: var(--text-muted);
  font-size: 0.82rem;
}

:deep(.condition-stock-pagination > div) {
  display: flex;
  gap: 0.5rem;
}

:deep(.condition-stock-controls) {
  display: grid;
  gap: 0.65rem;
  grid-template-columns: repeat(3, minmax(150px, 1fr));
}

:deep(.condition-stock-view) {
  display: grid;
  gap: 0.35rem;
  min-width: 0;
}

.condition-history {
  margin-top: 1.25rem;
}

.condition-disclosure {
  display: grid;
  gap: 0.35rem;
  margin-bottom: 1rem;
  padding: 0.75rem 0.9rem;
  border: 1px solid color-mix(in srgb, var(--warning, #b45309) 35%, transparent);
  border-radius: 0.6rem;
  background: color-mix(in srgb, var(--warning, #b45309) 8%, transparent);
  font-size: 0.86rem;
  line-height: 1.5;
}

.condition-disclosure p {
  margin: 0;
}

.condition-disclosure-meta {
  opacity: 0.75;
}

@media (max-width: 1050px) {
  .condition-research-grid {
    grid-template-columns: 1fr;
  }
}

.ranking-hint {
  margin: 0;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.55;
}

.ranking-hint {
  margin-top: 8px;
}

.condition-bridge-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  margin-top: 14px;
}

.condition-bridge-actions small {
  color: var(--subtle);
  font-size: 11px;
}

/* 主按钮默认占满整行，这里只需要它醒目的配色。 */
.condition-bridge-actions .primary-button {
  width: auto;
  min-height: 36px;
  margin-top: 0;
  padding: 0 18px;
}

@media (max-width: 640px) {
  /* .condition-stock-controls 是子组件内部节点，父组件的 scoped 样式只能够到
     子组件根节点，必须走 :deep() —— 上面那条同名的 :deep 规则就是这么写的。 */
  .condition-fields-top,
  :deep(.condition-stock-controls) {
    grid-template-columns: 1fr;
  }
}
</style>
