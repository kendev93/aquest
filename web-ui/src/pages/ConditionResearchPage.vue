<script setup lang="ts">
import PageGuide from '../components/PageGuide.vue'
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'

import {
  cancelConditionResearch,
  createConditionResearch,
  deleteConditionResearchHistory,
  fetchConditionResearchHistory,
  getConditionResearch,
  getConditionResearchHistory,
  getConditionResearchResults,
} from '../api/conditionResearch'
import { FormulaScreenApiError, fetchMetadata, parseFormula } from '../api/formulaScreen'
import { fetchActiveJobs } from '../api/jobs'
import DateRangeSelector from '../components/DateRangeSelector.vue'
import RuleGroupEditor from '../components/RuleGroupEditor.vue'
import TermHint from '../components/TermHint.vue'
import ConditionHistoryPanel from '../components/ConditionHistoryPanel.vue'
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
import { importCandidates } from '../api/candidateLibrary'
import { parseStoredRuleGroup } from '../utils/formulaScreen'
import { importSummaryText } from '../utils/candidateLibrary'
import { consumeSwingPlan } from '../utils/swingHandoff'
import {
  RANKING_CONFIDENCE_LABELS,
  STOCK_BOARD_OPTIONS,
  conditionHitRate as formatConditionHitRate,
  constraintText,
  duplicateTitle as formatDuplicateTitle,
  formatConditionText,
  formatNumber,
  formatPercent,
  hitRateTitle,
  mergedDuplicates,
  phaseCount,
  significanceLabel,
  significanceTitle as formatSignificanceTitle,
  topNTitle as formatTopNTitle,
  topNWinRate,
  validationTrades as formatValidationTrades,
} from '../utils/conditionResearch'
import type { ResearchHistorySummary } from '../utils/conditionResearch'

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
const loading = ref(false)
const saving = ref(false)
const customParseLoading = ref(false)
const customParseError = ref('')
const showAdvanced = ref(false)
const job = ref<ConditionResearchJobState | null>(null)
const report = ref<ConditionResearchReport | null>(null)
const history = ref<ResearchHistorySummary[]>([])
const historyLoading = ref(false)
const selectedStockView = ref('baseline:full')
// 历史列表：后端已按完成时间由近到远排好，这里只管切片分页。
const activeHistoryId = ref('')
const historyPage = ref(1)
// 结果里勾选的候选条件，直接追加进基础规则后再跑一次研究。
const selectedCandidateIds = ref<string[]>([])
// 排序规则：研究不建组合，但结论要落到组合回测的「候选池 + 有限槽位」模型上。
const rankingValue = ref('')
const rankingOrder = ref<RankOrder>('desc')
const rankingTopN = ref(5)
// 条件研究页 → 组合回测页的桥接，走独立的键，避免和策略实验室的回填互相覆盖。
const RESEARCH_APPLY_KEY = 'indicator-lab.condition-research.apply'
// 候选库 → 条件研究页：「载入复测」把来源研究的整份输入搬过来还原表单。
const RESTORE_KEY = 'indicator-lab.condition-research.restore'
// 条件研究页 → 公式选股页：只搬买入侧的条件（选股是一次性扫描，没有卖出这一说）。
const RESEARCH_TO_SCREEN_KEY = 'indicator-lab.condition-research.to-screen'
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
const rankingConfidenceLabel = computed(() => (
  RANKING_CONFIDENCE_LABELS[rankingRecommendation.value?.confidence ?? 'none'] ?? '暂无法判断'
))
const rankingValueName = computed(() => (
  rankingRecommendation.value?.display_name || rankingRecommendation.value?.value_id || '—'
))
const rankingOrderText = computed(() => (
  rankingRecommendation.value?.order === 'asc' ? '从小到大' : '从大到小'
))
const rankingQuantiles = computed(() => (
  (rankingRecommendation.value?.quantile_returns ?? []).map((item) => formatPercent(item))
))
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
    customParseError.value = error instanceof FormulaScreenApiError
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
  }))
  if (import.meta.env.MODE !== 'test') window.location.assign('/indicator-lab')
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
      // 旧候选 id 已经不属于这份结果，勾选状态一并作废。
      clearCandidateSelection()
      message.value = '研究完成：处理 ' + report.value.processed + ' 只股票，发现 '
        + report.value.candidates.length + ' 条候选规则。'
      await loadHistory()
      // 新记录排在最前面。回到第一页并把它标成当前，否则页面还停在第二页，
      // 看起来就像是这次研究没被保存。
      historyPage.value = 1
      if (history.value.some((item) => item.history_id === jobId)) {
        activeHistoryId.value = jobId
      }
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

