<script setup lang="ts">
import ResearchWorkDetails from '../components/ResearchWorkDetails.vue'
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'

import PageGuide from '../components/PageGuide.vue'
import { fetchStoreStatus } from '../api/marketData'
import {
  cancelStrategyPoolJob,
  createStrategyPoolJob,
  fetchResearchSettings,
  fetchStrategyPoolEntries,
  fetchStrategyPoolJobs,
  getStrategyPoolJob,
  saveResearchSettings,
  type StrategyPoolBoard,
} from '../api/strategyPool'
import { FormulaScreenApiError } from '../api/formulaScreen'
import type {
  DataStoreStatus,
  StrategyPoolCandidate,
  StrategyPoolBenchmark,
  StrategyPoolEntry,
  StrategyPoolHolding,
  StrategyPoolJobResult,
  StrategyPoolJobState,
  StrategyPoolJobSummary,
  StrategyPoolMetrics,
  StrategyPoolMode,
  StrategyPoolObjective,
  StrategyPoolSignal,
} from '../types'

const OBJECTIVE_STORAGE_KEY = 'indicator-lab.strategy-pool.objective'
const SIGNAL_PAGE_SIZE = 10
interface ActivePoolTickerProgress {
  symbol: string
  name: string
  progress: number
  workDetails?: Record<string, unknown>
  phaseLabel: string
  startedAt: number | null
}
type SellPanelItem =
  | { kind: 'rule'; key: string; signal: StrategyPoolSignal }
  | { kind: 'risk'; key: string; signal: Record<string, unknown> }

const objective = ref<StrategyPoolObjective | null>(null)
const entries = ref<StrategyPoolEntry[]>([])
const history = ref<StrategyPoolJobSummary[]>([])
const signalResult = ref<StrategyPoolJobResult | null>(null)
const marketStatus = ref<DataStoreStatus | null>(null)
const activeJobId = ref('')
const activeIntent = ref<StrategyPoolMode | ''>('')
const activeJob = ref<StrategyPoolJobState | null>(null)
const queuedSingleSymbol = ref('')
const pendingSingle = ref<StrategyPoolJobResult['single_candidate'] | null>(null)
const loading = ref(false)
const errorMessage = ref('')
const pollWarning = ref('')
const successMessage = ref('')
const riskTrials = ref<1_000 | 2_000 | 3_000>(3_000)
// 最长持仓交易日数（≈ 天数×7/5 的自然日）。20 交易日 ≈ 30 自然日。
const maxHoldDays = ref<number>(20)
// 研究范围（板块 + 过滤 ST）：只决定哪些票参与全市场研究，改动即时保存到后端，
// 下次提交任务自动带上。默认三板块全开、不过滤 ST——与未保存偏好时的行为一致。
const BOARD_OPTIONS: { id: StrategyPoolBoard; label: string }[] = [
  { id: 'main', label: '主板' },
  { id: 'chinext', label: '创业板' },
  { id: 'star', label: '科创板' },
]
const researchBoards = ref<StrategyPoolBoard[]>(['main', 'chinext', 'star'])
const excludeSt = ref(false)
const market = ref<'SH' | 'SZ'>('SH')
const code = ref('')
const searchQuery = ref('')
const sortColumn = ref<
  'status' | 'strategy_return' | 'annual_return' | 'sharpe' | 'win_rate' | 'payoff_ratio' | 'max_drawdown' | 'avg_holding_days' | 'benchmark_return' | 'created_return'
>('status')
const page = ref(1)
const pageSize = ref(50)
const elapsedClock = ref(Date.now())
let elapsedTimer: number | null = null
let pollTimer: number | null = null
let activePollToken = 0
let activeSignalTarget: { market: 'SH' | 'SZ'; code: string } | null = null

const isBusy = computed(() => Boolean(activeJobId.value))
watch(() => isBusy.value && ['full', 'single'].includes(activeIntent.value), (running) => {
  if (elapsedTimer !== null) window.clearInterval(elapsedTimer)
  elapsedTimer = null
  if (running) {
    elapsedClock.value = Date.now()
    elapsedTimer = window.setInterval(() => { elapsedClock.value = Date.now() }, 1000)
  }
})
const progress = computed(() => {
  const metadata = activeJob.value?.metadata ?? {}
  const value = metadata.overall_progress
  if (typeof value === 'number') return Math.max(0, Math.min(1, value))
  return Math.max(0, Math.min(1, activeJob.value?.progress ?? 0))
})
const progressPercent = computed(() => Math.round(progress.value * 100))
const phaseLabel = computed(() => {
  const metadata = activeJob.value?.metadata ?? {}
  const label = metadata.phase_label ?? metadata.current_symbol ?? activeJob.value?.phase
  return typeof label === 'string' ? label : '任务排队中'
})
const currentSymbol = computed(() => {
  const value = activeJob.value?.metadata?.current_symbol
  return typeof value === 'string' ? value : ''
})
const activeTickerProgress = computed<ActivePoolTickerProgress[]>(() => {
  if (!['full', 'single'].includes(activeIntent.value)) return []
  const metadata = activeJob.value?.metadata ?? {}
  const raw = metadata.active_symbols
  if (Array.isArray(raw)) {
    const active = raw.flatMap((item): ActivePoolTickerProgress[] => {
      if (!item || typeof item !== 'object') return []
      const progressItem = item as Record<string, unknown>
      if (typeof progressItem.symbol !== 'string') return []
      const progress = typeof progressItem.progress === 'number'
        ? Math.max(0, Math.min(1, progressItem.progress))
        : 0
      return [{
        symbol: progressItem.symbol,
        name: typeof progressItem.name === 'string' ? progressItem.name : '',
        progress,
        phaseLabel: typeof progressItem.phase_label === 'string' ? progressItem.phase_label : '研究中',
        startedAt: researchStartedAt(progressItem.started_at),
        workDetails: progressItem.work_details as Record<string, unknown> | undefined,
      }]
    })
    if (active.length) return active
  }
  const symbol = metadata.current_symbol ?? metadata.symbol
  const fraction = metadata.ticker_progress
  if (typeof symbol === 'string' && typeof fraction === 'number') {
    return [{
      symbol,
      name: typeof metadata.ticker_name === 'string' ? metadata.ticker_name : '',
      progress: Math.max(0, Math.min(1, fraction)),
      phaseLabel: typeof metadata.phase_label === 'string' ? metadata.phase_label : '研究中',
      startedAt: researchStartedAt(metadata.ticker_started_at),
      workDetails: metadata.work_details as Record<string, unknown> | undefined,
    }]
  }
  return []
})
const progressCountText = computed(() => {
  if (activeIntent.value !== 'full') return ''
  const metadata = activeJob.value?.metadata ?? {}
  const total = typeof metadata.total_symbols === 'number'
    ? metadata.total_symbols
    : activeJob.value?.total_candidates
  if (typeof total !== 'number') return ''
  const completed = typeof metadata.completed_symbols === 'number' ? metadata.completed_symbols : 0
  const skipped = typeof metadata.skipped_symbols === 'number' ? metadata.skipped_symbols : 0
  const failed = typeof metadata.failed_symbols === 'number' ? metadata.failed_symbols : 0
  const processed = typeof metadata.finished_symbols === 'number'
    ? metadata.finished_symbols
    : completed + failed
  const successful = Math.max(0, completed - skipped)
  const active = typeof metadata.current_parallelism === 'number'
    ? metadata.current_parallelism
    : activeTickerProgress.value.length
  return `已处理 ${processed} / ${total} 只（完成 ${successful} · 进行中 ${active} · 跳过 ${skipped} · 失败 ${failed}）`
})
const parallelismText = computed(() => {
  const metadata = activeJob.value?.metadata ?? {}
  const active = metadata.current_parallelism
  const maximum = metadata.maximum_parallelism
  const memoryLimit = metadata.memory_worker_limit
  const availableMemory = metadata.available_memory_mb
  const cpuLimit = metadata.cpu_worker_limit
  const cpuLoad = metadata.cpu_load_average_1m
  const visibleCpus = metadata.visible_cpus
  const cpuDetails: string[] = []
  if (typeof visibleCpus === 'number') cpuDetails.push(`${visibleCpus}核`)
  if (typeof cpuLoad === 'number') cpuDetails.push(`系统1分钟负载 ${cpuLoad.toFixed(1)}`)
  const cpuText = typeof cpuLimit === 'number'
    ? `CPU允许 ${cpuLimit} 个标的并发${cpuDetails.length ? `（${cpuDetails.join('，')}）` : ''}`
    : ''
  return typeof active === 'number' && typeof maximum === 'number'
    ? `标的并行 ${active} / 最大 ${maximum}${cpuText ? ` · ${cpuText}` : ''} · 内存动态上限 ${typeof memoryLimit === 'number' ? memoryLimit : maximum}${typeof availableMemory === 'number' ? `（可用约 ${availableMemory} MiB）` : ''}`
    : ''
})
const buySignals = computed(() => signalResult.value?.buy_signals ?? [])
const ruleSellSignals = computed(() => signalResult.value?.sell_signals ?? [])
const riskSellSignals = computed(() => signalResult.value?.model_portfolio?.risk_exit_signals ?? [])
const sellNextTradingDaySymbols = computed(() => new Set([
  ...ruleSellSignals.value.map((item) => item.symbol),
]))
const currentHoldings = computed<StrategyPoolHolding[]>(
  () => signalResult.value?.holding_scope === 'per_stock_strategy'
    && signalResult.value.holding_snapshot_complete === true
    ? signalResult.value.model_portfolio?.holdings ?? []
    : [],
)
const sellPanelItems = computed<SellPanelItem[]>(() => [
  ...ruleSellSignals.value.map((signal) => ({
    kind: 'rule' as const,
    key: `rule-${signal.symbol}`,
    signal,
  })),
  ...riskSellSignals.value.map((signal, index) => ({
    kind: 'risk' as const,
    key: `risk-${String(signal.market ?? '')}-${String(signal.code ?? '')}-${String(signal.date ?? index)}`,
    signal,
  })),
])
const heldSymbols = computed(() => new Set(
  currentHoldings.value.map((holding) => `${holding.market}${holding.code}`),
))
const currentHoldingReturns = computed(() => new Map(
  currentHoldings.value.map((holding) => [
    `${holding.market}${holding.code}`,
    holding.holding_return_pct,
  ]),
))
const signalAsOf = computed(() => signalResult.value?.as_of_date ?? '')
const buyPage = ref(1)
const sellPage = ref(1)
const holdingsPage = ref(1)
const visibleBuySignals = computed(() => pageItems(buySignals.value, buyPage.value))
const visibleSellSignals = computed(() => pageItems(sellPanelItems.value, sellPage.value))
const visibleCurrentHoldings = computed(() => pageItems(currentHoldings.value, holdingsPage.value))
const buyPageCount = computed(() => pageCount(buySignals.value.length))
const sellPageCount = computed(() => pageCount(sellPanelItems.value.length))
const holdingsPageCount = computed(() => pageCount(currentHoldings.value.length))

