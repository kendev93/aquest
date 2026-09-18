<script setup lang="ts">
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'

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
import TermHint from '../components/TermHint.vue'
import type {
  BacktestExecution,
  ConditionResearchCandidate,
  ConditionResearchJobState,
  ConditionResearchPayload,
  ConditionResearchRanking,
  ConditionResearchReport,
  ConditionResearchStockResult,
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
import { parseStoredRuleGroup, valueOptionLabel } from '../utils/formulaScreen'
import { formatChinaTimestamp } from '../utils/time'

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
  holdoutCandidates: 1,
  // 0 = 不做检验。默认 20 次在典型样本量下只多花不到 1 秒。
  significancePermutations: 20,
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
const customParseLoading = ref(false)
const customParseError = ref('')
const showAdvanced = ref(false)
const job = ref<ConditionResearchJobState | null>(null)
const report = ref<ConditionResearchReport | null>(null)
const history = ref<ResearchHistorySummary[]>([])
const historyLoading = ref(false)
const selectedHistoryId = ref('')
const selectedStockView = ref('baseline:full')
// 结果里勾选的候选条件，直接追加进基础规则后再跑一次研究。
const selectedCandidateIds = ref<string[]>([])
// 排序规则：研究不建组合，但结论要落到组合回测的「候选池 + 有限槽位」模型上。
const rankingValue = ref('')
const rankingOrder = ref<RankOrder>('desc')
const rankingTopN = ref(5)
// 条件研究页 → 组合回测页的桥接，走独立的键，避免和策略实验室的回填互相覆盖。
const RESEARCH_APPLY_KEY = 'indicator-lab.condition-research.apply'
// 「持有到期末」：不设基础卖出条件，让研究以「一直持有」为基准去搜索卖点。
const holdToEnd = ref(false)
// 「持续满仓」：持有到期末的对偶。不设基础买入条件，让研究以「天天买」为基准去搜索买点。
const alwaysInMarket = ref(false)
// 正在把历史记录回填进表单：此时不要让它触发的 watch 改写费用或板块。
const restoring = ref(false)
const stockFilter = ref('')
const stockSort = ref<'win_rate' | 'payoff_ratio' | 'total_return' | 'sharpe' | 'code'>('win_rate')
const stockPage = ref(1)
const stockPageSize = 50

const availableValues = computed<ValueDefinition[]>(() => (
  mode.value === 'custom'
    ? customMetadata.value?.values ?? []
    : metadata.value?.indicators.flatMap((indicator) => indicator.values ?? []) ?? []
))
// 排序下拉只列能跨股票比较的输出：按收盘价从高到低排序只是「谁股价贵谁排前面」。
const rankableValues = computed<ValueDefinition[]>(() => (
  availableValues.value.filter((value) => value.rankable === true)
))
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
const RANKING_CONFIDENCE_LABELS: Record<string, string> = {
  high: '证据充分',
  medium: '可用，建议先复验',
  low: '证据不足',
  none: '暂无法判断',
}
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
const STOCK_BOARD_OPTIONS: { key: InstrumentBoard; label: string }[] = [
  { key: 'main', label: '主板' },
  { key: 'star', label: '科创板' },
  { key: 'chinext', label: '创业板' },
  { key: 'b_share', label: 'B 股' },
]
const STOCK_COSTS = { commission: 0.0001, stampTax: 0.001 }
const FUND_COSTS = { commission: 0.00005, stampTax: 0 }
watch(() => form.instrumentType, (next, previous) => {
  if (restoring.value) return
  const from = previous === 'fund' ? FUND_COSTS : STOCK_COSTS
  const to = next === 'fund' ? FUND_COSTS : STOCK_COSTS
  if (form.commission === from.commission) form.commission = to.commission
  if (form.stampTax === from.stampTax) form.stampTax = to.stampTax
  if (next === 'fund') form.boards = []
})
const scopeNoun = computed(() => (form.instrumentType === 'fund' ? 'ETF' : '股票'))
const scopeHelper = computed(() => (
  form.instrumentType === 'fund'
    ? '每只 ETF 独立运行同一套规则，不设置跨标的资金分配或持仓数量上限。ETF 免印花税，佣金默认万 0.5、最低 0.1 元。'
    : '每只股票独立运行同一套规则，不设置跨股票资金分配或持仓数量上限。'
))
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

// 新报告（或历史记录）到达时必须回到第一页：沿用旧页码会出现
// 「第 3 / 1 页 + 空表体」，既不显示数据也不命中空态提示。
watch(() => report.value, () => {
  stockPage.value = 1
})

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
  if (!Number.isInteger(form.holdoutCandidates) || form.holdoutCandidates < 1 || form.holdoutCandidates > 3) {
    next.holdoutCandidates = '最终留出候选数必须在 1 到 3 之间。'
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
    holdout_candidates: form.holdoutCandidates,
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
    boards: form.instrumentType === 'fund' ? [] : [...form.boards],
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
      // 完成后把下拉切到这条新记录。否则它还停在挂载时选中的旧记录上，
      // 看起来就像是这次研究没被保存。
      if (history.value.some((item) => item.history_id === jobId)) {
        selectedHistoryId.value = jobId
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
 * 后端存的是解析后的请求：日期已经是具体区间，板块在 ETF 下已被清空，所以
 * 这里还原的就是"当时真正跑的那套配置"，而不是用户填的原始模式。回填期间
 * 置位 ``restoring``，避免证券类型的 watch 顺手改写费用和板块。
 */
async function applyStoredRequest(raw: unknown): Promise<void> {
  if (!raw || typeof raw !== 'object') return
  const request = raw as Record<string, unknown>
  restoring.value = true
  try {
    const types = Array.isArray(request.instrument_types) ? request.instrument_types : []
    const type = types.find(
      (item): item is InstrumentType => item === 'stock' || item === 'fund',
    )
    if (type) form.instrumentType = type
    if (request.universe === 'all' || request.universe === 'sh' || request.universe === 'sz') {
      form.universe = request.universe
    }
    form.boards = type === 'fund' ? [] : storedBoards(request.boards)

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
    form.holdoutCandidates = finiteNumber(request.holdout_candidates, form.holdoutCandidates)
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

async function loadHistoryDetail(): Promise<void> {
  if (!selectedHistoryId.value) return
  try {
    const saved = await getConditionResearchHistory(selectedHistoryId.value)
    await applyStoredRequest(saved.request)
    report.value = saved.result
    selectedStockView.value = 'baseline:full'
    clearCandidateSelection()
    message.value = '已加载历史买卖条件研究，并还原当时的研究配置。'
  } catch (error) {
    message.value = apiMessage(error)
  }
}

// 历史记录的 created_at 存的是朴素 UTC 时间，直接渲染会比北京时间慢 8 小时，
// 让人误以为刚跑完的研究没保存。
function formatHistoryDate(value: string): string {
  return formatChinaTimestamp(value)
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

// 买卖两侧的样本口径不同：买样本 = 每次入场 1 条，卖样本 = 持仓期内每个
// 提前离场决策点 1 条（同一笔交易可贡献多条）。因此卖出候选不能拿成交笔数
// 当分母，否则命中率会被高估甚至超过 100%。
function hitRateDenominator(candidate: ConditionResearchCandidate): number {
  const side = candidate.side === 'sell' ? 'sell' : 'buy'
  const sampleCount = report.value?.sample_totals?.[side]?.count
  if (typeof sampleCount === 'number' && sampleCount > 0) return sampleCount
  // 兼容没有 sample_totals 的历史结果。
  return report.value?.baseline.train.pooled_trades.closed_trades ?? 0
}

function conditionHitRate(candidate: ConditionResearchCandidate): string {
  const denominator = hitRateDenominator(candidate)
  if (!denominator) return '—'
  return ((candidate.mining.support_count / denominator) * 100).toFixed(3) + '%'
}

function hitRateTitle(candidate: ConditionResearchCandidate): string {
  return candidate.side === 'sell'
    ? '该条件在持仓期提前离场决策点上的命中比例（全市场合并）'
    : '该条件在基础规则入场日上的命中比例（全市场合并）'
}

/** 前 N 名口径胜率：只统计每个信号日排序后真会成交的那批样本。 */
function topNWinRate(candidate: ConditionResearchCandidate): string {
  const rate = candidate.mining.top_n_outcome_rate
  return rate === null || rate === undefined ? '—' : formatPercent(rate)
}

function topNTitle(candidate: ConditionResearchCandidate): string {
  const slots = report.value?.ranking?.top_n
  const days = candidate.mining.top_n_filtered_days ?? 0
  if (candidate.mining.top_n_outcome_rate === null || candidate.mining.top_n_outcome_rate === undefined) {
    // 卖出候选本就不参与该诊断（槽位竞争只发生在入场，卖出是触发式的），
    // 不能统一说成「没解析出排序指标」—— 上方卡片明明给了推荐。
    if (candidate.side === 'sell') {
      return '槽位竞争只发生在入场，卖出是触发式的，没有「先卖谁」这回事，因此卖出候选不做前 N 名诊断。'
    }
    return '这次研究没有解析出排序指标，无法按前 N 名口径评估。'
  }
  if (days === 0) {
    return `没有任何一天的命中数超过槽位数（${slots ?? '—'} 个），排序没起作用，这一列等于全池胜率。`
  }
  return (
    `有 ${days} 个交易日的命中数超过槽位数（${slots ?? '—'} 个），` +
    '这一列只统计这些日子里排序靠前、真正会成交的样本。'
  )
}

/** 噪声门槛：随机打乱标签重挖若干次，纯运气能把「最好的那个条件」推到多高的 lift。 */
function significanceTitle(candidate: ConditionResearchCandidate): string {
  const threshold = candidate.mining.lift_threshold
  if (threshold === null || threshold === undefined) {
    return '这次研究没有做置换检验，无法判断这个提升是不是运气。'
  }
  const pvalue = candidate.mining.permutation_pvalue
  const p = pvalue === null || pvalue === undefined ? '—' : pvalue.toFixed(3)
  return (
    `把交易结果随机打乱重新挖掘，纯运气最好的一次能把 lift 推到 ${threshold.toFixed(3)}。` +
    `这个候选的 lift 是 ${candidate.mining.lift.toFixed(3)}，随机重挖里有 p=${p} 的比例达到或超过它。`
  )
}

function significanceLabel(candidate: ConditionResearchCandidate): string {
  if (candidate.mining.significant === true) return '显著'
  if (candidate.mining.significant === false) return '疑似噪声'
  return ''
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
    return `噪声门槛 ${threshold.toFixed(3)}：${candidates.length} 个候选里有 ${passed} 个超过了纯运气能达到的水平。`
  }
  return (
    `噪声门槛 ${threshold.toFixed(3)}：没有一个候选超过随机打乱能达到的水平，` +
    '这次挖到的条件很可能是运气，建议扩大股票范围、延长时间区间或放宽基础规则以增加样本。'
  )
})

function validationTrades(candidate: ConditionResearchCandidate): string {
  const trades = candidate.validation.pooled_trades.closed_trades
  const threshold = report.value?.min_trades
  return threshold ? trades + ' / ' + threshold : String(trades)
}

function constraintText(candidate: ConditionResearchCandidate): string {
  if (candidate.passed_constraints) return '通过验证'
  const failures = candidate.constraint_failures ?? []
  return failures.length ? failures.join(' · ') : '未入围'
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
                  <option value="fund">ETF / 基金</option>
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
            <div v-if="form.instrumentType === 'stock'" class="scope-options condition-board-options" data-testid="condition-board-scope">
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
                <label for="condition-holdout-candidates">最终留出候选/侧</label>
                <input id="condition-holdout-candidates" v-model.number="form.holdoutCandidates" data-testid="condition-holdout-candidates" type="number" min="1" max="3" step="1">
              </div>
              <div>
                <label for="condition-permutations">噪声门槛检验次数<TermHint term="噪声门槛检验次数" /></label>
                <input id="condition-permutations" v-model.number="form.significancePermutations" data-testid="condition-permutations" type="number" min="0" max="500" step="10">
              </div>
            </div>
            <p v-if="errors.significancePermutations" class="field-error">{{ errors.significancePermutations }}</p>
            <div class="compact-fields condition-fields-top">
              <div><label>主目标</label><p class="field-static">跨股票交易胜率提升</p></div>
            </div>
            <p v-if="errors.holdoutCandidates" class="field-error">{{ errors.holdoutCandidates }}</p>
            <p v-if="errors.minInstruments" class="field-error">{{ errors.minInstruments }}</p>
            <p v-if="errors.candidateLimit" class="field-error">{{ errors.candidateLimit }}</p>
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
                      <td>{{ candidate.side === 'buy' ? '买入过滤' : '卖出触发' }}</td>
                      <td class="condition-rule">{{ conditionText(candidate) }}</td>
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
                      <td>
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
                {{ formatHistoryDate(item.created_at) }} · {{ item.history_id.slice(0, 8) }}
              </option>
            </select>
          </div>
          <button class="secondary-button load-history-button" data-testid="load-condition-history" type="button" :disabled="historyLoading || !selectedHistoryId" @click="loadHistoryDetail">加载历史研究</button>
          <p class="helper">加载时会一并还原当时的标的范围、买卖规则、日期区间和交易费用，方便在此基础上继续调整。</p>
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

.condition-history .compact-fields {
  align-items: end;
}

.condition-history .compact-fields .load-history-button {
  min-height: 40px;
  margin-top: 0;
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