/**
 * 把勾选的候选存进候选库。
 *
 * 和「加入基础规则并重测」是两件事：那条是继续往下挖（改规则、立刻重跑），
 * 这条是留档——候选编号只在本次报告里有效，研究结果保存在历史里也就只留最近
 * 20 条。存进库里的证据是快照，不回指这条历史记录。
 */
async function saveSelectedCandidates(): Promise<void> {
  if (!selectedCandidateIds.value.length) return
  if (!activeHistoryId.value) {
    message.value = '这次研究还没有历史记录，无法入库。'
    return
  }
  saving.value = true
  try {
    const outcome = await importCandidates({
      history_id: activeHistoryId.value,
      candidate_ids: [...selectedCandidateIds.value],
    })
    message.value = importSummaryText(outcome.added.length, outcome.skipped.length)
    clearCandidateSelection()
  } catch (error) {
    message.value = apiMessage(error)
  } finally {
    saving.value = false
  }
}

function isSameCondition(left: ScreenCondition, right: ScreenCondition): boolean {
  return left.left_value === right.left_value
    && left.operator === right.operator
    && left.right_value === right.right_value
    && left.right_constant === right.right_constant
}

function toggleCandidate(candidateId: string): void {
  selectedCandidateIds.value = selectedCandidateIds.value.includes(candidateId)
    ? selectedCandidateIds.value.filter((item) => item !== candidateId)
    : [...selectedCandidateIds.value, candidateId]
}

function clearCandidateSelection(): void {
  selectedCandidateIds.value = []
}

/**
 * 把勾选的候选条件按方向追加进基础规则，然后立刻重跑一次研究。
 *
 * 买入候选只进买入规则、卖出候选只进卖出规则：挖出来的方向就是它当初被
 * 评估时的位置，换到另一侧没有依据。已存在的相同条件不再重复添加。
 */
async function applySelectedCandidates(): Promise<void> {
  const selected = (report.value?.candidates ?? []).filter((candidate) => (
    selectedCandidateIds.value.includes(candidate.candidate_id)
  ))
  if (!selected.length) return
  let added = 0
  for (const candidate of selected) {
    const incoming: ScreenCondition = { ...candidate.condition }
    if (candidate.side === 'sell' && holdToEnd.value) {
      // 基准从「一直持有」换成这条候选，否则重测还是以持有到期末为参照，
      // 只会挖出同一批条件。
      holdToEnd.value = false
      setSellRule({ ...emptyRule(), conditions: [incoming] })
      added += 1
      continue
    }
    if (candidate.side === 'buy' && alwaysInMarket.value) {
      // 对偶：基准从「天天买」换成这条候选，否则重测还是以持续满仓为参照。
      alwaysInMarket.value = false
      setBuyRule({ ...emptyRule(), conditions: [incoming] })
      added += 1
      continue
    }
    const isSell = candidate.side === 'sell'
    const target = isSell ? form.sellRule : form.buyRule
    if (target.conditions.some((item) => isSameCondition(item, incoming))) continue
    // 买入候选是 AND 到基础买入规则上的（收窄入场），卖出候选是 OR 上去的
    // （多一个离场触发）—— 后端 sell_mask |= condition_window，界面必须同口径，
    // 否则重测跑的是与候选当初被评估时不同的规则。
    const merged: RuleGroupPayload = {
      ...target,
      signals: [],
      combine_mode: isSell ? 'any' : target.combine_mode,
      conditions: [...target.conditions, incoming],
    }
    if (isSell) setSellRule(merged)
    else setBuyRule(merged)
    added += 1
  }
  if (!added) {
    message.value = '勾选的条件都已经在基础规则里了，无需重复添加。'
    return
  }
  clearCandidateSelection()
  await submit()
}

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

/** 有门槛数据时给一句总结，避免用户看到五个候选全是「疑似噪声」却不知道意味着什么。 */
const significanceSummary = computed(() => {
  const candidates = report.value?.candidates ?? []
  const thresholds = candidates
    .map((item) => item.mining.lift_threshold)
    .filter((value): value is number => typeof value === 'number')
  if (!thresholds.length) return ''
  const threshold = Math.max(...thresholds)
  const passed = candidates.filter((item) => item.mining.significant === true).length
  if (passed > 0) {
    return (
      `噪声门槛 ${threshold.toFixed(3)}：${candidates.length} 个候选里有 ${passed} 个超过了纯运气能达到的水平。` +
      '未过门槛的候选不会进入留出期复验。'
    )
  }
  return (
    `噪声门槛 ${threshold.toFixed(3)}：没有一个候选超过随机打乱能达到的水平，` +
    '这次挖到的条件很可能是运气，建议扩大股票范围、延长时间区间或放宽基础规则以增加样本。' +
    '未过门槛的候选不会进入留出期复验。'
  )
})