watch([buySignals, sellPanelItems, currentHoldings], () => {
  buyPage.value = 1
  sellPage.value = 1
  holdingsPage.value = 1
})

const filteredEntries = computed(() => {
  const query = searchQuery.value.trim().toLocaleLowerCase()
  if (!query) return entries.value
  return entries.value.filter((entry) =>
    `${entry.market}${entry.code} ${entry.code} ${entry.name ?? ''}`.toLocaleLowerCase().includes(query),
  )
})

const sortedEntries = computed(() => {
  const rows = [...filteredEntries.value]
  const metric = sortColumn.value
  if (metric === 'status') {
    return rows.sort((a, b) => {
      const order = statusOrder(entryStatus(a), entryIsHeld(a)) - statusOrder(entryStatus(b), entryIsHeld(b))
      return order || metricValue(b, objective.value ?? 'annual_return', objective.value) - metricValue(a, objective.value ?? 'annual_return', objective.value) || symbol(a).localeCompare(symbol(b))
    })
  }
  return rows.sort((a, b) => {
    const byMetric = metricValue(b, metric, objective.value) - metricValue(a, metric, objective.value)
    return byMetric || statusOrder(entryStatus(a), entryIsHeld(a)) - statusOrder(entryStatus(b), entryIsHeld(b)) || symbol(a).localeCompare(symbol(b))
  })
})
const totalPages = computed(() => Math.max(1, Math.ceil(sortedEntries.value.length / pageSize.value)))
const pageEntries = computed(() => {
  const start = (page.value - 1) * pageSize.value
  return sortedEntries.value.slice(start, start + pageSize.value)
})
const pageStart = computed(() => sortedEntries.value.length ? (page.value - 1) * pageSize.value + 1 : 0)
const pageEnd = computed(() => Math.min(page.value * pageSize.value, sortedEntries.value.length))



function selectObjective(value: StrategyPoolObjective): void {
  objective.value = value
  window.localStorage.setItem(OBJECTIVE_STORAGE_KEY, value)
  errorMessage.value = ''
  pollWarning.value = ''
  successMessage.value = ''
  signalResult.value = null
  void refreshPage(true)
}

async function refreshPage(_refreshSignals: boolean): Promise<void> {
  if (!objective.value) return
  loading.value = true
  errorMessage.value = ''
  try {
    const [nextEntries, nextHistory, nextMarketStatus] = await Promise.all([
      fetchStrategyPoolEntries(objective.value),
      fetchStrategyPoolJobs(),
      fetchStoreStatus().catch(() => null),
    ])
    entries.value = nextEntries
    marketStatus.value = nextMarketStatus
    page.value = Math.min(page.value, Math.max(1, Math.ceil(nextEntries.length / pageSize.value)))
    history.value = nextHistory
    await restoreLatestSignalResult(nextHistory)
  } catch (error) {
    errorMessage.value = apiMessage(error)
  } finally {
    loading.value = false
  }
}

async function refreshPoolEntries(canApply: () => boolean): Promise<boolean> {
  const selectedObjective = objective.value
  if (!selectedObjective) return false
  const nextEntries = await fetchStrategyPoolEntries(selectedObjective)
  if (!canApply() || objective.value !== selectedObjective) return false
  entries.value = nextEntries
  page.value = Math.min(page.value, Math.max(1, Math.ceil(nextEntries.length / pageSize.value)))
  return true
}

async function restoreLatestSignalResult(jobs: StrategyPoolJobSummary[]): Promise<void> {
  const rebuild = jobs.find((job) => job.mode === 'full' && job.rebuild)
  const cutoff = rebuild ? timestampMillis(rebuild.created_at) : 0
  const signalJobs = jobs.filter((job) =>
    (!cutoff || timestampMillis(job.created_at) > cutoff)
    && job.mode === 'signals'
    && job.status === 'completed'
    && (!job.objective || job.objective === objective.value || job.dual_objectives),
  )
  const baselineIndex = signalJobs.findIndex((job) => !job.market || !job.code)
  if (baselineIndex < 0) {
    if (cutoff) {
      signalResult.value = null
    }
    return
  }

  const baseline = await readCompletedSignalResult(signalJobs[baselineIndex]!)
  if (!baseline) return
  let restored = baseline
  const incrementalJobs = signalJobs
    .slice(0, baselineIndex)
    .filter((job) => job.market && job.code)
    .reverse()
  for (const job of incrementalJobs) {
    const update = await readCompletedSignalResult(job)
    if (!update) continue
    restored = mergeSignalResult(restored, update, {
      market: job.market as 'SH' | 'SZ',
      code: job.code ?? '',
    })
  }
  signalResult.value = restored
}

async function readCompletedSignalResult(
  job: StrategyPoolJobSummary,
): Promise<StrategyPoolJobResult | null> {
  try {
    const state = await getStrategyPoolJob(job.job_id)
    const result = state.result?.results_by_objective?.[objective.value ?? 'annual_return'] ?? state.result
    return state.status === 'completed'
      && result?.mode === 'signals'
      && result.objective === objective.value
      ? result
      : null
  } catch {
    // Older task results may have been pruned.
    return null
  }
}

function mergeSignalResult(
  previous: StrategyPoolJobResult,
  update: StrategyPoolJobResult,
  target: { market: 'SH' | 'SZ'; code: string },
): StrategyPoolJobResult {
  const targetSymbol = `${target.market}${target.code}`
  const mergeRows = (before: StrategyPoolSignal[] | undefined, after: StrategyPoolSignal[] | undefined) =>
    [...(before ?? []).filter((item) => item.symbol !== targetSymbol), ...(after ?? [])]
      .sort((left, right) => {
        const primary = objective.value ?? 'annual_return'
        const leftScore = left[primary] ?? Number.NEGATIVE_INFINITY
        const rightScore = right[primary] ?? Number.NEGATIVE_INFINITY
        return rightScore - leftScore || left.symbol.localeCompare(right.symbol)
      })
  const mergePositions = (
    before: StrategyPoolHolding[] | undefined,
    after: StrategyPoolHolding[] | undefined,
  ) => [
    ...(before ?? []).filter((item) => `${item.market}${item.code}` !== targetSymbol),
    ...(after ?? []),
  ]
  const mergeRiskExits = (
    before: Array<Record<string, unknown>> | undefined,
    after: Array<Record<string, unknown>> | undefined,
  ) => [
    ...(before ?? []).filter(
      (item) => `${String(item.market ?? '')}${String(item.code ?? '')}` !== targetSymbol,
    ),
    ...(after ?? []),
  ]
  const previousPortfolio = previous.model_portfolio ?? {}
  const updatedPortfolio = update.model_portfolio ?? {}
  const canMergeCompleteHoldings = previous.holding_scope === 'per_stock_strategy'
    && previous.holding_snapshot_complete === true
    && updatedPortfolio.holding_scope === 'per_stock_strategy'
  const targetReplayAvailable = updatedPortfolio.available !== false
  const holdings = canMergeCompleteHoldings
    ? targetReplayAvailable
      ? mergePositions(previousPortfolio.holdings, updatedPortfolio.holdings)
      : previousPortfolio.holdings ?? []
    : updatedPortfolio.holding_scope === 'per_stock_strategy'
      ? updatedPortfolio.holdings ?? []
      : []
  const riskExitSignals = mergeRiskExits(
    canMergeCompleteHoldings && targetReplayAvailable ? previousPortfolio.risk_exit_signals : [],
    targetReplayAvailable ? updatedPortfolio.risk_exit_signals : [],
  )
  const previousCurrentState = previousPortfolio.current_state ?? {}
  const updatedCurrentState = updatedPortfolio.current_state ?? {}
  return {
    ...previous,
    ...update,
    holding_scope: 'per_stock_strategy',
    holding_snapshot_complete: canMergeCompleteHoldings,
    buy_signals: mergeRows(previous.buy_signals, update.buy_signals),
    sell_signals: mergeRows(previous.sell_signals, update.sell_signals),
    states: mergeRows(previous.states, update.states),
    model_portfolio: {
      ...previousPortfolio,
      ...updatedPortfolio,
      available: Boolean(previousPortfolio.available || updatedPortfolio.available),
      holdings,
      risk_exit_signals: riskExitSignals,
      current_state: {
        ...previousCurrentState,
        ...updatedCurrentState,
        holdings,
      },
    },
  }
}

