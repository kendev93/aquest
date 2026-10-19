<script setup lang="ts">
import ResearchWorkDetails from '../components/ResearchWorkDetails.vue'
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'

import PageIntro from '../components/PageIntro.vue'
import StrategyPoolEntryTable from '../components/StrategyPoolEntryTable.vue'
import StrategyPoolFollowButton from '../components/StrategyPoolFollowButton.vue'
import StrategyPoolPostLaunchBoard from '../components/StrategyPoolPostLaunchBoard.vue'
import { fetchStoreStatus } from '../api/marketData'
import {
  addStrategyPoolFocus,
  cancelStrategyPoolJob,
  createStrategyPoolJob,
  fetchResearchSettings,
  fetchStrategyPoolEntries,
  fetchStrategyPoolFocus,
  fetchStrategyPoolJobStats,
  fetchStrategyPoolJobs,
  fetchPoolSignalDays,
  fetchStrategyPoolResearchEstimate,
  getStrategyPoolJob,
  removeStrategyPoolFocus,
  saveResearchSettings,
  type StrategyPoolBoard,
  type StrategyPoolJobRequest,
} from '../api/strategyPool'
import { describeError } from '../api/client'
// 这里的任务轮询有意不用 composables/usePolling 的 pollUntil：它内嵌了
// 「标的状态刷新」编排（refreshPoolEntries + refreshingEntries 抑制）、
// token 竞态守卫（canApply）和多种完成态的清理动作，抽象盖不住这些差异。
import type {
  DataStoreStatus,
  StrategyPoolEntry,
  StrategyPoolFocusItem,
  StrategyPoolFollowTarget,
  StrategyPoolHolding,
  StrategyPoolJobResult,
  StrategyPoolJobState,
  StrategyPoolJobStats,
  StrategyPoolJobSummary,
  StrategyPoolMetrics,
  StrategyPoolResearchEstimate,
  StrategyPoolMode,
  StrategyPoolObjective,
  StrategyPoolSignal,
  StrategyPoolSignalDay,
} from '../types'
import AppTopbar from '../components/AppTopbar.vue'
import { formatDuration, formatNumber, formatPercent, formatSignedPercent } from '../utils/format'
import {
  formatHoldingDays,
  holdingRiskSummary,
  objectiveLabel,
  sortPoolEntriesByMetric,
  sortPoolEntriesByStatus,
  symbol,
  type StrategyPoolMetricColumn,
  type StrategyPoolSortColumn,
  type StrategyPoolSortDirection,
} from '../utils/strategyPoolStatus'
import { formatChinaTimestamp } from '../utils/time'

const OBJECTIVE_STORAGE_KEY = 'aquest.strategy-pool.objective'
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
  // 风控退出行是引擎直接落库的宽松对象，这里顺手把它归一成关注开关需要的市场+代码+名称，
  // 免得模板里再对 unknown 做一遍判断。
  | {
      kind: 'risk'
      key: string
      signal: Record<string, unknown>
      target: StrategyPoolFollowTarget
    }

const objective = ref<StrategyPoolObjective | null>(null)
const entries = ref<StrategyPoolEntry[]>([])
// 特别关注名单来自后端，一只标的行；只有本页会改它，所以进页面读一次即可，
// 不进 refreshPage 的并发读取（切目标/轮询时没必要重复拉）。
const focusItems = ref<StrategyPoolFocusItem[]>([])
const focusPending = ref(false)
const history = ref<StrategyPoolJobSummary[]>([])
const historyStats = ref<StrategyPoolJobStats | null>(null)
// 显示哪一天的信号由 signalDate 决定：默认是最新行情那天（live 路径，全市场扫描结果
// 再合并之后逐股重跑的增量）；选到更早的交易日就读那一天的扫描结果，两者形状一样。
const liveSignalResult = ref<StrategyPoolJobResult | null>(null)
const pinnedSignalResult = ref<StrategyPoolJobResult | null>(null)
const signalDays = ref<StrategyPoolSignalDay[]>([])
const signalDate = ref('')
const latestSignalDay = computed(() => signalDays.value[0]?.as_of_date ?? '')
const signalIsPinned = computed(() => !!signalDate.value
  && !!latestSignalDay.value
  && signalDate.value !== latestSignalDay.value)