/**
 * 合并了多少条同义条件。合并不等于"少给几条"——空出来的名额由更靠后的、
 * 不同义的条件顶上，所以这里要说明清楚，别让用户以为名额被砍了。
 */
const duplicateSummary = computed(() => {
  const merged = report.value?.merged_duplicate_count ?? 0
  const threshold = report.value?.duplicate_correlation
  if (!merged || threshold === undefined) return ''
  return (
    `按相关性门槛 ${threshold.toFixed(2)} 合并了 ${merged} 条同义条件（同一个发现的不同写法，`
    + '多数只是阈值差一点）。空出的名额由更靠后、不同义的条件顶上，明细见每行条件名后的「已合并」。'
  )
})

/**
 * 累计试验记账（候选池 #20）。
 *
 * 两件事要说清楚，第二件比第一件更重要：
 * ① 判定有没有因为「跑过很多轮」而被抬高；
 * ② 同一段数据被反复挖过几次 —— 这不改变 p 值，但它是过拟合的直接信号。
 */
const trialSummary = computed(() => {
  const trials = report.value?.trials
  if (!trials) return ''
  const parts = [
    `本次共评估 ${trials.tested_conditions} 个条件，是第 ${trials.trial_count} 轮独立研究（按数据区间算，不是按次数）。`,
  ]
  if (trials.adjusted) {
    parts.push(
      `判定已按 ${trials.trial_count} 轮校正：同样的 p 值，撞出来的机会是 ${trials.trial_count} 倍。`,
    )
  }
  if (trials.repeated_studies > 0) {
    parts.push(
      `注意：这 ${trials.study_count} 次研究里有 ${trials.repeated_studies} 次是在同一段数据上重复挖的`
        + '——重复挖不会增加撞好运的机会，所以不计入轮数，但它说明你在反复榨同一段行情。',
    )
  }
  return parts.join('')
})