function activeReplayableCandidate(entry: StrategyPoolEntry): StrategyPoolCandidate | null {
  const candidate = entry.active_candidate
  return candidate
    && candidate.replayable !== false
    && candidate.buy_rule
    && candidate.sell_rule
    ? candidate
    : null
}

function timestampMillis(value: number | string | null | undefined): number {
  if (typeof value === 'number') return value < 1_000_000_000_000 ? value * 1_000 : value
  if (typeof value === 'string') return Date.parse(value) || 0
  return 0
}

function researchStartedAt(value: unknown): number | null {
  if (typeof value !== 'number' && typeof value !== 'string') return null
  const timestamp = timestampMillis(value)
  return Number.isFinite(timestamp) && timestamp > 0 ? timestamp : null
}

function formatResearchElapsed(startedAt: number | null): string {
  if (startedAt === null) return '--:--:--'
  const seconds = Math.max(0, Math.floor((elapsedClock.value - startedAt) / 1000))
  return [Math.floor(seconds / 3600), Math.floor(seconds / 60) % 60, seconds % 60]
    .map((value) => String(value).padStart(2, '0')).join(':')
}

async function loadResearchSettings(): Promise<void> {
  try {
    const settings = await fetchResearchSettings()
    const boards = settings.boards.filter((id) => BOARD_OPTIONS.some((option) => option.id === id))
    researchBoards.value = boards.length ? boards : BOARD_OPTIONS.map((option) => option.id)
    excludeSt.value = settings.exclude_st
  } catch {
    // 偏好读不到就用默认值，不打断页面初始化。
  }
}

async function persistResearchSettings(): Promise<void> {
  try {
    await saveResearchSettings({ boards: [...researchBoards.value], exclude_st: excludeSt.value })
  } catch (error) {
    errorMessage.value = apiMessage(error)
  }
}

function toggleBoard(id: StrategyPoolBoard): void {
  const next = researchBoards.value.includes(id)
    ? researchBoards.value.filter((item) => item !== id)
    : [...researchBoards.value, id]
  if (!next.length) {
    errorMessage.value = '至少要保留一个板块。'
    return
  }
  errorMessage.value = ''
  researchBoards.value = next
  void persistResearchSettings()
}

function toggleExcludeSt(): void {
  excludeSt.value = !excludeSt.value
  void persistResearchSettings()
}

async function startFullResearch(rebuild = true): Promise<void> {
  if (isBusy.value) return
  await startTask({
    mode: 'full',
    rebuild,
    risk_trials: riskTrials.value,
    max_hold_days: maxHoldDays.value || null,
    boards: [...researchBoards.value],
    exclude_st: excludeSt.value,
  }, 'full')
}

async function startSingleRerun(): Promise<void> {
  if (isBusy.value) return
  if (!/^\d{6}$/.test(code.value)) {
    errorMessage.value = '请输入 6 位证券代码。'
    return
  }
  pendingSingle.value = null
  await startTask({ mode: 'single', market: market.value, code: code.value, risk_trials: riskTrials.value, max_hold_days: maxHoldDays.value || null }, 'single')
}

async function rerunEntry(entry: StrategyPoolEntry): Promise<void> {
  if (queuedSingleSymbol.value) return
  if (!isBusy.value) {
    market.value = entry.market
    code.value = entry.code
    await startSingleRerun()
    return
  }
  if (activeIntent.value !== 'signals') return

  errorMessage.value = ''
  successMessage.value = ''
  try {
    await createStrategyPoolJob({
      mode: 'single',
      market: entry.market,
      code: entry.code,
      risk_trials: riskTrials.value,
      max_hold_days: maxHoldDays.value || null,
    })
    queuedSingleSymbol.value = symbol(entry)
    successMessage.value = `${entry.market} ${entry.code} 已排队，信号扫描完成后开始逐股重跑。`
    await loadHistory()
  } catch (error) {
    errorMessage.value = apiMessage(error)
  }
}

function rerunEntryDisabled(): boolean {
  return Boolean(queuedSingleSymbol.value) || (isBusy.value && activeIntent.value !== 'signals')
}

function rerunEntryTitle(entry: StrategyPoolEntry): string {
  if (queuedSingleSymbol.value) {
    return queuedSingleSymbol.value === symbol(entry) ? '该标的已排队重跑' : '已有逐股重跑在排队中'
  }
  if (isBusy.value && activeIntent.value === 'signals') return '点击后排在当天信号扫描之后重跑'
  if (isBusy.value) return '当前全市场研究或组合回测结束后可重跑'
  return `重跑 ${entry.market} ${entry.code}`
}

async function startSignalScan(target?: { market: 'SH' | 'SZ'; code: string }, automatic = false): Promise<void> {
  if (!objective.value || isBusy.value) return
  await startTask({ mode: 'signals', objective: objective.value, ...target, ...(automatic ? { automatic: true } : {}) }, 'signals')
}

async function startTask(
  payload: { mode: StrategyPoolMode; automatic?: boolean; rebuild?: boolean; risk_trials?: 1_000 | 2_000 | 3_000; max_hold_days?: number | null; market?: 'SH' | 'SZ'; code?: string; objective?: StrategyPoolObjective; boards?: StrategyPoolBoard[]; exclude_st?: boolean },
  intent: StrategyPoolMode,
): Promise<void> {
  errorMessage.value = ''
  pollWarning.value = ''
  successMessage.value = ''
  try {
    activeSignalTarget = payload.mode === 'signals' && payload.market && payload.code
      ? { market: payload.market, code: payload.code }
      : null
    const created = await createStrategyPoolJob(payload)
    if (created.skipped) {
      activeSignalTarget = null
      await loadHistory()
      return
    }
    if (payload.rebuild) {
      entries.value = []
      signalResult.value = null
    }
    activeJobId.value = created.job_id
    activeIntent.value = intent
    activeJob.value = { job_id: created.job_id, status: created.status, progress: 0 }
    void pollJob(created.job_id, intent)
  } catch (error) {
    errorMessage.value = apiMessage(error)
  }
}

async function pollJob(jobId: string, intent: StrategyPoolMode): Promise<void> {
  const token = ++activePollToken
  const canApply = () => token === activePollToken && activeJobId.value === jobId
  let refreshedFinishedCount = 0
  const poll = async (): Promise<void> => {
    if (!canApply()) return
    let refreshingEntries = false
    try {
      const state = await getStrategyPoolJob(jobId)
      if (!canApply()) return
      pollWarning.value = ''
      activeJob.value = state
      if (intent === 'full' && state.status === 'running') {
        const rawCount = state.metadata?.finished_symbols ?? state.total_scanned ?? 0
        const finishedCount = typeof rawCount === 'number' && Number.isFinite(rawCount)
          ? Math.max(0, Math.floor(rawCount))
          : 0
        if (finishedCount !== refreshedFinishedCount) {
          refreshingEntries = true
          if (await refreshPoolEntries(canApply)) refreshedFinishedCount = finishedCount
          if (!canApply()) return
        }
      }
      if (intent === 'full' && ['failed', 'cancelled'].includes(state.status)) {
        refreshingEntries = true
        await refreshPoolEntries(canApply)
        if (!canApply()) return
      }
      if (intent === 'single' && state.status === 'running') queuedSingleSymbol.value = ''
      if (['completed', 'failed', 'cancelled'].includes(state.status)) {
        const result = state.result?.results_by_objective?.[objective.value ?? 'annual_return'] ?? state.result ?? null
        if (intent === 'single') queuedSingleSymbol.value = ''
        activeJobId.value = ''
        activeIntent.value = ''
        if (pollTimer !== null) window.clearTimeout(pollTimer)
        pollTimer = null
        await finishJob(intent, state, result)
        return
      }
    } catch (error) {
      pollWarning.value = refreshingEntries
        ? `标的状态刷新暂时失败：${apiMessage(error)} 下一轮轮询会自动重试。`
        : `任务仍在后台运行，状态读取暂时失败：${apiMessage(error)} 正在自动重试。`
    }
    if (canApply()) pollTimer = window.setTimeout(() => void poll(), 1_500)
  }
  await poll()
}