const signalResult = computed<StrategyPoolJobResult | null>(() => {
  if (!signalIsPinned.value) return liveSignalResult.value
  const raw = pinnedSignalResult.value
  if (!raw) return null
  return raw.results_by_objective?.[objective.value ?? 'annual_return'] ?? raw
})
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
// 点「增量更新策略池」之前的工作量预估：不点按钮也该知道这一轮要跑多少只。
const researchEstimate = ref<StrategyPoolResearchEstimate | null>(null)
const researchEstimatePending = ref(false)
// 第一次任务历史回来之前不许预估：那时还不知道有没有任务在跑，而预扫描要读整张池表。
const researchEstimateArmed = ref(false)
const market = ref<'SH' | 'SZ'>('SH')
const code = ref('')
const searchQuery = ref('')
const sortColumn = ref<StrategyPoolSortColumn>('status')
// 'status' 档就是「未点任何指标列」的默认分组，也是取消排序落回的地方，
// 因此方向只在指标列档位生效。
const sortDirection = ref<StrategyPoolSortDirection>('desc')
// 买卖信号列表的显示排序（2026-10-13 用户需求）：后端返回顺序对跟踪增量结果是按
// 证券代码的，这里允许按行内展示的全区间指标重排。'default' 表示保持返回顺序。
const SIGNAL_SORT_OPTIONS = [
  { value: 'default', label: '默认顺序' },
  { value: 'win_rate', label: '全区间胜率 高→低' },
  { value: 'payoff_ratio', label: '全区间盈亏比 高→低' },
  { value: 'total_return', label: '全区间收益 高→低' },
] as const
type SignalSortValue = (typeof SIGNAL_SORT_OPTIONS)[number]['value']
const signalSort = ref<SignalSortValue>('default')
const page = ref(1)
const pageSize = ref(50)
const elapsedClock = ref(Date.now())
let elapsedTimer: number | null = null
let pollTimer: number | null = null
let activePollToken = 0
let activeSignalTarget: { market: 'SH' | 'SZ'; code: string } | null = null
// 跑动中重取整张列表的最小间隔：见 pollJob 里那段注释。
const ENTRIES_REFRESH_MIN_MS = 20_000
let lastEntriesRefreshAt = 0
let refreshingEntriesLive = false

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
// 任务在 worker 领取之前一直是 queued：标题不能说「进行中」，否则"排队两分钟"和"真的在跑"
// 看起来一模一样（2026-10-16 一次认领响应丢失，任务在队列里空转到 120 秒被判死，页面全程
// 写着「全市场研究进行中 0%」加一排 0 计数）。
const progressHeading = computed(() => {
  const queued = activeJob.value?.status === 'queued'
  if (activeIntent.value === 'signals') return queued ? '当天信号扫描排队中' : '正在扫描当天信号'
  if (activeIntent.value === 'portfolio') return queued ? '组合回测排队中' : '组合回测任务进行中'
  if (activeIntent.value === 'single') return queued ? '单股重跑排队中' : '正在重跑单只股票'
  return queued ? '全市场研究排队中' : '全市场研究进行中'
})
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
  // 后端还没报过标的数时不给「需重算 0 只（已完成 0 · 进行中 0 …）」——那行字看着像
  // 一份算完的计划，实际是任务还在排队。
  if (typeof total !== 'number' || total <= 0) return ''
  const completed = typeof metadata.completed_symbols === 'number' ? metadata.completed_symbols : 0
  // 跳过数 = 预扫描纯复用 + 队列内补算完成，都是「复用旧结论没重新研究」的标的；
  // total_symbols 是预扫描后的真实研究队列，进度行以它为主（2026-10-13 用户决策）。
  const reused = typeof metadata.skipped_symbols === 'number' ? metadata.skipped_symbols : 0
  const failed = typeof metadata.failed_symbols === 'number' ? metadata.failed_symbols : 0
  // 历史样本不足（绝大多数是上市未满一年）每轮都会排进队列，也每轮都在几毫秒内以"跳过"
  // 结束。不摊开的话，进度条 100% 而「已完成」只有几十，看着就像任务没干活（2026-10-19
  // 用户报告）。摊开之后这一行的几项加起来就是队列长度，读者能自己对上。
  // 注意：历史样本不足的标的本来就在 skipped 里（它们也是"没重新研究"结束的），所以
  // 「已完成」照旧是 completed − skipped，这里只是把 skipped 拆成两半报出来，不能再减一次。
  const insufficient = typeof metadata.insufficient_symbols === 'number'
    ? Math.min(reused, metadata.insufficient_symbols)
    : 0
  const successful = Math.max(0, completed - reused)
  const active = typeof metadata.current_parallelism === 'number'
    ? metadata.current_parallelism
    : activeTickerProgress.value.length
  const parts = [`已完成 ${successful}`]
  if (insufficient > 0) parts.push(`历史样本不足 ${insufficient}`)
  parts.push(`进行中 ${active}`, `复用 ${reused - insufficient}`, `失败 ${failed}`)
  const line = `需重算 ${total} 只（${parts.join(' · ')}）`
  // 排期出去的标的在预扫描就不进队列了，括号里的几项加起来仍然是队列长度；单独说一句
  // 它们哪天回来，免得「需重算 0 只」看着像整池都研究完了。
  const scheduled = typeof metadata.deferred_insufficient_symbols === 'number'
    ? metadata.deferred_insufficient_symbols
    : 0
  return scheduled > 0 ? `${line} · 另排期 ${scheduled} 只` : line
})
const progressCountTitle = computed(() => {
  const metadata = activeJob.value?.metadata ?? {}
  const insufficient = typeof metadata.insufficient_symbols === 'number' ? metadata.insufficient_symbols : 0
  const scheduled = typeof metadata.deferred_insufficient_symbols === 'number'
    ? metadata.deferred_insufficient_symbols
    : 0
  const eligible = typeof metadata.next_eligible_date === 'string' ? metadata.next_eligible_date : ''
  const notes: string[] = []
  if (insufficient > 0) {
    notes.push(`本轮有 ${insufficient} 只研究完仍然历史样本不足（绝大多数是上市未满一年），不产出策略结果。`)
  }
  if (scheduled > 0) {
    notes.push(`另有 ${scheduled} 只历史样本不足的不进今天的队列：未满一年的排到「上市首日 + 365 天」，`
      + `其余每 30 天重查一次${eligible ? `，最早 ${eligible} 自动回到增量队列` : ''}，不需要手动重跑。`)
  }
  return notes.length ? notes.join('') : undefined
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

// 运行中的这轮跑得是快还是慢，只有百分比看不出来：按已完成的新研究标的数
// 外推。分母故意用「completed - 复用」，复用标的不花时间，算进去会假装很快。
//
// 两个计数器的分母不一样，混着用会把剩余算成负数：`total_symbols` 是**预扫描剔除纯复用
// 之后**的队列长度（2026-10-08 改的，增量轮里 5224 只哪怕 5100 只可复用也不再显示
// 「0 / 5224」），而 `completed_symbols` 把复用也计进去了。所以剩下的只数必须拿
// 「队列长度 − 真正研究完的 − 失败的」，不能直接减 completed。
const researchPace = computed(() => {
  if (activeIntent.value !== 'full') return null
  const job = activeJob.value
  const metadata = job?.metadata ?? {}
  const total = typeof metadata.total_symbols === 'number' ? metadata.total_symbols : 0
  const completed = typeof metadata.completed_symbols === 'number' ? metadata.completed_symbols : 0
  const reused = typeof metadata.skipped_symbols === 'number' ? metadata.skipped_symbols : 0
  const failed = typeof metadata.failed_symbols === 'number' ? metadata.failed_symbols : 0
  const insufficient = typeof metadata.insufficient_symbols === 'number' ? metadata.insufficient_symbols : 0
  const researched = Math.max(0, completed - reused)
  const started = researchStartedAt(job?.started_at)
  const base = started ?? researchStartedAt(job?.created_at)
  if (base === null || researched < 1 || total <= 0) return null
  const elapsedSeconds = Math.max(1, (elapsedClock.value - base) / 1000)
  return {
    elapsedSeconds,
    excludesQueue: started !== null,
    perSymbolSeconds: elapsedSeconds / researched,
    // 历史样本不足的那些几毫秒就结束，按"每只平均耗时"去外推会凭空多出几十分钟的假剩余。
    remainingSymbols: Math.max(0, total - researched - failed - insufficient),
  }
})

const researchPaceText = computed(() => {
  const pace = researchPace.value
  if (!pace) return ''
  const parts = [
    `已运行 ${formatDuration(pace.elapsedSeconds)}${pace.excludesQueue ? '' : '（含排队）'}`,
    `平均每只新研究标的 ${formatDuration(pace.perSymbolSeconds)}`,
  ]
  if (pace.remainingSymbols > 0) {
    parts.push(
      `剩余 ${pace.remainingSymbols} 只预计还需 ${formatDuration(pace.perSymbolSeconds * pace.remainingSymbols)}（队列里剩余的复用标的不会花这么多时间）`,
    )
  }
  return parts.join(' · ')
})

const POOL_JOB_STATUS_LABELS: Record<string, string> = {
  queued: '排队中',
  running: '运行中',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
}

function poolJobModeLabel(job: StrategyPoolJobSummary): string {
  if (job.mode === 'full') return job.rebuild ? '全市场重选（清空重建）' : '全市场重选'
  if (job.mode === 'single') return `${job.market ?? ''} ${job.code ?? ''} 单股重跑`.trim()
  if (job.mode === 'signals') return '当天信号扫描'
  return '组合回测'
}

// 终态任务用后端算好的时长；没跑完的行按页面自己的秒表走，不然列表里正在
// 研究的那条永远停在「0 分钟」，看不出它到底是卡住了还是刚起步。
function poolJobDurationText(job: StrategyPoolJobSummary): string {
  if (!['queued', 'running'].includes(job.status)) {
    if (typeof job.duration_seconds !== 'number') return ''
    return `耗时 ${formatDuration(job.duration_seconds)}${job.duration_includes_queue ? '（含排队）' : ''}`
  }
  const started = researchStartedAt(job.started_at)
  const base = started ?? researchStartedAt(job.created_at)
  if (base === null) return ''
  const seconds = Math.max(0, (elapsedClock.value - base) / 1000)
  if (job.status === 'queued') return `已等待 ${formatDuration(seconds)}`
  return `已运行 ${formatDuration(seconds)}${started === null ? '（含排队）' : ''}`
}

function poolJobDetailText(job: StrategyPoolJobSummary): string {
  const parts = [POOL_JOB_STATUS_LABELS[job.status] ?? job.status]
  const duration = poolJobDurationText(job)
  if (duration) parts.push(duration)
  const total = job.total_symbols ?? 0
  if (total > 0) {
    parts.push(
      `已完成 ${job.completed_symbols ?? 0}/${total}（新研究 ${job.newly_researched_symbols ?? 0} · 复用 ${job.skipped_symbols ?? 0}）`,
    )
    if (job.failed_symbols) parts.push(`失败 ${job.failed_symbols}`)
  }
  if (job.maximum_parallelism) parts.push(`并行 ${job.maximum_parallelism}`)
  if (job.mode === 'full' || job.mode === 'signals') parts.push(job.automatic ? '自动触发' : '手动触发')
  if (job.dual_objectives) parts.push('两套目标')
  // 正在被本页轮询的那条已经有上方进度面板显示阶段，这里只给其它运行中的任务补。
  if (job.status === 'running' && job.job_id !== activeJobId.value && job.phase_label) {
    parts.push(job.phase_label)
  }
  if (job.error) parts.push(`原因：${job.error}`)
  return parts.join(' · ')
}

// 后台跟踪每十分钟写一条信号扫描记录，纯按时间倒序取十条就全是同一句话的扫描，
// 跑了几小时的全市场研究一条都看不见。分两组各留几条，时间顺序在组内保持。
const historyGroups = computed(() => {
  const groups = [
    {
      label: '研究与重跑任务（全市场重选 · 单股重跑 · 组合回测）',
      jobs: history.value.filter((job) => job.mode !== 'signals').slice(0, 4),
    },
    {
      label: '当天信号扫描（后台跟踪每十分钟一条）',
      jobs: history.value.filter((job) => job.mode === 'signals').slice(0, 5),
    },
  ]
  return groups.filter((group) => group.jobs.length > 0)
})

const historyStatsText = computed(() => {
  const modes = historyStats.value?.modes
  if (!modes) return ''
  const limit = typeof historyStats.value?.sample_limit === 'number' ? historyStats.value.sample_limit : 0
  const lines: string[] = []
  const full = modes.full
  if (full && full.samples > 0) {
    lines.push(
      `全市场重选近 ${full.samples} 次完成耗时：中位 ${formatDuration(full.median_duration_seconds)} · 平均 ${formatDuration(full.avg_duration_seconds)}`,
    )
  }
  if (full && full.samples === 0 && full.throughput_samples > 0 && limit > 0) {
    // 没有一次正常完成的全市场重选时，这一行会整条消失；不点破就被读成「统计坏了」。
    lines.push(`全市场重选最近 ${limit} 次终态里没有一次正常完成（都是失败或取消），给不出平均完成耗时`)
  }
  if (full && full.throughput_samples > 0) {
    lines.push(
      `每只新研究标的中位 ${formatDuration(full.median_seconds_per_symbol)}（${full.throughput_samples} 轮样本，平均并行 ${full.avg_parallelism?.toFixed(1) ?? '—'}）`,
    )
  }
  const signals = modes.signals
  if (signals && signals.samples > 0) {
    lines.push(`信号扫描近 ${signals.samples} 次中位 ${formatDuration(signals.median_duration_seconds)}`)
  }
  const single = modes.single
  if (single && single.samples > 0) {
    lines.push(`单股重跑近 ${single.samples} 次中位 ${formatDuration(single.median_duration_seconds)}`)
  }
  return lines.join('；')
})

const historyStatsNote = computed(() => {
  const stats = historyStats.value
  if (!stats || typeof stats.sample_limit !== 'number') return ''
  const full = stats.modes?.full
  const missingStart = full && full.samples > 0 ? full.samples - full.started_at_samples : 0
  return `统计口径：每种任务各取最近 ${stats.sample_limit} 次终态记录；被取消的轮次只计入「每只新研究标的」耗时，不计入完成耗时；失败任务两项都不计。${missingStart > 0 ? `其中 ${missingStart} 次全市场重选没有记录开始时间，时长含排队。` : ''}`
})
const buySignals = computed(() => signalResult.value?.buy_signals ?? [])
const ruleSellSignals = computed(() => signalResult.value?.sell_signals ?? [])
const riskSellSignals = computed(() => signalResult.value?.model_portfolio?.risk_exit_signals ?? [])
const sellNextTradingDaySymbols = computed(() => new Set([
  ...ruleSellSignals.value.map((item) => item.symbol),
]))
const currentHoldings = computed<StrategyPoolHolding[]>(
  () => signalResult.value?.holding_scope === 'per_stock_strategy'
    && signalResult.value.model_portfolio?.available !== false
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
    target: {
      market: signal.market === 'SZ' ? 'SZ' as const : 'SH' as const,
      code: String(signal.code ?? ''),
      name: typeof signal.name === 'string' ? signal.name : '',
    },
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
const sortedBuySignals = computed(() => sortSignalRows(buySignals.value, (item) => item.symbol))
const sortedSellPanelItems = computed(() => sortSignalRows(sellPanelItems.value, sellItemSymbol))
const visibleBuySignals = computed(() => pageItems(sortedBuySignals.value, buyPage.value))
const visibleSellSignals = computed(() => pageItems(sortedSellPanelItems.value, sellPage.value))
const visibleCurrentHoldings = computed(() => pageItems(currentHoldings.value, holdingsPage.value))
const buyPageCount = computed(() => pageCount(buySignals.value.length))
const sellPageCount = computed(() => pageCount(sellPanelItems.value.length))
const holdingsPageCount = computed(() => pageCount(currentHoldings.value.length))

watch([buySignals, sellPanelItems, currentHoldings], () => {
  buyPage.value = 1
  sellPage.value = 1
  holdingsPage.value = 1
})
watch(signalSort, () => {
  buyPage.value = 1
  sellPage.value = 1
})

const filteredEntries = computed(() => {
  const query = searchQuery.value.trim().toLocaleLowerCase()
  if (!query) return entries.value
  return entries.value.filter((entry) =>
    `${entry.market}${entry.code} ${entry.code} ${entry.name ?? ''}`.toLocaleLowerCase().includes(query),
  )
})

const signalStates = computed(() => signalResult.value?.states ?? [])

const sortedEntries = computed(() => {
  const metric = sortColumn.value
  if (metric === 'status') {
    return sortPoolEntriesByStatus(filteredEntries.value, signalStates.value, heldSymbols.value, objective.value)
  }
  return sortPoolEntriesByMetric(
    filteredEntries.value,
    metric,
    sortDirection.value,
    signalStates.value,
    heldSymbols.value,
    objective.value,
  )
})
const totalPages = computed(() => Math.max(1, Math.ceil(sortedEntries.value.length / pageSize.value)))
const pageEntries = computed(() => {
  const start = (page.value - 1) * pageSize.value
  return sortedEntries.value.slice(start, start + pageSize.value)
})
const pageStart = computed(() => sortedEntries.value.length ? (page.value - 1) * pageSize.value + 1 : 0)
const pageEnd = computed(() => Math.min(page.value * pageSize.value, sortedEntries.value.length))

// 特别关注面板只按状态优先级排，不跟主表的搜索/排序/分页联动；行数据仍来自 entries，
// 所以暂时不在策略池里的关注标的不会占行，只在面板上提示数量。
const followKeys = computed(() => new Set(focusItems.value.map(symbol)))
const focusRows = computed(() => sortPoolEntriesByStatus(
  entries.value.filter((entry) => followKeys.value.has(symbol(entry))),
  signalStates.value,
  heldSymbols.value,
  objective.value,
))
const focusMissing = computed(() => {
  const poolKeys = new Set(entries.value.map(symbol))
  return focusItems.value.filter((item) => !poolKeys.has(symbol(item)))
})
const focusMissingCount = computed(() => focusMissing.value.length)
const listContext = computed(() => new URLSearchParams({
  objective: objective.value ?? 'annual_return',
  search: searchQuery.value,
  sort: sortColumn.value,
  dir: sortDirection.value,
  page: String(page.value),
  pageSize: String(pageSize.value),
}).toString())

async function toggleFollow(target: StrategyPoolFollowTarget): Promise<void> {
  if (focusPending.value) return
  const key = symbol(target)
  const name = target.name ?? ''
  const followed = followKeys.value.has(key)
  const previous = focusItems.value
  focusPending.value = true
  // 先按本地更新让面板跟手，再用服务端返回的权威名单覆盖；被拒（例如超过上限）就整表回滚。
  focusItems.value = followed
    ? previous.filter((item) => symbol(item) !== key)
    : [...previous, { market: target.market, code: target.code, name }]
  errorMessage.value = ''
  try {
    focusItems.value = followed
      ? await removeStrategyPoolFocus(target.market, target.code)
      : await addStrategyPoolFocus(target.market, target.code, name)
  } catch (error) {
    focusItems.value = previous
    errorMessage.value = apiMessage(error)
  } finally {
    focusPending.value = false
  }
}

// 池外的关注标的在两张表里都没有行，也就没有单只取消的入口；这里给一个批量移出的出口，
// 逐只走同一个 DELETE 接口，避免为了「清理」再加一套后端语义。
async function pruneMissingFocus(): Promise<void> {
  if (focusPending.value) return
  const missing = focusMissing.value
  if (!missing.length) return
  const previous = focusItems.value
  focusPending.value = true
  errorMessage.value = ''
  try {
    let items = previous
    for (const item of missing) {
      items = await removeStrategyPoolFocus(item.market, item.code)
    }
    focusItems.value = items
  } catch (error) {
    // 中途失败时后端可能已经移除了几只，重读一次让界面和库里一致。
    focusItems.value = await fetchStrategyPoolFocus().catch(() => previous)
    errorMessage.value = apiMessage(error)
  } finally {
    focusPending.value = false
  }
}

async function loadFocus(): Promise<void> {
  try {
    focusItems.value = await fetchStrategyPoolFocus()
  } catch {
    // 读不到关注名单就当作一只都没关注，不打断页面初始化。
  }
}

function selectObjective(value: StrategyPoolObjective): void {
  objective.value = value
  window.localStorage.setItem(OBJECTIVE_STORAGE_KEY, value)
  errorMessage.value = ''
  pollWarning.value = ''
  successMessage.value = ''
  liveSignalResult.value = null
  pinnedSignalResult.value = null
  signalDate.value = latestSignalDay.value
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
    void loadHistoryStats()
    await Promise.all([restoreLatestSignalResult(nextHistory), loadSignalDays()])
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
      liveSignalResult.value = null
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
  liveSignalResult.value = restored
  if (!signalDate.value) signalDate.value = restored.as_of_date ?? latestSignalDay.value
}

// 可回看的交易日：后端每个交易日只留最后一次全市场扫描，最多 10 天。读不到就当没有
// 历史可看，页面照常显示最新那天，不打断初始化。
async function loadSignalDays(): Promise<void> {
  const before = signalDate.value
  try {
    signalDays.value = (await fetchPoolSignalDays()) ?? []
  } catch {
    signalDays.value = []
    return
  }
  if (!before || before === latestSignalDay.value) signalDate.value = latestSignalDay.value
}

async function selectSignalDay(value: string): Promise<void> {
  if (value === latestSignalDay.value) {
    signalDate.value = value
    pinnedSignalResult.value = null
    await restoreLatestSignalResult(history.value)
    return
  }
  const day = signalDays.value.find((item) => item.as_of_date === value)
  if (!day) return
  signalDate.value = value
  try {
    const state = await getStrategyPoolJob(day.job_id)
    pinnedSignalResult.value = state.result ?? null
    if (!state.result) {
      errorMessage.value = `${value} 那天已经没有可看的信号结果了。`
    }
  } catch (error) {
    pinnedSignalResult.value = null
    errorMessage.value = apiMessage(error)
  }
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

/**
 * 全市场任务的参数。预估和点击共用这一个构造器：分两处写就会漂移，
 * 而页面上那个数字正是用户决定「现在就点」还是「等会儿再点」的依据。
 */
function fullJobPayload(rebuild: boolean): StrategyPoolJobRequest {
  return {
    mode: 'full',
    rebuild,
    risk_trials: riskTrials.value,
    max_hold_days: maxHoldDays.value || null,
    boards: [...researchBoards.value],
    exclude_st: excludeSt.value,
  }
}

async function refreshResearchEstimate(): Promise<void> {
  // 有任务在跑时队列已经在动了，进度条上有实时数；这时预估反而会说谎。
  if (isBusy.value) return
  researchEstimatePending.value = true
  try {
    researchEstimate.value = await fetchStrategyPoolResearchEstimate(fullJobPayload(false))
  } catch {
    // 预估失败就留白，不拿上一次的数字冒充现在的状态。
    researchEstimate.value = null
  } finally {
    researchEstimatePending.value = false
  }
}

const researchEstimateText = computed(() => {
  if (researchEstimatePending.value) return '统计中…'
  const estimate = researchEstimate.value
  if (!estimate) return ''
  if (!estimate.universe_symbols) return '当前研究范围里没有可研究的标的'
  // 排期只报最早的那一天：各标的回来的日子不同，但用户只需要知道哪天之前点了也一样。
  const scheduled = estimate.deferred_symbols ?? 0
  const schedule = scheduled > 0
    ? ` · ${scheduled} 只样本不足已排期（最早 ${estimate.next_eligible_date ?? '待定'}）`
    : ''
  if (estimate.pending_symbols === 0) {
    return `没有待更新：${estimate.reusable_symbols} 只可直接复用${schedule}`
  }
  return `待重算 ${estimate.pending_symbols} 只 · 可复用 ${estimate.reusable_symbols} 只${schedule}`
})
const researchEstimateTitle = computed(() => {
  const estimate = researchEstimate.value
  if (!estimate) return '按当前研究范围与参数预估下一次「增量更新策略池」的工作量'
  const scheduled = estimate.deferred_symbols ?? 0
  const schedule = scheduled > 0
    ? `另有 ${scheduled} 只历史样本不足（绝大多数是上市未满一年）不进队列：未满一年的排到「上市首日 + 365 天」、`
      + `其余每 30 天重查一次，最早 ${estimate.next_eligible_date ?? '待定'} 自动回来，不需要手动重跑。`
    : ''
  return '按当前研究范围与参数，下次「增量更新策略池」真正要逐只研究的标的数：'
    + '研究口径指纹没变、结果没欠账的直接复用，弱策略到期与上线后衰减的仍会重跑。'
    + schedule
    + `标的范围 ${estimate.universe_symbols} 只，数据截至 ${estimate.data_end ?? '未知'}。`
})

async function startFullResearch(rebuild = true): Promise<void> {
  if (isBusy.value) return
  // 这个入口会先删空整张策略池再从头逐只重跑，误点一次的代价是几个小时，所以先确认。
  // 增量更新不删任何东西，不拦。
  if (rebuild && !window.confirm(
    '清空并重建全市场：会删除当前的自动策略、手动覆盖和失败记录（行情、任务历史与特别关注名单都保留），'
    + '然后从第一只股票重新逐只研究，跑完一轮要几个小时。确定继续？',
  )) {
    return
  }
  await startTask(fullJobPayload(rebuild), 'full')
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
      liveSignalResult.value = null
      pinnedSignalResult.value = null
      signalDays.value = []
      signalDate.value = ''
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
        // 一次重取是整张表（2026-10-19 实测 4439 行 / 7 MB），4 路并行几乎每几秒就有
        // 一只完成，逐次刷新会让浏览器一直在解析与重排。跑动中最少隔
        // ENTRIES_REFRESH_MIN_MS 再刷；没刷时保留旧的完成数，下一轮照样会补上。
        // 任务到终态、失败或被取消那些分支不受这条限制，仍然立刻刷一次。
        if (
          finishedCount !== refreshedFinishedCount
          && !refreshingEntriesLive
          && Date.now() - lastEntriesRefreshAt >= ENTRIES_REFRESH_MIN_MS
        ) {
          refreshingEntries = true
          // 一次重取要 1.4–3.3 秒，比 1.5 秒的轮询间隔还长，所以「正在刷」和「上次刷成
          // 功是几时」要分开记：前者挡住重叠发请求，后者只在成功时消耗这个窗口——失败
          // 不该赔进去，下一轮就得立刻重试。
          refreshingEntriesLive = true
          let refreshed: boolean
          try {
            refreshed = await refreshPoolEntries(canApply)
          } finally {
            refreshingEntriesLive = false
          }
          if (refreshed) {
            refreshedFinishedCount = finishedCount
            lastEntriesRefreshAt = Date.now()
          }
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
        const previous = liveSignalResult.value
        liveSignalResult.value = requestedTarget && previous?.objective === result.objective
          ? mergeSignalResult(previous, result, requestedTarget)
          : result
        page.value = 1
        // 新的一天要出现在日期下拉里；用户钉在更早的交易日时不抢他的显示。
        await loadSignalDays()
        if (!signalIsPinned.value) signalDate.value = latestSignalDay.value
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
  await loadHistoryStats()
}

// 统计读不到不影响任务列表本身（例如后端还没有这个路由），单独吞掉错误。
async function loadHistoryStats(): Promise<void> {
  try {
    historyStats.value = await fetchStrategyPoolJobStats()
  } catch {
    historyStats.value = null
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

// 列头三态循环：高→低 → 低→高 → 取消排序（回到默认的按状态分组）。
// 点别的列直接从高→低开始，不让上一列的方向状态串到新的一列。
function setSort(column: StrategyPoolMetricColumn): void {
  if (sortColumn.value !== column || sortDirection.value === 'asc') {
    sortColumn.value = sortColumn.value === column ? 'status' : column
    sortDirection.value = 'desc'
  } else {
    sortDirection.value = 'asc'
  }
  page.value = 1
}

function changePageSize(value: string): void {
  pageSize.value = Number(value) || 50
  page.value = 1
}

function pageCount(count: number): number {
  return Math.max(1, Math.ceil(count / SIGNAL_PAGE_SIZE))
}

function pageItems<T>(items: T[], page: number): T[] {
  const start = (page - 1) * SIGNAL_PAGE_SIZE
  return items.slice(start, start + SIGNAL_PAGE_SIZE)
}

function holdingStatus(holding: StrategyPoolHolding): string {
  // 风控挂单是昨收判定、今开成交，还没成交前它仍是持仓，标签要说清是哪条线触发的。
  const exitLabel = holding.pending_exit_reason
    ? `今日触发${holding.pending_exit_reason} · 下一交易日开盘卖出`
    : sellNextTradingDaySymbols.value.has(`${holding.market}${holding.code}`)
      ? '今日触发卖出 · 下一交易日卖出'
      : '当前持仓中'
  const holdingReturn = typeof holding.holding_return_pct === 'number'
    && Number.isFinite(holding.holding_return_pct)
    ? `持仓阶段收益 ${formatSignedPercent(holding.holding_return_pct)}`
    : ''
  return holdingReturn ? `${holdingReturn} · ${exitLabel}` : exitLabel
}

function incompleteHoldingsWarning(): string {
  const result = signalResult.value
  const portfolio = result?.model_portfolio
  const reason = portfolio?.reason ? ` ${portfolio.reason}` : ''
  if (typeof portfolio?.failed_symbols === 'number' && portfolio.failed_symbols > 0) {
    return `逐股持仓回放有 ${portfolio.failed_symbols} 个标的未能确认，结果不完整；当前仅展示已确认的持仓。${reason}`
  }
  if (typeof portfolio?.failed_batches === 'number' && portfolio.failed_batches > 0) {
    return `逐股持仓回放有 ${portfolio.failed_batches} 个批次失败，结果不完整；当前仅展示已确认的持仓。${reason}`
  }
  if (result?.signal_target) {
    return '这是单股增量结果，尚未覆盖全市场；当前仅展示已确认的持仓，请刷新当天信号查看完整持仓。'
  }
  return '结果不完整，当前仅展示已确认的持仓；请刷新当天信号查看完整持仓。'
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
    const candidate = entry.active_candidate
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

function sellItemSymbol(item: SellPanelItem): string {
  return item.kind === 'rule'
    ? item.signal.symbol
    : `${String(item.signal.market ?? '')}${String(item.signal.code ?? '')}`
}

// 排序取全区间指标：与行内「全区间胜率 / 盈亏比」文案同一口径，且跟踪增量返回的
// 信号行本身不带指标，只有条目表补得上。缺全区间指标的行排在末尾。
function sortSignalRows<T>(rows: T[], keyOf: (row: T) => string): T[] {
  if (signalSort.value === 'default') return rows
  const metric = signalSort.value
  const score = (row: T): number => {
    const value = entryQualityMap.value.get(keyOf(row))?.[metric]
    return typeof value === 'number' && Number.isFinite(value) ? value : Number.NEGATIVE_INFINITY
  }
  return [...rows].sort((left, right) => (
    score(right) - score(left) || keyOf(left).localeCompare(keyOf(right))
  ))
}

function riskExitReturnText(signal: Record<string, unknown>): string {
  const value = signal.trade_return_pct
  return typeof value === 'number' && Number.isFinite(value)
    ? `本次持仓收益 ${formatSignedPercent(value)}`
    : ''
}

/** 风控退出行的出处：哪一天的收盘碰到哪条线、哪一天开盘成交，两个日期都写出来。 */
function riskExitFilledText(signal: Record<string, unknown>): string {
  const reason = (typeof signal.reason === 'string' && signal.reason)
    || (typeof signal.risk_trigger === 'string' && signal.risk_trigger)
    || '风控'
  const trigger = typeof signal.signal_date === 'string' && signal.signal_date
    ? `${signal.signal_date} 收盘`
    : '前一交易日收盘'
  const filled = typeof signal.date === 'string' && signal.date ? `${signal.date} 开盘` : '今日开盘'
  return `${trigger}触发${reason} · ${filled}已成交`
}

// created_at 是裸 UTC（后端 `_now()` 落库前把 tzinfo 去掉了），必须显式按东八区展示。
// 以前直接 `new Date(串).toLocaleString('zh-CN')` 会按浏览器时区解释，同一个
// job.created_at 在本页和别的页面能差出 8 小时。
function formatTimestamp(value: unknown): string {
  if (typeof value === 'number') {
    return Number.isFinite(value)
      ? formatChinaTimestamp(new Date(value * 1000).toISOString())
      : '—'
  }
  return formatChinaTimestamp(value)
}

function apiMessage(error: unknown): string {
  return describeError(error, { fallback: '全市场策略池请求失败，请重试。', localMessage: 'raw' })
}

async function initialize(): Promise<void> {
  void loadResearchSettings()
  void loadFocus()
  const params = new URLSearchParams(window.location.search)
  searchQuery.value = params.get('search') ?? ''
  page.value = Math.max(1, Number(params.get('page') ?? 1))
  pageSize.value = Number(params.get('pageSize') ?? 50)
  const sort = params.get('sort')
  if (['status','strategy_return','annual_return','sharpe','win_rate','payoff_ratio','max_drawdown','avg_holding_days','benchmark_return','created_return'].includes(sort ?? '')) sortColumn.value = sort as typeof sortColumn.value
  // 方向只认 asc，其余一律回到高→低；sort 已经是 status（取消排序）时方向不参与排序。
  sortDirection.value = params.get('dir') === 'asc' ? 'asc' : 'desc'
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
  // 到这一步第一次任务历史已经回来，才知道有没有任务在跑；研究正忙的时候白跑一次
  // 预扫描（要读整张池表）等于给正在被锁饿着的库再加一份读。
  researchEstimateArmed.value = true
  void refreshResearchEstimate()
}

onMounted(() => void initialize())
watch(
  [riskTrials, maxHoldDays, researchBoards, excludeSt, isBusy],
  () => {
    if (researchEstimateArmed.value) void refreshResearchEstimate()
  },
)
onUnmounted(() => {
  activePollToken += 1
  if (pollTimer !== null) window.clearTimeout(pollTimer)
  if (elapsedTimer !== null) window.clearInterval(elapsedTimer)
})
</script>

<template>
  <div class="screen-page strategy-pool-page" data-testid="strategy-pool-page">
    <AppTopbar />

    <main class="content-shell">
      <PageIntro
        eyebrow="MARKET / STRATEGY POOL"
        title="全市场策略池"
        copy="每只股票分别维护年化收益与夏普最高的策略，查看当天信号和逐股策略的分段表现。"
        guide-key="strategy-pool"
      >
        <template v-if="objective" #default>
          <span class="note-label">当前决策目标</span>
          <strong>{{ objectiveLabel(objective) }}</strong>
          <small>此目标决定每只股票采用哪套策略，也决定 5 个持仓槽的买入优先级。</small>
        </template>
      </PageIntro>

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
            <small v-if="researchEstimateText" class="research-estimate" data-testid="pool-research-estimate" :title="researchEstimateTitle">{{ researchEstimateText }}</small>
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
          <p class="empty-copy">清空重建会删除当前自动策略、手动覆盖和失败记录，从头逐只研究，点击后会先让你确认一次；增量更新保留已有策略并复用未变化的结果。</p>

          <div v-if="isBusy" class="pool-progress" data-testid="pool-job-progress">
            <div class="progress-heading" data-testid="pool-progress-heading"><strong>{{ progressHeading }}</strong><strong>{{ progressPercent }}%</strong></div>
            <div class="progress-track"><span :style="{ width: `${progressPercent}%` }"></span></div>
            <p v-if="progressCountText" class="progress-count" data-testid="pool-progress-count" :title="progressCountTitle">{{ progressCountText }}</p>
            <p v-if="researchPaceText" class="progress-count" data-testid="pool-research-pace">{{ researchPaceText }}</p>
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

        <div class="signal-controls-row" data-testid="pool-signal-controls">
          <label class="signal-sort">买卖信号排序
            <select v-model="signalSort" data-testid="pool-signal-sort">
              <option v-for="option in SIGNAL_SORT_OPTIONS" :key="option.value" :value="option.value">{{ option.label }}</option>
            </select>
          </label>
          <label class="signal-sort">信号日期
            <select
              :value="signalDate"
              data-testid="pool-signal-date"
              :disabled="!signalDays.length"
              @change="selectSignalDay(($event.target as HTMLSelectElement).value)"
            >
              <option v-for="day in signalDays" :key="day.as_of_date" :value="day.as_of_date">
                {{ day.as_of_date }}{{ day.as_of_date === latestSignalDay ? '（最新）' : '' }}
              </option>
            </select>
          </label>
          <small>按全区间指标排序只改显示顺序；没有全区间指标的标的排在末尾。</small>
          <small v-if="signalIsPinned" data-testid="pool-signal-day-note">{{ signalDate }} 那天最后一次全市场扫描的结果；之后的逐股重跑不进历史视图</small>
          <small v-else-if="signalDays.length > 1" data-testid="pool-signal-day-note">可回看最近 {{ signalDays.length }} 个交易日的扫描结果，默认显示最新行情那天</small>
        </div>

        <section class="signal-grid">
          <article class="signal-panel panel-card" data-testid="pool-buy-signals">
            <div class="section-heading"><div><p class="eyebrow">BUY SIGNALS</p><h2>触发买入 <span>{{ buySignals.length }}</span></h2></div><small>{{ signalAsOf || '信号扫描后显示日期' }}</small></div>
            <div v-if="buySignals.length" class="signal-list">
              <div v-for="item in visibleBuySignals" :key="item.symbol" class="signal-row">
                <strong>{{ item.symbol }}<template v-if="item.name"> · {{ item.name }}</template></strong>
                <span>买入信号触发 · 下一交易日执行<template v-if="holdingReturnText(item)"> · {{ holdingReturnText(item) }}</template><template v-if="signalMetricsSummary(item)"> · {{ signalMetricsSummary(item) }}</template><template v-if="qualityText(item.symbol)"> · {{ qualityText(item.symbol) }}</template></span>
                <StrategyPoolFollowButton
                  :target="item"
                  :follow-keys="followKeys"
                  :busy="focusPending"
                  :test-id="`pool-buy-follow-${item.code}`"
                  @toggle="toggleFollow(item)"
                />
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
            <div class="section-heading"><div><p class="eyebrow">SELL SIGNALS</p><h2>卖出规则与风控退出 <span>{{ ruleSellSignals.length + riskSellSignals.length }}</span></h2></div><small>{{ signalAsOf ? `${signalAsOf} 收盘判定卖出规则 · 风控退出在下一交易日开盘成交` : '卖出条件与持仓风控退出' }}</small></div>
            <div v-if="ruleSellSignals.length || riskSellSignals.length" class="signal-list">
              <template v-for="item in visibleSellSignals" :key="item.key">
                <div v-if="item.kind === 'rule'" class="signal-row">
                  <strong>{{ item.signal.symbol }}<template v-if="item.signal.name"> · {{ item.signal.name }}</template></strong>
                  <span>卖出规则触发 · 下一交易日执行<template v-if="holdingReturnText(item.signal)"> · {{ holdingReturnText(item.signal) }}</template><template v-if="signalMetricsSummary(item.signal)"> · {{ signalMetricsSummary(item.signal) }}</template><template v-if="qualityText(item.signal.symbol)"> · {{ qualityText(item.signal.symbol) }}</template></span>
                  <StrategyPoolFollowButton
                    :target="item.signal"
                    :follow-keys="followKeys"
                    :busy="focusPending"
                    :test-id="`pool-sell-rule-follow-${item.signal.code}`"
                    @toggle="toggleFollow(item.signal)"
                  />
                </div>
                <div v-else class="signal-row risk-signal">
                  <strong>{{ item.signal.market }}{{ item.signal.code }}<template v-if="item.signal.name"> · {{ item.signal.name }}</template></strong>
                  <span>{{ riskExitFilledText(item.signal) }}<template v-if="riskExitReturnText(item.signal)"> · {{ riskExitReturnText(item.signal) }}</template></span>
                  <StrategyPoolFollowButton
                    :target="item.target"
                    :follow-keys="followKeys"
                    :busy="focusPending"
                    :test-id="`pool-sell-risk-follow-${item.target.code}`"
                    @toggle="toggleFollow(item.target)"
                  />
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
            <div class="section-heading"><div><p class="eyebrow">CURRENT HOLDINGS</p><h2>持仓中 <span>{{ currentHoldings.length }}</span></h2></div><small>{{ objectiveLabel(objective) }}目标逐股策略 · {{ signalAsOf || '信号扫描后显示日期' }}</small></div>
            <div v-if="currentHoldings.length" class="signal-list">
              <div v-for="holding in visibleCurrentHoldings" :key="`${holding.market}${holding.code}`" class="signal-row holding-row">
                <strong>{{ holding.market }}{{ holding.code }} · {{ holding.name || '—' }}</strong>
                <span>{{ holdingStatus(holding) }}<template v-if="qualityText(symbol(holding))"> · {{ qualityText(symbol(holding)) }}</template></span>
                <small v-if="holdingRiskSummary(holding)" :data-testid="`pool-holding-risk-${holding.code}`">{{ holdingRiskSummary(holding) }}</small>
                <StrategyPoolFollowButton
                  :target="holding"
                  :follow-keys="followKeys"
                  :busy="focusPending"
                  :test-id="`pool-holding-follow-${holding.code}`"
                  @toggle="toggleFollow(holding)"
                />
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
            <p v-else-if="signalResult.holding_snapshot_complete !== true" class="empty-copy">{{ incompleteHoldingsWarning() }}</p>
            <p v-else-if="!currentHoldings.length" class="empty-copy">当前没有仍处于持仓状态的标的。</p>
          </article>
        </section>
        <p class="pool-scale-note">买入和卖出规则都在最新完整行情日收盘后判定，下一交易日执行，因此今日触发卖出规则的标的当天仍列在持仓中。止损、止盈、移动止盈走同一条口径：触发当日收盘后判定、下一交易日按开盘价成交；持仓行上的三档风控价是引擎实际在用的那三条线，按标注那一日收盘后的数据算出。「最新收盘」是这一行标的最后一根已完成日线的收盘价（不复权），直接从行情取、不走跟踪摘要，所以空仓、当天没有买卖信号、甚至还没有跟踪版本的行都有。它和那三条线不一定同一天：风控线跟着跟踪快照的判定日期，行情却可能已经多走了几个交易日（停牌或那一轮跟踪没跑成），这时价下面会标出「价截至 X」，比对之前先看清各自是哪一天。</p>

        <section class="pool-focus-section panel-card" data-testid="pool-focus-section">
          <div class="section-heading"><div><p class="eyebrow">FOLLOWED</p><h2>特别关注 <span>{{ focusRows.length }}</span></h2></div><small>只列关注的标的 · 按状态优先级排序，不跟随下方搜索、排序和分页</small></div>
          <StrategyPoolEntryTable
            v-if="focusRows.length"
            :entries="focusRows"
            :objective="objective"
            :states="signalStates"
            :held-keys="heldSymbols"
            :follow-keys="followKeys"
            testid="focus"
            :sortable="false"
            :follow-busy="focusPending"
            :queued-symbol="queuedSingleSymbol"
            :busy="isBusy"
            :busy-intent="activeIntent"
            :list-context="listContext"
            @rerun="rerunEntry"
            @toggle-follow="toggleFollow"
          />
          <p v-else class="empty-copy" data-testid="pool-focus-empty">{{ focusMissingCount ? '已关注的标的目前都不在策略池里；跑完全市场研究或单股重跑后会显示。' : '在下方「全市场标的状态」的维护列点击关注，这里就只显示这几只标的的当前状态。' }}</p>
          <p v-if="focusMissingCount > 0" class="pool-scale-note" data-testid="pool-focus-missing">另有 {{ focusMissingCount }} 只关注标的暂时不在策略池，重跑后会显示；不需要了可以<button class="text-button" data-testid="pool-focus-prune" :disabled="focusPending" @click="void pruneMissingFocus()">移出这些关注</button>。</p>
        </section>

        <StrategyPoolPostLaunchBoard
          :entries="entries"
          :objective="objective"
          :loading="loading"
          :list-context="listContext"
        />

        <section class="pool-table-section panel-card" data-testid="pool-entry-table-section">
          <div class="section-heading"><div><p class="eyebrow">DAILY STATUS</p><h2>全市场标的状态 <span>{{ entries.length.toLocaleString() }}</span></h2></div><span>{{ loading ? '正在读取策略池…' : `按${objectiveLabel(objective)}策略决策` }}</span></div>
          <p class="pool-scale-note">收益、年化、夏普、胜率、盈亏比、最大回撤均为该策略在完整历史区间实际回放（全区间）的指标；平均持仓天数为全区间已平仓交易按股数加权的平均持有天数，用于估算买入后大致需要持有多久。旧候选会在下次全市场重选时补算。策略的选择与排序目标仍按验证段年化/夏普决策，避免用未来数据选策略。全段基准显示个股买入持有涨跌；验证和留出分别用同区间基准计算总收益超额。</p>
          <p class="pool-scale-note">列头排序三态循环：点一次按这一列从高到低，再点从低到高，第三次取消排序、回到默认的按状态分组；点别的列直接从高到低开始。排序只改显示顺序，取不到这一列指标的标的（无可用策略、创建至今待初始化或更新失败）无论升降都排在末尾。</p>
          <div class="pool-search-row">
            <label class="pool-search">搜索标的
              <input v-model="searchQuery" type="search" placeholder="输入证券代码或中文名称" data-testid="pool-entry-search" @input="page = 1">
            </label>
            <small>匹配 {{ filteredEntries.length.toLocaleString() }} / {{ entries.length.toLocaleString() }} 只</small>
          </div>
          <StrategyPoolEntryTable
            :entries="pageEntries"
            :objective="objective"
            :states="signalStates"
            :held-keys="heldSymbols"
            :follow-keys="followKeys"
            testid="pool"
            :sort-column="sortColumn"
            :sort-direction="sortDirection"
            :follow-busy="focusPending"
            :queued-symbol="queuedSingleSymbol"
            :busy="isBusy"
            :busy-intent="activeIntent"
            :list-context="listContext"
            :empty-text="searchQuery.trim() && entries.length ? '没有符合代码或名称的标的。' : loading ? '读取中…' : '策略池为空，开始全市场研究后会逐只写入候选。'"
            @sort="setSort"
            @rerun="rerunEntry"
            @toggle-follow="toggleFollow"
          />
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
          <p v-if="historyStatsText" class="progress-count" data-testid="pool-history-stats">{{ historyStatsText }}</p>
          <p v-if="historyStatsNote" class="pool-scale-note" data-testid="pool-history-stats-note">{{ historyStatsNote }}</p>
          <div v-if="historyGroups.length" class="history-groups">
            <div v-for="group in historyGroups" :key="group.label" class="history-group">
              <p class="history-group-label">{{ group.label }}</p>
              <div v-for="job in group.jobs" :key="job.job_id" class="history-row" :data-testid="`pool-history-${job.job_id}`"><strong>{{ poolJobModeLabel(job) }}</strong><span>{{ poolJobDetailText(job) }}</span><small>{{ formatTimestamp(job.created_at) }}</small></div>
            </div>
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
.panel-card { border: 1px solid #25364f; background: rgba(12, 20, 36, .78); padding: 22px; }
.pool-choice { max-width: 900px; margin: 24px auto; text-align: center; }
.pool-choice h2, .toolbar-heading h2, .section-heading h2 { margin: 6px 0 10px; }
.pool-choice p:not(.eyebrow) { color: var(--muted); }
.choice-buttons, .pool-actions, .editor-actions { display: flex; flex-wrap: wrap; align-items: end; gap: 12px; }
.choice-buttons { justify-content: center; margin-top: 24px; }
.button-primary, .button-secondary { border: 1px solid #37506f; border-radius: 8px; padding: 11px 16px; font: inherit; font-weight: 700; cursor: pointer; }
.button-primary { background: #18c467; border-color: #18c467; color: #07170e; }
.button-secondary { background: #131f34; color: #e9f0fa; }
button:disabled { opacity: .48; cursor: not-allowed; }
.toolbar-heading, .section-heading, .progress-heading, .progress-detail { display: flex; justify-content: space-between; align-items: center; gap: 16px; }
.toolbar-heading { margin-bottom: 18px; }
.objective-switch, .pool-actions label, .pool-editor label { display: grid; gap: 7px; color: #9baac0; font-size: .9rem; }
.pool-actions select, .pool-actions input:not([type="checkbox"]), .objective-switch select, .pool-editor select, .pool-editor textarea { min-height: 42px; border: 1px solid #354b69; border-radius: 7px; padding: 9px 11px; background: #111c30; color: #edf2fb; font: inherit; }
/* 复选框不吃上面那套输入框规格（min-height/padding 会把它撑成 42px 的方块）。 */
.board-option input { flex: 0 0 auto; width: 15px; height: 15px; margin: 0; accent-color: #18c467; }
.pool-actions .ticker-input input { width: 140px; }
.board-filter { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; padding: 8px 12px; border: 1px solid #354b69; border-radius: 7px; background: #111c30; }
.board-option { display: flex; align-items: center; gap: 5px; color: #9baac0; font-size: .9rem; }
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
.signal-controls-row { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 12px; margin: 0 0 12px; color: #8798b1; font-size: .88rem; }
.signal-sort { display: flex; align-items: center; gap: 8px; color: #9baac0; }
.signal-sort select { width: auto; min-width: 0; border: 1px solid #354b69; border-radius: 7px; padding: 6px 8px; background: #111c30; color: #edf2fb; font: inherit; }
.section-heading { margin-bottom: 16px; }
.section-heading h2 span { color: #18c467; }
.signal-panel { min-height: 180px; }
.signal-panel .section-heading small { color: #899ab3; white-space: nowrap; }
.signal-list { display: grid; gap: 8px; }
.history-groups, .history-group { display: grid; gap: 8px; }
.history-groups { gap: 16px; }
.history-group-label { margin: 0; color: #8798b1; font-size: .82rem; }
.signal-pagination { display: flex; justify-content: flex-end; align-items: center; gap: 8px; margin-top: 12px; color: #91a1b8; font-size: .82rem; }
.signal-pagination .button-secondary { padding: 6px 9px; font-size: .82rem; }
.research-estimate { color: #91a1b8; font-size: .78rem; white-space: nowrap; align-self: center; }
.signal-row, .history-row { display: grid; gap: 5px; padding: 10px 12px; border-left: 3px solid #18c467; background: #111c30; }
/* 关注开关贴在文字下面，不占满整行宽度。 */
.signal-row .pool-follow-button { justify-self: start; margin-top: 2px; padding-left: 0; }
.signal-row span, .history-row span, .history-row small { color: #9aaac0; font-size: .88rem; }
.risk-signal { border-left-color: #ff6a69; }
.holding-row { border-left-color: #6380a5; }
.candidate-apply-grid article { padding: 16px; border: 1px solid #263850; background: #101a2d; }
.pool-pagination { display: flex; flex-wrap: wrap; justify-content: flex-end; align-items: center; gap: 10px; margin-top: 14px; color: #91a1b8; font-size: .88rem; }
.pool-pagination label { display: flex; align-items: center; gap: 7px; }
.pool-pagination select { border: 1px solid #354b69; border-radius: 7px; padding: 6px 8px; background: #111c30; color: #edf2fb; font: inherit; }
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