function apiMessage(error: unknown): string {
  return error instanceof FormulaScreenApiError
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

onMounted(async () => {
  try {
    metadata.value = await fetchMetadata()
    resetRules()
    // 候选库「载入复测」先走：它还原的是一次完整研究的配置，要压在波段计划之上。
    await restoreFromCandidateLibrary()
    applySwingPlan()
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
                <a class="nav-link" href="/portfolio-backtest">组合回测</a>
        <a class="nav-link" href="/strategy-lab">策略实验室</a>
        <a class="nav-link active" href="/condition-research" aria-current="page">买卖条件研究</a>
        <a class="nav-link" href="/strategy-fitness">策略适配性</a>
        <a class="nav-link" href="/market-data">本地行情</a>
        <a class="nav-link" href="/swing-research">波段研究</a>
        <a class="nav-link" href="/strategy-tracking">策略追踪</a>
        <a class="nav-link" href="/factor-test">因子检验</a>
        <a class="nav-link" href="/history-compare">历史对比</a>
        <a class="nav-link" href="/instrument-lab">标的检验台</a>
        <a class="nav-link" href="/candidate-library">候选库</a>
        <a class="nav-link" href="/factor-attribution">因子归因</a>
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
              <div><label>主目标</label><p class="field-static">跨股票交易胜率提升</p></div>
            </div>
            <p v-if="errors.holdoutCandidates" class="field-error">{{ errors.holdoutCandidates }}</p>
            <p v-if="errors.minInstruments" class="field-error">{{ errors.minInstruments }}</p>
            <p v-if="errors.candidateLimit" class="field-error">{{ errors.candidateLimit }}</p>
            <p v-if="errors.duplicateCorrelation" class="field-error">{{ errors.duplicateCorrelation }}</p>
            <p class="helper">两条候选的命中向量相关性达到门槛就算同一个发现，只留排序最好的那条，其余列在它下面。0.6 会合并同一指标相邻分位的阈值；调到 1 等于基本不合并。</p>
            <p class="helper">主目标是提高跨股票合并交易胜率；候选还需保持盈亏比和平均单笔净收益，并通过最终留出期复验。</p>
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
            </div>
            <div v-if="rankingReport" class="ranking-advice" data-testid="condition-ranking-advice">
              <div class="ranking-advice-head">
                <strong>排序规则推荐</strong>
                <span class="ranking-confidence" data-testid="condition-ranking-confidence">{{ rankingConfidenceLabel }}</span>
              </div>
              <template v-if="rankingRecommendation">
                <p class="ranking-advice-line">
                  建议按 <b>{{ rankingValueName }}</b> {{ rankingOrderText }} 排序
                  <button
                    class="link-button"
                    data-testid="condition-use-recommended-ranking"
                    type="button"
                    @click="applyRecommendedRanking"
                  >采用这个排序</button>
                </p>
                <div class="ranking-evidence">
                  <div>
                    <span>平均信息系数<TermHint term="icMean" /></span>
                    <strong>{{ formatNumber(rankingRecommendation.ic_mean, 3) }}</strong>
                  </div>
                  <div>
                    <span>信息系数信息比率<TermHint term="icir" /></span>
                    <strong>{{ formatNumber(rankingRecommendation.icir, 2) }}</strong>
                  </div>
                  <div>
                    <span>分段同号<TermHint term="segment" /></span>
                    <strong>{{ rankingRecommendation.same_sign_segments }} / {{ rankingRecommendation.segments }}</strong>
                  </div>
                  <div>
                    <span>前 {{ rankingReport.top_n }} 名提升<TermHint term="topNLift" /></span>
                    <strong>{{ formatPercent(rankingRecommendation.top_n_lift) }}</strong>
                  </div>
                </div>
                <div v-if="rankingQuantiles.length" class="ranking-quantiles">
                  <span>分层收益<TermHint term="quantile" /></span>
                  <span v-for="(value, index) in rankingQuantiles" :key="index" class="ranking-quantile">
                    <small>第 {{ index + 1 }} 档</small>{{ value }}
                  </span>
                </div>
                <p class="ranking-note">{{ rankingRecommendation.note }}</p>
              </template>
              <p v-else class="ranking-note" data-testid="condition-ranking-reason">
                {{ rankingReport.reason ?? '这次研究没有形成可用的排序结论。' }}
              </p>
            </div>
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
              <div class="table-wrap">
                <table>
                  <caption class="sr-only">买卖条件候选验证结果</caption>
                  <thead><tr><th>选择</th><th>方向</th><th>新增条件</th><th>样本 / 股票</th><th>训练 lift</th><th>前 N 名胜率<TermHint term="topNWinRate" /></th><th title="买入按基础规则入场日计，卖出按持仓期提前离场决策点计（全市场合并，分母见结果字段 sample_totals）">条件命中率</th><th title="验证期合并成交笔数 / 你设置的最少交易样本">验证成交 / 门槛</th><th>验证胜率</th><th>全区间胜率</th><th>股票胜率提升率</th><th>盈亏比</th><th>留出胜率</th><th>状态</th></tr></thead>
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
                      <td>{{ formatPercent(candidate.validation.per_stock.improved_stock_ratio) }}</td>
                      <td>{{ formatNumber(candidate.validation.pooled_trades.payoff_ratio) }}</td>
                      <td>{{ formatPercent(candidate.holdout?.pooled_trades.win_rate) }}</td>
                      <td class="condition-status">
                        <span :class="candidate.passed_constraints ? 'positive' : 'muted-code'">{{ constraintText(candidate) }}</span>
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

.ranking-advice {
  display: grid;
  gap: 10px;
  margin-top: 18px;
  padding: 14px 16px;
  border: 1px solid var(--border);
  border-left: 2px solid var(--accent);
  background: rgba(34, 197, 94, 0.05);
}

.ranking-advice-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.ranking-confidence {
  padding: 2px 8px;
  border: 1px solid var(--border-strong);
  border-radius: 999px;
  color: var(--muted);
  font-size: 11px;
}

.ranking-advice-line {
  margin: 0;
  color: var(--text);
  font-size: 14px;
}

.link-button {
  margin-left: 8px;
  padding: 0;
  border: 0;
  background: none;
  color: var(--accent);
  font-size: 13px;
  cursor: pointer;
  text-decoration: underline;
}

.ranking-evidence {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 10px;
}

.ranking-evidence > div {
  display: grid;
  gap: 3px;
}

.ranking-evidence span {
  color: var(--subtle);
  font-size: 11px;
}

.ranking-evidence strong {
  font-size: 15px;
  font-weight: 500;
}

.ranking-quantiles {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 10px;
  color: var(--subtle);
  font-size: 11px;
}

.ranking-quantile {
  color: var(--text);
  font-size: 13px;
}

.ranking-quantile small {
  margin-right: 4px;
  color: var(--subtle);
  font-size: 10px;
}

.ranking-note,
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
  .condition-fields-top,
  .condition-stock-controls {
    grid-template-columns: 1fr;
  }
}
</style>