async function finishJob(
  intent: StrategyPoolMode,
  state: StrategyPoolJobState,
  result: StrategyPoolJobResult | null,
): Promise<void> {
  if (state.status === 'completed') {
    if (intent === 'full') {
      await refreshPage(false)
      successMessage.value = '全市场策略重选已完成；每只股票的留出段表现仅用于复核。'
      return
    }
    if (intent === 'single') {
      pendingSingle.value = result?.single_candidate ?? null
      await refreshPage(false)
      if (pendingSingle.value?.status === 'insufficient_history') {
        successMessage.value = `${pendingSingle.value.market} ${pendingSingle.value.code} 历史样本不足，已记录；数据或研究配置变化后可再次重跑。`
      } else if (pendingSingle.value?.saved_to_pool) {
        successMessage.value = `${pendingSingle.value.market} ${pendingSingle.value.code} 重跑完成，年化和夏普自动候选已更新；已有手工覆盖仍优先生效。`
      } else {
        successMessage.value = '单股研究完成，结果尚未写入策略池；可从候选卡片保存为手工覆盖。'
      }
    } else if (intent === 'signals') {
      const requestedTarget = activeSignalTarget
      activeSignalTarget = null
      if (result?.objective === objective.value) {
        const previous = signalResult.value
        signalResult.value = requestedTarget && previous?.objective === result.objective
          ? mergeSignalResult(previous, result, requestedTarget)
          : result
        page.value = 1
      }
      successMessage.value = `当天信号已按 ${objectiveLabel(objective.value)} 目标更新。`
      await loadHistory()
      if (await activateQueuedSingleRerun()) return
    }
    return
  }
  if (state.status === 'cancelled') {
    successMessage.value = '任务已取消；已经完成的逐股策略检查点已保存。再次全量重选时会复用未变化的标的。'
  } else if (state.status === 'failed') {
    errorMessage.value = state.error || '策略池任务失败；已完成的逐股检查点仍可复用。'
  }
  if (intent === 'signals') {
    activeSignalTarget = null
  }
  await loadHistory()
  if (intent === 'signals') await activateQueuedSingleRerun()
}

async function loadHistory(): Promise<void> {
  try {
    history.value = await fetchStrategyPoolJobs()
  } catch (error) {
    errorMessage.value = apiMessage(error)
  }
}

async function activateQueuedSingleRerun(): Promise<boolean> {
  const queued = history.value.find(
    (job) => job.mode === 'single' && ['queued', 'running'].includes(job.status),
  )
  if (!queued) {
    queuedSingleSymbol.value = ''
    return false
  }
  queuedSingleSymbol.value = queued.market && queued.code ? `${queued.market}${queued.code}` : ''
  activeJobId.value = queued.job_id
  activeIntent.value = 'single'
  activeJob.value = {
    job_id: queued.job_id,
    status: queued.status,
    progress: 0,
    metadata: { phase_label: '排队等待策略 worker' },
  }
  successMessage.value = queuedSingleSymbol.value
    ? `${queuedSingleSymbol.value} 已排队，前序任务完成后开始逐股重跑。`
    : '逐股重跑已排队，前序任务完成后开始。'
  void pollJob(queued.job_id, 'single')
  return true
}

async function cancelCurrentJob(): Promise<void> {
  if (!activeJobId.value) return
  try {
    await cancelStrategyPoolJob(activeJobId.value)
    if (activeJob.value) activeJob.value.status = 'cancelling'
  } catch (error) {
    errorMessage.value = apiMessage(error)
  }
}

function detailHref(entry: StrategyPoolEntry): string {
  const list = new URLSearchParams({objective:objective.value ?? 'annual_return', search:searchQuery.value, sort:sortColumn.value, page:String(page.value), pageSize:String(pageSize.value)})
  return `/strategy-pool/${entry.market}/${entry.code}?objective=${objective.value ?? 'annual_return'}&list=${encodeURIComponent(list.toString())}`
}

function setSort(column: typeof sortColumn.value): void {
  sortColumn.value = column
  page.value = 1
}

function changePageSize(value: string): void {
  pageSize.value = Number(value) || 50
  page.value = 1
}

function entryStatus(entry: StrategyPoolEntry): string {
  const row = signalResult.value?.states?.find((item) => item.symbol === symbol(entry))
  const candidate = activeReplayableCandidate(entry)
  if (!candidate) return entry.status === 'insufficient_history' ? 'insufficient_history' : 'no_strategy'
  if (!row || row.status === 'no_strategy' || row.candidate_id !== candidate.candidate_id) {
    return 'signal_stale'
  }
  if (row.status === 'held') return 'flat'
  return row.status ?? 'signal_stale'
}

function entryIsHeld(entry: StrategyPoolEntry): boolean {
  return heldSymbols.value.has(symbol(entry))
}

function pageCount(count: number): number {
  return Math.max(1, Math.ceil(count / SIGNAL_PAGE_SIZE))
}

function pageItems<T>(items: T[], page: number): T[] {
  const start = (page - 1) * SIGNAL_PAGE_SIZE
  return items.slice(start, start + SIGNAL_PAGE_SIZE)
}

function holdingStatus(holding: StrategyPoolHolding): string {
  const tradeStatus = sellNextTradingDaySymbols.value.has(`${holding.market}${holding.code}`)
    ? '今日触发卖出 · 下一交易日卖出'
    : '当前持仓中'
  const holdingReturn = typeof holding.holding_return_pct === 'number'
    && Number.isFinite(holding.holding_return_pct)
    ? `持仓阶段收益 ${formatSignedPercent(holding.holding_return_pct)}`
    : ''
  return holdingReturn ? `${holdingReturn} · ${tradeStatus}` : tradeStatus
}

function holdingReturnText(signal: StrategyPoolSignal): string {
  const value = currentHoldingReturns.value.get(signal.symbol)
  return typeof value === 'number' && Number.isFinite(value)
    ? `当前持仓收益 ${formatSignedPercent(value)}`
    : ''
}

function signalMetricsSummary(signal: StrategyPoolSignal): string {
  const values: string[] = []
  if (typeof signal.full_period_return === 'number') {
    values.push(`全区间收益 ${formatSignedPercent(signal.full_period_return)}`)
  }
  if (typeof signal.total_return === 'number') {
    values.push(`验证收益 ${formatSignedPercent(signal.total_return)}`)
  }
  if (typeof signal.holdout_return === 'number') {
    values.push(`留出收益 ${formatSignedPercent(signal.holdout_return)}`)
  }
  if (typeof signal.annual_return === 'number') {
    values.push(`验证年化 ${formatPercent(signal.annual_return)}`)
  }
  if (typeof signal.sharpe === 'number') values.push(`夏普 ${formatNumber(signal.sharpe)}`)
  if (typeof signal.win_rate === 'number') values.push(`胜率 ${formatPercent(signal.win_rate)}`)
  if (typeof signal.payoff_ratio === 'number') values.push(`盈亏比 ${formatNumber(signal.payoff_ratio)}`)
  if (typeof signal.max_drawdown === 'number') {
    values.push(`验证回撤 ${formatPercent(signal.max_drawdown)}`)
  }
  if (signal.validation_trades != null || signal.holdout_trades != null) {
    values.push(
      `交易数 验证 ${signal.validation_trades == null ? '—' : formatNumber(signal.validation_trades, 0)} · 留出 ${signal.holdout_trades == null ? '—' : formatNumber(signal.holdout_trades, 0)}`,
    )
  }
  return values.join(' · ')
}

// 三个信号列表行内的策略质量指标（2026-10-09 用户需求）：全区间胜率 / 盈亏比 /
// 平均持仓天数，与「全市场标的状态」表格口径一致。从条目表按代码查找——跟踪
// 路径的信号行与持仓行本身不带指标，这样两条来源的行都能显示。
const entryQualityMap = computed(() => {
  const map = new Map<string, StrategyPoolMetrics>()
  for (const entry of entries.value) {
    const candidate =
      entry.active_candidate ??
      (objective.value ? entry.automatic_candidates?.[objective.value] : null)
    const full = candidate?.metrics?.full_period
    if (full) map.set(symbol(entry), full)
  }
  return map
})

function qualityText(key: string): string {
  const full = entryQualityMap.value.get(key)
  if (!full) return ''
  const parts: string[] = []
  if (typeof full.win_rate === 'number' && Number.isFinite(full.win_rate)) {
    parts.push(`全区间胜率 ${formatPercent(full.win_rate)}`)
  }
  if (typeof full.payoff_ratio === 'number' && Number.isFinite(full.payoff_ratio)) {
    parts.push(`全区间盈亏比 ${formatNumber(full.payoff_ratio)}`)
  }
  if (typeof full.avg_holding_days === 'number' && Number.isFinite(full.avg_holding_days)) {
    parts.push(`平均持仓 ${formatHoldingDays(full.avg_holding_days)}`)
  }
  return parts.join(' · ')
}

function riskExitReturnText(signal: Record<string, unknown>): string {
  const value = signal.trade_return_pct
  return typeof value === 'number' && Number.isFinite(value)
    ? `本次持仓收益 ${formatSignedPercent(value)}`
    : ''
}

// 默认排序档位：触发买入 → 触发卖出 → 持仓中 → 今日无买卖信号 → 待刷新 / 无策略 / 其他。
// 持仓中不带买卖信号时状态为 flat，需要靠 held 与「今日无买卖信号」区分开。
function statusOrder(value: string, held = false): number {
  if (value === 'buy' || value === 'buy_sell') return 0
  if (value === 'sell') return 1
  // 有买卖信号的持仓标的归入买卖档；其余持仓标的统一排在「持仓中」。
  if (held && value !== 'no_strategy' && value !== 'insufficient_history') return 2
  if (value === 'flat') return 3
  return { signal_stale: 4, no_strategy: 5 }[value] ?? 6
}

function metricValue(
  entry: StrategyPoolEntry,
  key: 'strategy_return' | 'annual_return' | 'sharpe' | 'win_rate' | 'payoff_ratio' | 'max_drawdown' | 'avg_holding_days' | 'benchmark_return' | 'created_return',
  target: StrategyPoolObjective | null,
): number {
  if (key === 'created_return') return entry.created_return ?? Number.NEGATIVE_INFINITY
  if (key === 'benchmark_return') {
    return benchmarkReturn(entry) ?? Number.NEGATIVE_INFINITY
  }
  const candidate = entry.active_candidate
    ?? (target ? entry.automatic_candidates[target] : null)
  // 表格展示统一用全区间实际回放口径；验证段指标只在策略选择时使用。
  const metricKey = key === 'strategy_return' ? 'total_return' : key
  const value = candidate?.metrics?.full_period?.[metricKey]
  return typeof value === 'number' && Number.isFinite(value) ? value : Number.NEGATIVE_INFINITY
}

function tradeCount(
  candidate: StrategyPoolCandidate | null | undefined,
  phase: 'validation' | 'holdout',
): number | null {
  return candidate?.metrics?.[phase]?.closed_trades
    ?? candidate?.metrics?.[phase]?.total_trades
    ?? null
}

function benchmarkReturn(entry: StrategyPoolEntry): number | null {
  const value = entry.benchmark?.full_period?.total_return
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function symbol(item: Pick<StrategyPoolEntry, 'market' | 'code'>): string {
  return `${item.market}${item.code}`
}

function statusLabel(value: string): string {
  return {
    buy: '触发买入',
    buy_sell: '买卖均触发',
    sell: '触发卖出',
    held: '持仓中',
    flat: '今日无买卖信号',
    signal_stale: '策略可用 · 信号待刷新',
    no_strategy: '无可用策略',
    insufficient_history: '历史样本不足',
    error: '扫描失败',
  }[value] ?? value
}

function objectiveLabel(value: StrategyPoolObjective | null): string {
  return value === 'sharpe' ? '夏普' : '年化收益'
}

function formatPercent(value: unknown): string {
  return typeof value === 'number' && Number.isFinite(value) ? `${(value * 100).toFixed(2)}%` : '—'
}

function formatSignedPercent(value: unknown): string {
  return typeof value === 'number' && Number.isFinite(value)
    ? `${value >= 0 ? '+' : ''}${(value * 100).toFixed(2)}%`
    : '—'
}

function formatNumber(value: unknown, digits = 2): string {
  return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(digits) : '—'
}

function formatHoldingDays(value: unknown): string {
  return typeof value === 'number' && Number.isFinite(value) ? `${value.toFixed(1)} 天` : '—'
}

function formatTimestamp(value: unknown): string {
  if (typeof value !== 'number' && typeof value !== 'string') return '—'
  const date = typeof value === 'number' ? new Date(value * 1000) : new Date(value)
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('zh-CN', { hour12: false })
}

function apiMessage(error: unknown): string {
  return error instanceof FormulaScreenApiError
    ? error.message
    : error instanceof Error
      ? error.message
      : '全市场策略池请求失败，请重试。'
}

async function initialize(): Promise<void> {
  void loadResearchSettings()
  const params = new URLSearchParams(window.location.search)
  searchQuery.value = params.get('search') ?? ''
  page.value = Math.max(1, Number(params.get('page') ?? 1))
  pageSize.value = Number(params.get('pageSize') ?? 50)
  const sort = params.get('sort')
  if (['status','strategy_return','annual_return','sharpe','win_rate','payoff_ratio','max_drawdown','avg_holding_days','benchmark_return','created_return'].includes(sort ?? '')) sortColumn.value = sort as typeof sortColumn.value
  const stored = params.get('objective') ?? window.localStorage.getItem(OBJECTIVE_STORAGE_KEY)
  if (stored === 'annual_return' || stored === 'sharpe') {
    objective.value = stored
    await refreshPage(false)
    const queuedSingle = history.value.find((item) => item.mode === 'single' && item.status === 'queued')
    queuedSingleSymbol.value = queuedSingle?.market && queuedSingle.code
      ? `${queuedSingle.market}${queuedSingle.code}`
      : ''
    // 信号轮询几分钟就有一次 running 窗口，全市场研究会连跑数小时。恢复
    // 时优先接回长任务，否则页面一刷新就会被顺手的信号扫描抢走进度显示，
    // 让人以为全市场研究停了。
    const running = history.value.find(
      (item) => ['full', 'single'].includes(item.mode) && item.status === 'running',
    )
      ?? history.value.find((item) => item.status === 'running')
      ?? history.value.find((item) => item.status === 'queued')
    if (running) {
      activeJobId.value = running.job_id
      activeIntent.value = running.mode
      activeSignalTarget = running.mode === 'signals' && running.market && running.code
        ? { market: running.market as 'SH' | 'SZ', code: running.code }
        : null
      void pollJob(running.job_id, running.mode)
    }
  }
}

onMounted(() => void initialize())
onUnmounted(() => {
  activePollToken += 1
  if (pollTimer !== null) window.clearTimeout(pollTimer)
  if (elapsedTimer !== null) window.clearInterval(elapsedTimer)
})
</script>

<template>
  <div class="screen-page strategy-pool-page" data-testid="strategy-pool-page">
    <header class="topbar">
      <a class="brand" href="/indicator-lab" aria-label="指标实验室首页"><img class="brand-mark" src="/indicator-lab-mark.png" alt="" aria-hidden="true" width="34" height="34"><span><strong>Indicator Lab</strong><small>指标实验室</small></span></a>
      <nav aria-label="主导航">
        <a class="nav-link" href="/indicator-lab">公式选股</a>
        <a class="nav-link" href="/portfolio-backtest">组合回测</a>
        <a class="nav-link" href="/strategy-lab">策略实验室</a>
        <a class="nav-link" href="/condition-research">买卖条件研究</a>
        <a class="nav-link" href="/individual-research">个股研究</a>
        <a class="nav-link active" href="/strategy-pool" aria-current="page">全市场策略池</a>
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
          <p class="eyebrow">MARKET / STRATEGY POOL</p>
          <h1>全市场策略池</h1>
          <p class="intro-copy">每只股票分别维护年化收益与夏普最高的策略，查看当天信号和逐股策略的分段表现。</p>
        </div>
        <div v-if="objective" class="intro-note">
          <span class="note-label">当前决策目标</span>
          <strong>{{ objectiveLabel(objective) }}</strong>
          <small>此目标决定每只股票采用哪套策略，也决定 5 个持仓槽的买入优先级。</small>
        </div>
      </section>

      <PageGuide page-key="strategy-pool" />

      <section v-if="!objective" class="pool-choice panel-card" data-testid="strategy-pool-first-choice">
        <p class="eyebrow">CHOOSE YOUR OBJECTIVE</p>
        <h2>你希望策略优先追求什么？</h2>
        <p>两种目标的最佳策略都会保存。这里的选择只决定当前列表、当天信号优先级和组合回测使用哪套策略，之后随时可以切换。</p>
        <div class="choice-buttons">
          <button data-testid="pool-select-annual" class="button-primary" @click="selectObjective('annual_return')">关注年化收益</button>
          <button data-testid="pool-select-sharpe" class="button-secondary" @click="selectObjective('sharpe')">关注夏普，控制波动</button>
        </div>
      </section>

      <template v-else>
        <section class="pool-toolbar panel-card" aria-label="策略池操作">
          <div class="toolbar-heading">
            <div>
              <p class="eyebrow">POOL CONTROL</p>
              <h2>研究与信号</h2>
            </div>
            <label class="objective-switch">当前目标
              <select :value="objective" data-testid="pool-objective" @change="selectObjective(($event.target as HTMLSelectElement).value as StrategyPoolObjective)">
                <option value="annual_return">年化收益</option>
                <option value="sharpe">夏普</option>
              </select>
            </label>
          </div>
          <div class="pool-actions">
            <div class="board-filter" data-testid="pool-board-filter">
              <label v-for="option in BOARD_OPTIONS" :key="option.id" class="board-option">
                <input
                  type="checkbox"
                  :checked="researchBoards.includes(option.id)"
                  :disabled="isBusy"
                  :data-testid="`pool-board-${option.id}`"
                  @change="toggleBoard(option.id)"
                >
                {{ option.label }}
              </label>
              <label class="board-option">
                <input
                  type="checkbox"
                  :checked="excludeSt"
                  :disabled="isBusy"
                  data-testid="pool-exclude-st"
                  @change="toggleExcludeSt"
                >
                过滤ST
              </label>
            </div>
            <label>每组规则最多尝试次数
              <select v-model.number="riskTrials" :disabled="isBusy" data-testid="pool-risk-trials">
                <option :value="1000">1000 组</option>
                <option :value="2000">2000 组</option>
                <option :value="3000">3000 组</option>
              </select>
            </label>
            <label class="ticker-input">最长持仓(交易日)
              <input v-model.number="maxHoldDays" type="number" min="1" max="10000" :disabled="isBusy" data-testid="pool-max-hold-days">
            </label>
            <button class="button-primary" data-testid="pool-start-full" :disabled="isBusy" @click="startFullResearch(true)">清空并重建全市场</button>
            <button class="button-secondary" data-testid="pool-start-incremental" :disabled="isBusy" @click="startFullResearch(false)">增量更新策略池</button>
            <button class="button-secondary" data-testid="pool-refresh-signals" :disabled="isBusy || !entries.some((item) => item.active_candidate)" @click="startSignalScan()">刷新当天信号</button>
            <label class="ticker-input">市场
              <select v-model="market" :disabled="isBusy"><option value="SH">SH</option><option value="SZ">SZ</option></select>
            </label>
            <label class="ticker-input">单股代码
              <input v-model="code" inputmode="numeric" maxlength="6" placeholder="例如 600000" :disabled="isBusy" data-testid="pool-single-code">
            </label>
            <button class="button-secondary" data-testid="pool-rerun-single" :disabled="isBusy" @click="startSingleRerun">逐股重跑</button>
          </div>

          <p class="empty-copy">后台在新收盘行情就绪后每日跟踪两套策略一次；打开页面只读取缓存。需要重算时请点击“刷新当天信号”。</p>
          <p class="empty-copy">清空重建会删除当前自动策略、手动覆盖和失败记录，从头逐只研究；增量更新保留已有策略并复用未变化的结果。</p>

          <div v-if="isBusy" class="pool-progress" data-testid="pool-job-progress">
            <div class="progress-heading"><strong>{{ activeIntent === 'signals' ? '正在扫描当天信号' : activeIntent === 'portfolio' ? '组合回测任务进行中' : activeIntent === 'single' ? '正在重跑单只股票' : '全市场研究进行中' }}</strong><strong>{{ progressPercent }}%</strong></div>
            <div class="progress-track"><span :style="{ width: `${progressPercent}%` }"></span></div>
            <p v-if="progressCountText" class="progress-count" data-testid="pool-progress-count">{{ progressCountText }}</p>
            <div v-if="activeTickerProgress.length" class="active-ticker-progress" data-testid="pool-active-ticker-progress" role="list" aria-label="正在研究的标的进度">
              <div v-for="item in activeTickerProgress" :key="item.symbol" class="active-ticker-progress-row" :data-testid="`pool-active-symbol-${item.symbol}`" role="listitem">
                <div class="active-ticker-progress-heading"><strong>{{ item.symbol }}<template v-if="item.name"> · {{ item.name }}</template></strong><span>{{ item.phaseLabel }} · {{ Math.round(item.progress * 100) }}%<span class="ticker-elapsed" :data-testid="`pool-elapsed-${item.symbol}`"> · 耗时 {{ formatResearchElapsed(item.startedAt) }}</span></span></div>
                <ResearchWorkDetails :details="item.workDetails" />
                <div class="active-ticker-progress-track" role="progressbar" :aria-label="`${item.symbol}${item.name ? ` ${item.name}` : ''}研究进度`" :aria-valuenow="Math.round(item.progress * 100)" aria-valuemin="0" aria-valuemax="100"><span :style="{ width: `${Math.round(item.progress * 100)}%` }"></span></div>
              </div>
            </div>
            <div class="progress-detail"><span>{{ phaseLabel }}</span><span v-if="currentSymbol">当前：{{ currentSymbol }}</span><span v-if="parallelismText" data-testid="pool-parallelism">{{ parallelismText }}</span><button class="text-button danger-text" data-testid="pool-cancel-job" @click="cancelCurrentJob">取消任务</button></div>
          </div>
          <p class="pool-scale-note">全量重选会逐只保存检查点，并按 CPU 负载、可用内存和股票历史长度自动调节并发；重新运行时会跳过行情和配置指纹未变化的股票。3000 组会让每只股票的退出风控计算更充分，实际耗时取决于本地标的数和历史长度。</p>
        </section>

        <p v-if="errorMessage" class="notice error-notice" role="alert">{{ errorMessage }}</p>
        <p v-if="successMessage" class="notice success-notice" role="status">{{ successMessage }}</p>
        <p v-if="pollWarning && isBusy" class="notice poll-warning" role="status" data-testid="pool-poll-warning">{{ pollWarning }}</p>

        <section class="signal-grid">
          <article class="signal-panel panel-card" data-testid="pool-buy-signals">
            <div class="section-heading"><div><p class="eyebrow">BUY SIGNALS</p><h2>当天触发买入 <span>{{ buySignals.length }}</span></h2></div><small>{{ signalAsOf || '信号扫描后显示日期' }}</small></div>
            <div v-if="buySignals.length" class="signal-list">
              <div v-for="item in visibleBuySignals" :key="item.symbol" class="signal-row">
                <strong>{{ item.symbol }}<template v-if="item.name"> · {{ item.name }}</template></strong>
                <span>买入信号触发 · 下一交易日执行<template v-if="holdingReturnText(item)"> · {{ holdingReturnText(item) }}</template><template v-if="signalMetricsSummary(item)"> · {{ signalMetricsSummary(item) }}</template><template v-if="qualityText(item.symbol)"> · {{ qualityText(item.symbol) }}</template></span>
              </div>
            </div>
            <div v-if="buySignals.length > SIGNAL_PAGE_SIZE" class="signal-pagination" data-testid="pool-buy-pagination">
              <button class="button-secondary" :disabled="buyPage <= 1" @click="buyPage -= 1">上一页</button>
              <span>第 {{ buyPage }} / {{ buyPageCount }} 页 · 共 {{ buySignals.length }} 条</span>
              <button class="button-secondary" :disabled="buyPage >= buyPageCount" @click="buyPage += 1">下一页</button>
            </div>
            <p v-if="!buySignals.length" class="empty-copy">{{ signalResult ? '当前没有股票触发买入规则。' : '选择目标后点击“刷新当天信号”，扫描最新完整行情日。' }}</p>
          </article>
          <article class="signal-panel panel-card" data-testid="pool-sell-signals">
            <div class="section-heading"><div><p class="eyebrow">SELL SIGNALS</p><h2>当天触发卖出 <span>{{ ruleSellSignals.length + riskSellSignals.length }}</span></h2></div><small>{{ signalAsOf || '卖出条件和持仓风控退出' }}</small></div>
            <div v-if="ruleSellSignals.length || riskSellSignals.length" class="signal-list">
              <template v-for="item in visibleSellSignals" :key="item.key">
                <div v-if="item.kind === 'rule'" class="signal-row">
                  <strong>{{ item.signal.symbol }}<template v-if="item.signal.name"> · {{ item.signal.name }}</template></strong>
                  <span>卖出规则触发 · 下一交易日执行<template v-if="holdingReturnText(item.signal)"> · {{ holdingReturnText(item.signal) }}</template><template v-if="signalMetricsSummary(item.signal)"> · {{ signalMetricsSummary(item.signal) }}</template><template v-if="qualityText(item.signal.symbol)"> · {{ qualityText(item.signal.symbol) }}</template></span>
                </div>
                <div v-else class="signal-row risk-signal">
                  <strong>{{ item.signal.market }}{{ item.signal.code }}<template v-if="item.signal.name"> · {{ item.signal.name }}</template></strong>
                  <span>回测日内风控已触发：{{ item.signal.reason || item.signal.risk_trigger || '风控规则' }} · 按风控成交规则处理<template v-if="riskExitReturnText(item.signal)"> · {{ riskExitReturnText(item.signal) }}</template></span>
                </div>
              </template>
            </div>
            <div v-if="ruleSellSignals.length + riskSellSignals.length > SIGNAL_PAGE_SIZE" class="signal-pagination" data-testid="pool-sell-pagination">
              <button class="button-secondary" :disabled="sellPage <= 1" @click="sellPage -= 1">上一页</button>
              <span>第 {{ sellPage }} / {{ sellPageCount }} 页 · 共 {{ sellPanelItems.length }} 条</span>
              <button class="button-secondary" :disabled="sellPage >= sellPageCount" @click="sellPage += 1">下一页</button>
            </div>
            <p v-if="!sellPanelItems.length" class="empty-copy">{{ signalResult ? '当前没有卖出条件或持仓风控退出信号。' : '信号扫描会同时检查卖出规则，并复核逐股策略的风控退出。' }}</p>
          </article>
          <article class="signal-panel panel-card" data-testid="pool-current-holdings">
            <div class="section-heading"><div><p class="eyebrow">CURRENT HOLDINGS</p><h2>今日持仓中 <span>{{ currentHoldings.length }}</span></h2></div><small>{{ objectiveLabel(objective) }}目标逐股策略 · {{ signalAsOf || '信号扫描后显示日期' }}</small></div>
            <div v-if="currentHoldings.length" class="signal-list">
              <div v-for="holding in visibleCurrentHoldings" :key="`${holding.market}${holding.code}`" class="signal-row holding-row">
                <strong>{{ holding.market }}{{ holding.code }} · {{ holding.name || '—' }}</strong>
                <span>{{ holdingStatus(holding) }}<template v-if="qualityText(symbol(holding))"> · {{ qualityText(symbol(holding)) }}</template></span>
              </div>
            </div>
            <div v-if="currentHoldings.length > SIGNAL_PAGE_SIZE" class="signal-pagination" data-testid="pool-holdings-pagination">
              <button class="button-secondary" :disabled="holdingsPage <= 1" @click="holdingsPage -= 1">上一页</button>
              <span>第 {{ holdingsPage }} / {{ holdingsPageCount }} 页 · 共 {{ currentHoldings.length }} 条</span>
              <button class="button-secondary" :disabled="holdingsPage >= holdingsPageCount" @click="holdingsPage += 1">下一页</button>
            </div>
            <p v-if="!signalResult" class="empty-copy">信号扫描完成后显示各股对应策略的持仓状态。</p>
            <p v-else-if="signalResult.model_portfolio?.available === false" class="empty-copy">{{ signalResult.model_portfolio.reason || '当前无法计算逐股策略持仓。' }}</p>
            <p v-else-if="signalResult.holding_scope !== 'per_stock_strategy'" class="empty-copy">当前历史结果没有逐股持仓快照；请刷新当天信号后查看。</p>
            <p v-else-if="signalResult.holding_snapshot_complete !== true" class="empty-copy">{{ signalResult.model_portfolio?.failed_batches ? `逐股持仓回放有 ${signalResult.model_portfolio.failed_batches} 个批次失败，当前结果不完整。${signalResult.model_portfolio.reason || ''}` : signalResult.signal_target ? '这是单股增量结果，尚未覆盖全市场；请刷新当天信号查看完整持仓。' : '当前历史结果没有完整的逐股持仓快照；请刷新当天信号后查看。' }}</p>
            <p v-else-if="!currentHoldings.length" class="empty-copy">当前没有仍处于持仓状态的标的。</p>
          </article>
        </section>
        <p class="pool-scale-note">买入和卖出规则信号都在最新完整行情日收盘后判定，并于下一交易日执行。因此，今日触发卖出规则的标的当天仍列在持仓中，下一交易日卖出。止损、止盈按回测中的日线风控成交规则模拟。</p>

        <section class="pool-table-section panel-card">
          <div class="section-heading"><div><p class="eyebrow">DAILY STATUS</p><h2>全市场标的状态 <span>{{ entries.length.toLocaleString() }}</span></h2></div><span>{{ loading ? '正在读取策略池…' : `按${objectiveLabel(objective)}策略决策` }}</span></div>
          <p class="pool-scale-note">收益、年化、夏普、胜率、盈亏比、最大回撤均为该策略在完整历史区间实际回放（全区间）的指标；平均持仓天数为全区间已平仓交易按股数加权的平均持有天数，用于估算买入后大致需要持有多久。旧候选会在下次全市场重选时补算。策略的选择与排序目标仍按验证段年化/夏普决策，避免用未来数据选策略。全段基准显示个股买入持有涨跌；验证和留出分别用同区间基准计算总收益超额。</p>
          <div class="pool-search-row">
            <label class="pool-search">搜索标的
              <input v-model="searchQuery" type="search" placeholder="输入证券代码或中文名称" data-testid="pool-entry-search" @input="page = 1">
            </label>
            <small>匹配 {{ filteredEntries.length.toLocaleString() }} / {{ entries.length.toLocaleString() }} 只</small>
          </div>
          <div class="table-scroll">
            <table class="pool-table">
              <thead><tr>
                <th>持仓 / 今日信号</th><th>证券</th>
                <th><button @click="setSort('benchmark_return')">基准涨跌{{ sortColumn === 'benchmark_return' ? ' ↓' : '' }}</button></th>
                <th><button @click="setSort('created_return')">创建至今收益{{ sortColumn === 'created_return' ? ' ↓' : '' }}</button></th>
                <th><button @click="setSort('strategy_return')">策略收益{{ sortColumn === 'strategy_return' ? ' ↓' : '' }}</button></th>
                <th><button @click="setSort('annual_return')">年化{{ sortColumn === 'annual_return' ? ' ↓' : '' }}</button></th>
                <th><button @click="setSort('sharpe')">夏普{{ sortColumn === 'sharpe' ? ' ↓' : '' }}</button></th>
                <th><button @click="setSort('win_rate')">胜率{{ sortColumn === 'win_rate' ? ' ↓' : '' }}</button></th>
                <th><button @click="setSort('payoff_ratio')">盈亏比{{ sortColumn === 'payoff_ratio' ? ' ↓' : '' }}</button></th>
                <th><button @click="setSort('max_drawdown')">最大回撤{{ sortColumn === 'max_drawdown' ? ' ↓' : '' }}</button></th>
                <th><button @click="setSort('avg_holding_days')">平均持仓{{ sortColumn === 'avg_holding_days' ? ' ↓' : '' }}</button></th>
                <th>策略 / 风险标记</th><th>维护</th>
              </tr></thead>
              <tbody>
                <tr v-for="entry in pageEntries" :key="symbol(entry)" :data-testid="`pool-row-${entry.code}`">
                  <td><div class="status-stack"><span v-if="entryIsHeld(entry)" class="status-chip status-held">持仓中</span><span class="status-chip" :class="`status-${entryStatus(entry)}`">{{ statusLabel(entryStatus(entry)) }}</span></div></td>
                  <td><strong>{{ entry.market }} {{ entry.code }}</strong><small>{{ entry.name }}</small></td>
                  <td :data-testid="`pool-entry-benchmark-${entry.code}`">
                    <div v-if="entry.benchmark" class="benchmark-summary">
                      <div>全段基准 <b>{{ formatSignedPercent(benchmarkReturn(entry)) }}</b></div>
                    </div>
                    <span v-else class="empty-copy">下次全市场重选时补算</span>
                  </td>
                  <td :data-testid="`pool-created-return-${entry.code}`">{{ typeof entry.created_return === 'number' ? formatPercent(entry.created_return) : entry.tracking_status === 'pending' ? '待初始化' : entry.tracking_status === 'failed' ? '更新失败' : '—' }}<small v-if="entry.tracking_status === 'failed'">同步失败，保留上次结果</small><small v-if="entry.tracking_as_of_date">截至 {{ entry.tracking_as_of_date }}</small></td>
                  <td>{{ formatPercent(metricValue(entry, 'strategy_return', objective)) }}</td>
                  <td>{{ formatPercent(metricValue(entry, 'annual_return', objective)) }}</td>
                  <td>{{ formatNumber(metricValue(entry, 'sharpe', objective)) }}</td>
                  <td>{{ formatPercent(metricValue(entry, 'win_rate', objective)) }}</td>
                  <td>{{ formatNumber(metricValue(entry, 'payoff_ratio', objective)) }}</td>
                  <td>{{ formatPercent(metricValue(entry, 'max_drawdown', objective)) }}</td>
                  <td :data-testid="`pool-holding-days-${entry.code}`">{{ formatHoldingDays(metricValue(entry, 'avg_holding_days', objective)) }}</td>
                  <td><span v-if="entry.active_candidate?.quality?.badges?.length" class="badge-row"><i v-for="badge in entry.active_candidate.quality.badges" :key="badge">{{ badge }}</i></span><span v-else-if="entry.status === 'no_strategy'">无可计算的策略绩效</span><span v-else-if="entry.last_error" class="danger-text">{{ entry.last_error }}</span><span v-else>—</span><small v-if="entry.active_candidate?.metrics">交易数：验证 {{ formatNumber(tradeCount(entry.active_candidate, 'validation'), 0) }} · 留出 {{ formatNumber(tradeCount(entry.active_candidate, 'holdout'), 0) }} 笔</small><small v-if="entry.active_source === 'manual'">手工覆盖 · {{ objectiveLabel(objective) }}</small></td>
                  <td><a class="text-button" :href="detailHref(entry)" :data-testid="`pool-detail-${entry.code}`">详情</a><button :data-testid="`pool-rerun-entry-${entry.code}`" class="text-button" :disabled="rerunEntryDisabled()" :title="rerunEntryTitle(entry)" @click="void rerunEntry(entry)">{{ queuedSingleSymbol === symbol(entry) ? '排队中' : '重跑' }}</button></td>
                </tr>
                <tr v-if="!filteredEntries.length"><td colspan="12" class="empty-copy" data-testid="pool-entry-empty">{{ searchQuery.trim() && entries.length ? '没有符合代码或名称的标的。' : loading ? '读取中…' : '策略池为空，开始全市场研究后会逐只写入候选。' }}</td></tr>
              </tbody>
            </table>
          </div>
          <div class="pool-pagination" data-testid="pool-pagination">
            <span>显示 {{ pageStart }}–{{ pageEnd }} / {{ sortedEntries.length.toLocaleString() }} 只</span>
            <label>每页
              <select data-testid="pool-page-size" :value="pageSize" @change="changePageSize(($event.target as HTMLSelectElement).value)">
                <option value="25">25</option><option value="50">50</option><option value="100">100</option>
              </select>
            </label>
            <button class="button-secondary" :disabled="page <= 1" @click="page -= 1">上一页</button>
            <span>第 {{ page }} / {{ totalPages }} 页</span>
            <button class="button-secondary" :disabled="page >= totalPages" @click="page += 1">下一页</button>
          </div>
        </section>

        <section v-if="pendingSingle" class="pending-candidates panel-card" data-testid="pool-single-candidates">
          <div class="section-heading"><div><p class="eyebrow">SINGLE RERUN</p><h2>{{ pendingSingle.market }} {{ pendingSingle.code }} · 重跑结果</h2></div><span>{{ pendingSingle.status === 'insufficient_history' ? pendingSingle.error : pendingSingle.saved_to_pool ? '年化与夏普自动候选已保存；已有手工覆盖仍优先生效' : '历史候选尚未写入自动策略，可保存为手工覆盖' }}</span></div>
          <div class="candidate-apply-grid">
            <article v-for="target in (['annual_return', 'sharpe'] as const)" :key="target">
              <strong>{{ objectiveLabel(target) }}最佳</strong>
              <p v-if="pendingSingle[target === 'annual_return' ? 'annual_candidate' : 'sharpe_candidate']">验证年化 {{ formatPercent(pendingSingle[target === 'annual_return' ? 'annual_candidate' : 'sharpe_candidate']?.metrics?.validation?.annual_return) }} · 夏普 {{ formatNumber(pendingSingle[target === 'annual_return' ? 'annual_candidate' : 'sharpe_candidate']?.metrics?.validation?.sharpe) }}</p>
              <p v-else>没有可计算的候选。</p>
            </article>
          </div>
        </section>

        <section class="pool-history panel-card">
          <div class="section-heading"><div><p class="eyebrow">TASK HISTORY</p><h2>策略池任务记录</h2></div><button class="text-button" @click="loadHistory">刷新</button></div>
          <div v-if="history.length" class="history-list">
            <div v-for="job in history.slice(0, 10)" :key="job.job_id" class="history-row"><strong>{{ job.mode === 'full' ? '全市场重选' : job.mode === 'single' ? `${job.market} ${job.code} 单股重跑` : job.mode === 'signals' ? '当天信号扫描' : `${job.mode} 组合回测` }}</strong><span>{{ job.status }} · {{ job.completed_symbols ?? 0 }}/{{ job.total_symbols ?? '—' }} 只完成 · 跳过 {{ job.skipped_symbols ?? 0 }} · 失败 {{ job.failed_symbols ?? 0 }}</span><small>{{ formatTimestamp(job.created_at) }}</small></div>
          </div>
          <p v-else class="empty-copy">完成或启动的任务会保存在本地历史中。取消任务后，再次全量重选会继续复用已完成标的。</p>
        </section>


      </template>
    </main>
  </div>
</template>

<style scoped>
.strategy-pool-page { min-height: 100vh; }
.strategy-pool-page .content-shell { display: grid; gap: 20px; }
.panel-card { border: 1px solid var(--line, #25364f); background: rgba(12, 20, 36, .78); padding: 22px; }
.pool-choice { max-width: 900px; margin: 24px auto; text-align: center; }
.pool-choice h2, .toolbar-heading h2, .section-heading h2 { margin: 6px 0 10px; }
.pool-choice p:not(.eyebrow) { color: var(--muted, #91a1b8); }
.choice-buttons, .pool-actions, .editor-actions { display: flex; flex-wrap: wrap; align-items: end; gap: 12px; }
.choice-buttons { justify-content: center; margin-top: 24px; }
.button-primary, .button-secondary { border: 1px solid #37506f; border-radius: 8px; padding: 11px 16px; font: inherit; font-weight: 700; cursor: pointer; }
.button-primary { background: #18c467; border-color: #18c467; color: #07170e; }
.button-secondary { background: #131f34; color: #e9f0fa; }
button:disabled { opacity: .48; cursor: not-allowed; }
.toolbar-heading, .section-heading, .progress-heading, .progress-detail { display: flex; justify-content: space-between; align-items: center; gap: 16px; }
.toolbar-heading { margin-bottom: 18px; }
.objective-switch, .pool-actions label, .pool-editor label { display: grid; gap: 7px; color: #9baac0; font-size: .9rem; }
.pool-actions select, .pool-actions input, .objective-switch select, .pool-editor select, .pool-editor textarea { min-height: 42px; border: 1px solid #354b69; border-radius: 7px; padding: 9px 11px; background: #111c30; color: #edf2fb; font: inherit; }
.pool-actions .ticker-input input { width: 140px; }
.board-filter { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; padding: 8px 12px; border: 1px solid #354b69; border-radius: 7px; background: #111c30; }
.board-option { display: flex; align-items: center; gap: 5px; color: #9baac0; font-size: .9rem; }
.board-option input { accent-color: #18c467; }
.pool-progress { margin-top: 22px; padding: 16px; border: 1px solid #293c58; }
.progress-track { height: 8px; overflow: hidden; margin: 12px 0; background: #17243a; }
.progress-track span { display: block; height: 100%; background: #18c467; transition: width .2s ease; }
.progress-count { margin: 0 0 12px; color: #acbbd0; font-size: .88rem; }
.active-ticker-progress { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 8px; margin-bottom: 12px; }
.active-ticker-progress-row { display: grid; gap: 7px; padding: 9px 10px; border: 1px solid #25364f; background: #101a2d; }
.active-ticker-progress-heading { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 10px; color: #aab9ce; font-size: .8rem; }
.active-ticker-progress-heading strong { color: #eef4fc; }
.active-ticker-progress-heading span { text-align: right; }
.ticker-elapsed { font-variant-numeric: tabular-nums; white-space: nowrap; }
.active-ticker-progress-track { height: 5px; overflow: hidden; background: #17243a; }
.active-ticker-progress-track span { display: block; height: 100%; background: #18c467; transition: width .25s ease; }
.progress-detail { color: #91a1b8; font-size: .9rem; }
.pool-scale-note, .empty-copy { color: #8798b1; font-size: .9rem; line-height: 1.6; }
.pool-scale-note { margin: 16px 0 0; }
.pool-search-row { display: flex; flex-wrap: wrap; align-items: end; justify-content: space-between; gap: 12px; margin: 16px 0 12px; color: #8798b1; font-size: .88rem; }
.pool-search { display: grid; width: min(420px, 100%); gap: 7px; color: #9baac0; }
.pool-search input { min-height: 42px; border: 1px solid #354b69; border-radius: 7px; padding: 9px 11px; background: #111c30; color: #edf2fb; font: inherit; }
.signal-grid, .candidate-apply-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
.signal-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.section-heading { margin-bottom: 16px; }
.section-heading h2 span { color: #18c467; }
.signal-panel { min-height: 180px; }
.signal-panel .section-heading small { color: #899ab3; white-space: nowrap; }
.signal-list, .history-list { display: grid; gap: 8px; }
.signal-pagination { display: flex; justify-content: flex-end; align-items: center; gap: 8px; margin-top: 12px; color: #91a1b8; font-size: .82rem; }
.signal-pagination .button-secondary { padding: 6px 9px; font-size: .82rem; }
.signal-row, .history-row { display: grid; gap: 5px; padding: 10px 12px; border-left: 3px solid #18c467; background: #111c30; }
.signal-row span, .history-row span, .history-row small { color: #9aaac0; font-size: .88rem; }
.risk-signal { border-left-color: #ff6a69; }
.holding-row { border-left-color: #6380a5; }
.candidate-apply-grid article { padding: 16px; border: 1px solid #263850; background: #101a2d; }
.table-scroll { overflow: auto; }
.pool-pagination { display: flex; flex-wrap: wrap; justify-content: flex-end; align-items: center; gap: 10px; margin-top: 14px; color: #91a1b8; font-size: .88rem; }
.pool-pagination label { display: flex; align-items: center; gap: 7px; }
.pool-pagination select { border: 1px solid #354b69; border-radius: 7px; padding: 6px 8px; background: #111c30; color: #edf2fb; font: inherit; }
.pool-table { width: 100%; min-width: 980px; border-collapse: collapse; text-align: left; }
.pool-table th, .pool-table td { border-bottom: 1px solid #25364f; padding: 12px 10px; vertical-align: top; }
.pool-table th { color: #8798b1; font-size: .88rem; white-space: nowrap; }
.pool-table th button { border: 0; padding: 0; background: none; color: inherit; font: inherit; cursor: pointer; }
.pool-table td small { display: block; margin-top: 5px; color: #899ab3; }
.benchmark-summary { display: grid; min-width: 250px; gap: 4px; color: #a8b8cc; font-size: .78rem; line-height: 1.45; white-space: nowrap; }
.benchmark-summary b { color: #eff4fc; }
.status-chip { display: inline-block; padding: 4px 8px; border: 1px solid #42516a; border-radius: 999px; color: #a9b5c7; font-size: .78rem; white-space: nowrap; }
.status-stack { display: flex; flex-wrap: wrap; gap: 5px; }
.status-buy, .status-buy_sell { border-color: #128f4c; color: #25d475; }
.status-sell, .status-error { border-color: #974d50; color: #ff7775; }
.status-held { border-color: #8b7b3c; color: #e0c55b; }
.badge-row { display: flex; flex-wrap: wrap; gap: 5px; }
.badge-row i { padding: 3px 6px; border-radius: 4px; background: #263348; color: #b9c7d9; font-size: .75rem; font-style: normal; }
.text-button { border: 0; padding: 4px 7px; background: none; color: #94a9c4; font: inherit; cursor: pointer; }
.danger-text { color: #ff7775; }
.notice { padding: 12px 15px; border: 1px solid #30435e; }
.error-notice { border-color: #984b50; color: #ff9b99; }
.success-notice { border-color: #14733f; color: #43d986; }
.poll-warning { border-color: #816b32; color: #e6ca73; }
.candidate-apply-grid { margin-top: 12px; }
.history-row { grid-template-columns: minmax(150px, 1fr) 2fr auto; align-items: center; border-left-color: #536b89; }
.editor-backdrop { position: fixed; inset: 0; z-index: 20; display: grid; place-items: center; padding: 24px; background: rgba(2, 7, 15, .78); }
.pool-editor { width: min(1080px, 100%); max-height: 90vh; overflow: auto; box-shadow: 0 24px 80px rgba(0, 0, 0, .5); }
.pool-editor .section-heading { margin-bottom: 18px; }
.editor-fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; margin: 14px 0 18px; }
.editor-fields textarea { width: 100%; resize: vertical; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .82rem; }
.editor-risk { grid-column: 1 / -1; }
@media (max-width: 760px) {
  .signal-grid, .candidate-apply-grid, .editor-fields { grid-template-columns: 1fr; }
  .editor-risk { grid-column: auto; }
  .pool-toolbar, .pool-table-section, .pool-history { padding: 15px; }
  .history-row { grid-template-columns: 1fr; }
  .progress-detail { flex-wrap: wrap; }
}
</style>
