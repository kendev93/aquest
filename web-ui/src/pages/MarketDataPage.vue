<script setup lang="ts">
import PageIntro from '../components/PageIntro.vue'
import { computed, onMounted, reactive, ref } from 'vue'

import { ApiError, describeError } from '../api/client'
import { INTERACTIVE_JOB_TIMEOUT_MS, PollFatalError, PollTimeoutError, pollUntil, useBackgroundPoll } from '../composables/usePolling'
import {
  cancelMarketDataJob,
  createHealthCheck,
  createHealthRepair,
  createLocalImport,
  createOnlineRefresh,
  createOnlineSync,
  deleteMarketBars,
  detectLocalVipdoc,
  fetchActiveMarketDataJobs,
  fetchLocalChart,
  fetchLocalInstruments,
  fetchStoreStatus,
  getMarketDataJob,
} from '../api/marketData'
import type {
  DataStoreStatus,
  InstrumentBoard,
  InstrumentType,
  LocalInstrument,
  LocalInstrumentMeta,
  LocalMarketChart,
  LocalMarketScope,
  LocalVipdocCandidate,
  MarketChartPeriod,
  MarketDataHealthReport,
  MarketHealthJobState,
  MarketHealthRepairOutcome,
  MarketSyncJobState,
} from '../types'
import MarketCandlestickChart from '../components/MarketCandlestickChart.vue'
import TermHint from '../components/TermHint.vue'
import AppTopbar from '../components/AppTopbar.vue'
import { BOARD_OPTIONS, INSTRUMENT_TYPE_OPTIONS } from '../utils/instrumentOptions'
import { formatDuration, formatNumber } from '../utils/format'
import { formatChinaTimestamp } from '../utils/time'

const MA_OPTIONS = [
  { key: 'ma5', label: 'MA5' },
  { key: 'ma10', label: 'MA10' },
  { key: 'ma20', label: 'MA20' },
  { key: 'ma60', label: 'MA60' },
]

// 本系统只研究股票：基金（含 ETF）、指数和 B 股既不在线同步也不本地导入，
// 所以不再作为可选项出现。在线通道与本地导入的范围现在是同一套。
const form = reactive({
  syncUniverse: 'all' as 'all' | 'sh' | 'sz',
  syncInstrumentTypes: ['stock'] as InstrumentType[],
  syncBoards: [] as InstrumentBoard[],
  // 默认不强制：本交易日已经下完时，点一次应该什么都没发生，而不是把 5000 多只重新问一遍。
  syncForce: false,
  // 本地导入走自己的范围：默认不限制，否则会沿用在线的默认勾选，
  // 把未勾选的板块静默过滤掉而界面上完全看不出来。
  localInstrumentTypes: [] as InstrumentType[],
  localBoards: [] as InstrumentBoard[],
  vipdocPath: '',
  vipdocDetected: [] as LocalVipdocCandidate[],
  vipdocMountHint: '',
  vipdocDetecting: false,
  vipdocDetectNote: '',
  vipdocHostPrefix: '',
  market: 'all' as LocalMarketScope,
  keyword: '',
  page: 1,
  pageSize: 50,
  startDate: '',
  endDate: '',
})
const instruments = ref<LocalInstrument[]>([])
const listMeta = ref<LocalInstrumentMeta | null>(null)
const selected = ref<LocalInstrument | null>(null)
// 目录到底读到过没有。挂载时若有任务在跑会跳过首屏加载（见 onMounted），
// 任务结束那次刷新就是唯一的补调时机，否则列表和 K 线会一直空白。
const catalogLoaded = ref(false)
const chart = ref<LocalMarketChart | null>(null)
const maKeys = ref(MA_OPTIONS.map((option) => option.key))
const period = ref<MarketChartPeriod>('daily')
const showRsi = ref(true)
const showMacd = ref(true)
const loading = ref(false)
const chartLoading = ref(false)
const message = ref('')
const storeStatus = ref<DataStoreStatus | null>(null)
const syncLoading = ref(false)
const syncLabel = ref('')
const syncNotice = ref('')
const marketJob = ref<MarketSyncJobState | null>(null)
const activeMarketJobs = ref<MarketSyncJobState[]>([])
const MARKET_JOB_POLL_INTERVAL_MS = 500
const MARKET_STATUS_POLL_INTERVAL_MS = 5000
const ACTIVE_JOB_POLL_INTERVAL_MS = 1000
const MARKET_POLL_RETRY_STEP_MS = 2_000
const MARKET_POLL_RETRY_MAX_MS = 10_000
/** 连续读状态到这个次数就认输：一直 5xx 说明服务真出问题，无限重试只是把假死藏起来。 */
const MARKET_POLL_MAX_FAILURES = 10

/** 体检/修复任务由健康检查面板负责显示，同一个任务不在上方同步面板再画一条进度条。 */
const healthOwnedJobId = ref('')
/** 按任务记「页面已经不再跟踪」的原因：进度条停在旧数字上时必须同时说明它在停什么。 */
const trackingLostReason = ref<Record<string, string>>({})

function trackingLostText(jobId: string | undefined): string {
  return jobId ? trackingLostReason.value[jobId] ?? '' : ''
}

function clearTrackingLost(jobId: string): void {
  if (!trackingLostReason.value[jobId]) return
  const rest = { ...trackingLostReason.value }
  delete rest[jobId]
  trackingLostReason.value = rest
}

/**
 * 读一次任务状态失败不代表任务死了。
 *
 * 以前一次网络抖动就把 `pollUntil` 打断，而进度条继续显示「正在重新下载… 13%」：
 * 用户既看不出页面已经放弃，也看不出后台还在跑，这正是「不知道好了还是没好」的来路。
 * 任务是终态（`until` 抛的 `PollFatalError`）和状态已被回收（404）不该重试——
 * 前者会把真失败藏成十次重试，后者再怎么等都不会再有状态。
 */
function marketPollRetryDelay(error: unknown, failures: number): number | null {
  if (error instanceof PollFatalError) return null
  if (error instanceof ApiError && error.status === 404) return null
  if (failures >= MARKET_POLL_MAX_FAILURES) return null
  return Math.min(MARKET_POLL_RETRY_STEP_MS * failures, MARKET_POLL_RETRY_MAX_MS)
}

function noteTrackingLost(job: MarketSyncJobState | null): void {
  if (!job || (job.status !== 'queued' && job.status !== 'running')) return
  // 失败原因由提示条负责，这里只负责说一句"下面的数字不再更新了，但任务还活着"。
  trackingLostReason.value = {
    ...trackingLostReason.value,
    [job.job_id]: '页面已停止读取任务状态，后台任务仍在运行；下面的数字停在最后一次读到的状态。',
  }
}

const selectedTitle = computed(() => (
  selected.value ? `${selected.value.market} ${selected.value.code}` : '请选择品种'
))

/**
 * 任务阶段的人话。`deleting` 是修复独有的第一段：它没有任何可报的中间量，
 * 不点名的话用户看到的就是一条钉在 0% 的进度条，而这段时间命中标的在库里是空的。
 */
const MARKET_PHASE_TEXT: Record<string, string> = {
  planning: '正在规划同步区间…',
  deleting: '正在删除待修标的的全部历史…',
  fetching: '正在并发下载在线日线…',
  instrument_discovery: '正在刷新证券列表…',
  online_bars: '正在同步在线日线…',
  merging: '正在合并行情分片…',
  cleanup: '正在清理临时分片…',
}

/** 进度条的数字是**按阶段**归一的：合并阶段会从现在的 99% 掉回 60% 左右，所以百分比必须跟着阶段名一起看。 */
function progressPhaseText(job: MarketSyncJobState | null): string {
  if (!job) return ''
  if (job.status === 'queued') return '排队等待行情任务闸门…'
  return MARKET_PHASE_TEXT[job.phase ?? ''] ?? ''
}

const marketJobTitle = computed(() => {
  if (marketJob.value?.status === 'queued' || marketJob.value?.status === 'running') {
    return activeJobDescription(marketJob.value)
  }
  if (syncLoading.value) return syncLabel.value || '后台任务进行中…'
  return '最近一次任务'
})

/** 同步与体检/修复共用这套显示函数：同一个任务不该在两个面板各画一套口径。 */
function isLiveMarketJob(job: MarketSyncJobState | null): boolean {
  return job?.status === 'queued' || job?.status === 'running'
}

function progressWorkerCount(job: MarketSyncJobState | null): number {
  const value = job?.metadata?.worker_count
  if (typeof value === 'number' && Number.isFinite(value)) return value
  return job?.worker_count ?? 8
}

function currentSyncDetail(job: MarketSyncJobState | null): Record<string, unknown> | null {
  const value = job?.metadata?.current_instrument
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  return value as Record<string, unknown>
}

function currentSyncInstrumentText(job: MarketSyncJobState | null): string {
  const detail = currentSyncDetail(job)
  if (!detail) return '—'
  const market = typeof detail.market === 'string' ? detail.market : ''
  const code = typeof detail.code === 'string' ? detail.code : ''
  const symbol = typeof detail.symbol === 'string' ? detail.symbol : ''
  const name = typeof detail.name === 'string' && detail.name ? detail.name : ''
  const assetType = typeof detail.asset_type === 'string' ? detail.asset_type : ''
  const identity = [market, code].filter(Boolean).join(' ') || symbol
  return [identity, name, assetType].filter(Boolean).join(' · ') || '—'
}

function currentSyncRangeText(job: MarketSyncJobState | null): string {
  const metadata = job?.metadata
  if (!metadata) return '—'
  const start = typeof metadata.range_start === 'string'
    ? metadata.range_start
    : typeof metadata.requested_start === 'string' ? metadata.requested_start : ''
  const end = typeof metadata.range_end === 'string'
    ? metadata.range_end
    : typeof metadata.requested_end === 'string' ? metadata.requested_end : ''
  if (start && end) return `${start} → ${end}`
  return typeof metadata.range_label === 'string' ? metadata.range_label : '正在读取日期范围…'
}

function currentSyncStatusText(job: MarketSyncJobState | null): string {
  const status = job?.metadata?.current_status
  return {
    fetching: '正在请求',
    imported: '已导入',
    updated: '已更新',
    unchanged: '无变化',
    error: '失败',
  }[typeof status === 'string' ? status : ''] ?? '处理中'
}

/**
 * 并发下载时这只标的只是「某个 worker 最后上报的那一只」。
 *
 * 8 路进程池都往同一个 `current_instrument` 键上写自己那只，谁最后写完谁上屏；
 * 说「当前同步」会让用户以为整条任务只在传这只票，进而误判「下错标的了」。
 */
function currentSyncDetailLabel(job: MarketSyncJobState | null): string {
  if (job?.status !== 'running') return '最近同步'
  const workers = progressWorkerCount(job)
  return workers > 1 ? `${workers} 路并发下载中，这是最近上报的一只` : '当前同步'
}

/** 哪一路 worker 报的，用来解释这条记录为什么会被下一秒换掉。 */
function currentSyncWorkerText(job: MarketSyncJobState | null): string {
  const value = job?.metadata?.worker_id
  return typeof value === 'number' && Number.isFinite(value) ? `worker ${value} 上报` : ''
}

function currentSyncBarCount(job: MarketSyncJobState | null): number | null {
  const value = job?.metadata?.bar_count
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function currentSyncRetry(job: MarketSyncJobState | null): number {
  const value = job?.metadata?.retry
  return typeof value === 'number' && Number.isFinite(value) ? value : 0
}

const catalogCountsText = computed(() => {
  const counts = marketJob.value?.result?.catalog_counts
  if (!counts) return ''
  return Object.entries(counts).map(([label, count]) => `${label} ${count}`).join(' · ')
})

const lastFullSyncText = computed(() => {
  const summary = storeStatus.value?.last_market_sync_summary
  if (!summary) return ''
  const completed = typeof summary.completed === 'number' ? summary.completed : 0
  const skipped = typeof summary.skipped === 'number' ? summary.skipped : 0
  const failed = typeof summary.failed === 'number' ? summary.failed : 0
  const finishedAt = typeof summary.finished_at === 'string' ? summary.finished_at : ''
  return `上次全市场同步：完成 ${completed} · 跳过 ${skipped} · 失败 ${failed}${finishedAt ? ` · ${formatChinaTimestamp(finishedAt)}` : ''}`
})

/**
 * 行情比交易日历新时给一句解释。
 *
 * 日历表只由自动同步在收盘后（18:30 之后）写入，手动同步只补日线、不碰它，所以
 * 只要那晚容器没在线、或日历请求失败回退到自然日，日历就会停在最后一个成功写入的
 * session。面板上三个日期来自两条路径，不点破就会被读成"日期错了"——而停牌判定和
 * 自动同步的目标 session 都以这张表为准，滞后本身是有后果的。
 */
const calendarBehindBarsText = computed(() => {
  const latest = storeStatus.value?.market_calendar_latest_date
  const dataEnd = storeStatus.value?.data_end
  if (!latest || !dataEnd || latest >= dataEnd) return ''
  return `交易日历停在 ${latest}，行情已到 ${dataEnd}：日历只由自动同步在收盘后写入，手动同步不会更新它`
})

/**
 * 按任务记最近 30 秒的轮询采样。
 *
 * 速率只能在这儿算：任务状态里没有一个"开始下载"的时间戳（`created_at` 还含着排队
 * 和删除），而后端各阶段的总数口径又各不相同。更要紧的是页面侧能分辨"跑得慢"和
 * "真卡住"——一条不动的百分比分不出这两种情况。
 */
interface PaceSample {
  at: number
  scanned: number
}

const PACE_WINDOW_MS = 30_000
const PACE_MIN_SAMPLES_MS = 3_000
const paceSamples = ref<Record<string, PaceSample[]>>({})

function noteProgressSample(job: MarketSyncJobState): void {
  if (job.status !== 'running') {
    if (!paceSamples.value[job.job_id]) return
    const rest = { ...paceSamples.value }
    delete rest[job.job_id]
    paceSamples.value = rest
    return
  }
  const now = Date.now()
  const kept = [...(paceSamples.value[job.job_id] ?? []), { at: now, scanned: job.total_scanned }]
    .filter((sample) => now - sample.at <= PACE_WINDOW_MS)
    .slice(-60)
  paceSamples.value = { ...paceSamples.value, [job.job_id]: kept }
}

/** 进度条的百分比是按阶段归一的，所以单位也跟着阶段走：合并数的是区间，不是标的。 */
function progressUnit(job: MarketSyncJobState | null): string {
  if (job?.phase === 'merging') return '个区间'
  if (job?.phase === 'cleanup') return '个分片'
  return '只'
}

function progressPaceText(job: MarketSyncJobState | null): string {
  if (!job || job.status !== 'running' || job.total_candidates <= 1) return ''
  const samples = paceSamples.value[job.job_id] ?? []
  if (samples.length < 2) return ''
  const first = samples[0]
  const last = samples[samples.length - 1]
  const elapsedSeconds = (last.at - first.at) / 1000
  if (elapsedSeconds < PACE_MIN_SAMPLES_MS / 1000) return ''
  const advanced = last.scanned - first.scanned
  const unit = progressUnit(job)
  if (advanced <= 0) {
    return elapsedSeconds >= 5
      ? `最近 ${Math.round(elapsedSeconds)} 秒没有新增进度（多半卡在等在线源或等库）`
      : ''
  }
  const perSecond = advanced / elapsedSeconds
  const remaining = Math.max(job.total_candidates - job.total_scanned, 0)
  const eta = remaining > 0 ? ` · 本阶段还剩约 ${formatDuration(remaining / perSecond)}` : ' · 本阶段即将结束'
  return `实测 ${perSecond.toFixed(1)} ${unit}/秒${eta}`
}

/** 阶段那行只说「这一步跑得多快、还差多久」；已下载多少只由上面的统计行负责，两行不重复。 */
function progressStageNote(job: MarketSyncJobState | null): string {
  if (!job) return ''
  if (job.phase === 'instrument_discovery' && !currentSyncDetail(job)) return '等待在线证券列表返回…'
  return progressPaceText(job)
}

/**
 * 只报 0/1 的任务（体检就是：扫完之前拿不出分母）用动画条而不是 0%：
 * 一条钉在 0% 的实心和「卡住了」长得一模一样，而体检本来就要跑几十秒。
 */
function marketProgressIndeterminate(job: MarketSyncJobState | null): boolean {
  return Boolean(job?.status === 'running' && job.total_candidates <= 1)
}

function marketProgressSummary(job: MarketSyncJobState | null): string {
  if (!job) return '已处理 0 / 0'
  const metadata = job.metadata ?? {}
  if (job.phase === 'deleting') {
    const targets = typeof metadata.delete_targets === 'number' ? metadata.delete_targets : job.total_candidates
    const range = typeof metadata.delete_range === 'string' ? `（${metadata.delete_range}）` : ''
    return `待删除 ${targets} 只标的的全部历史${range}`
  }
  if (job.phase === 'merging') {
    const planned = typeof metadata.planned_jobs === 'number' ? metadata.planned_jobs : null
    if (planned !== null) {
      const completed = typeof metadata.completed === 'number' ? metadata.completed : job.total_scanned
      const failed = typeof metadata.failed === 'number' ? metadata.failed : job.errors
      return `已合并 ${completed + failed} / ${planned} 个区间`
    }
    const merged = typeof metadata.merged_workers === 'number' ? metadata.merged_workers : job.total_scanned
    const total = typeof metadata.total_workers === 'number' ? metadata.total_workers : job.worker_count ?? 8
    return `已合并 ${merged} / ${total} 个分片`
  }
  if (job.phase === 'fetching') {
    const downloaded = typeof metadata.downloaded_targets === 'number'
      ? metadata.downloaded_targets
      : typeof metadata.completed === 'number' ? metadata.completed + (typeof metadata.failed === 'number' ? metadata.failed : 0) : job.total_scanned
    const total = typeof metadata.planned_jobs === 'number' ? metadata.planned_jobs : job.total_candidates
    return `已下载 ${downloaded} / ${total}`
  }
  return `已处理 ${job.total_scanned} / ${job.total_candidates}`
}

function activeJobDescription(job: MarketSyncJobState): string {
  return job.description || '行情同步'
}

function apiMessage(error: unknown): string {
  return describeError(error, { fallback: '行情数据读取失败，请检查数据导入状态后重试。' })
}

function hasActiveMarketDataJob(): boolean {
  // 体检和修复一样独占行情库，所以它们进行中时 503 同样是噪声，不该弹错误条。
  const health = healthJob.value
  return syncLoading.value
    || activeMarketJobs.value.some((job) => job.status === 'queued' || job.status === 'running')
    || marketJob.value?.status === 'queued'
    || marketJob.value?.status === 'running'
    || health?.status === 'queued'
    || health?.status === 'running'
}

function isTransientStoreStatusError(error: unknown): boolean {
  return error instanceof ApiError
    && error.status === 503
    && hasActiveMarketDataJob()
}

// 后端说「不用下载」时要讲清三件事：覆盖到哪一天、这个范围有多少只、想硬下该怎么办。
function skippedSyncText(job: {
  session?: string | null
  instruments?: number | null
  synced_at?: string | null
}): string {
  const session = job.session ?? '最新交易日'
  const at = job.synced_at ? `，完成于 ${formatChinaTimestamp(job.synced_at)}` : ''
  return `本地已覆盖 ${session} 收盘${at}，本次没有下载。要重下请勾选「强制重新下载」。`
}

function syncScopePayload(): {
  instrument_types?: InstrumentType[]
  boards?: InstrumentBoard[]
} {
  return {
    ...(form.syncInstrumentTypes.length > 0
      ? { instrument_types: [...form.syncInstrumentTypes] }
      : {}),
    ...(form.syncBoards.length > 0 ? { boards: [...form.syncBoards] } : {}),
  }
}

function localScopePayload(): {
  instrument_types?: InstrumentType[]
  boards?: InstrumentBoard[]
} {
  return {
    ...(form.localInstrumentTypes.length > 0
      ? { instrument_types: [...form.localInstrumentTypes] }
      : {}),
    ...(form.localBoards.length > 0 ? { boards: [...form.localBoards] } : {}),
  }
}

function toggleInstrumentType(type: InstrumentType): void {
  form.syncInstrumentTypes = form.syncInstrumentTypes.includes(type)
    ? form.syncInstrumentTypes.filter((item) => item !== type)
    : [...form.syncInstrumentTypes, type]
}

function toggleLocalInstrumentType(type: InstrumentType): void {
  form.localInstrumentTypes = form.localInstrumentTypes.includes(type)
    ? form.localInstrumentTypes.filter((item) => item !== type)
    : [...form.localInstrumentTypes, type]
}

function toggleLocalBoard(board: InstrumentBoard): void {
  form.localBoards = form.localBoards.includes(board)
    ? form.localBoards.filter((item) => item !== board)
    : [...form.localBoards, board]
}

function toggleBoard(board: InstrumentBoard): void {
  form.syncBoards = form.syncBoards.includes(board)
    ? form.syncBoards.filter((item) => item !== board)
    : [...form.syncBoards, board]
}

async function loadStoreStatus(): Promise<void> {
  try {
    const status = await fetchStoreStatus()
    storeStatus.value = status
    const pendingJob = status.auto_sync_job_id
      ? { id: status.auto_sync_job_id, label: '正在自动同步最新行情…' }
      : null
    if (pendingJob && marketJob.value?.job_id !== pendingJob.id) {
      syncLoading.value = true
      syncLabel.value = pendingJob.label
      try {
        beginJob(pendingJob.id, 'running')
        await waitForJob(pendingJob.id)
        await loadStoreStatus()
        await loadInstruments()
      } catch (error) {
        message.value = apiMessage(error)
      } finally {
        syncLoading.value = false
        syncLabel.value = ''
      }
    }
  } catch (error) {
    if (isTransientStoreStatusError(error)) return
    message.value = apiMessage(error)
  }
}

function updateActiveMarketJob(state: MarketSyncJobState): void {
  if (state.status === 'completed' || state.status === 'failed' || state.status === 'cancelled') {
    activeMarketJobs.value = activeMarketJobs.value.filter((job) => job.job_id !== state.job_id)
    return
  }
  const current = activeMarketJobs.value.filter((job) => job.job_id !== state.job_id)
  activeMarketJobs.value = [...current, state]
}

async function loadActiveMarketJobs(): Promise<void> {
  try {
    const previous = activeMarketJobs.value
    const running = (await fetchActiveMarketDataJobs()) ?? []
    running.forEach(noteProgressSample)
    // 体检/修复交给健康检查面板显示；在两个面板各画一条进度条只会让人对不上数。
    const jobs = running.filter((job) => job.job_id !== healthOwnedJobId.value)
    activeMarketJobs.value = jobs
    const activeIds = new Set(jobs.map((job) => job.job_id))
    for (const previousJob of previous) {
      if (activeIds.has(previousJob.job_id)) continue
      try {
        const terminal = await getMarketDataJob(previousJob.job_id)
        if (terminal.status === 'completed' || terminal.status === 'failed' || terminal.status === 'cancelled') {
          if (marketJob.value?.job_id === terminal.job_id) marketJob.value = terminal
          if (terminal.status === 'cancelled') {
            message.value = `行情任务 ${terminal.job_id.slice(0, 8)}… 已取消，已经写入的数据已保留。`
          }
        }
      } catch {
        // The in-memory runner may evict a completed task before this refresh.
      }
    }
    const activeCurrent = marketJob.value && jobs.find((job) => job.job_id === marketJob.value?.job_id)
    if (activeCurrent) {
      marketJob.value = activeCurrent
    } else if (jobs.length > 0) {
      marketJob.value = jobs[0]
    }
    if (jobs.length > 0) {
      syncLoading.value = true
      if (!syncLabel.value) syncLabel.value = activeJobDescription(jobs[0])
    } else {
      const currentStatus = marketJob.value?.status
      const currentFinished = currentStatus === 'completed'
        || currentStatus === 'failed'
        || currentStatus === 'cancelled'
      if (!syncLoading.value || (currentStatus && !currentFinished)) return
      syncLoading.value = false
      syncLabel.value = ''
      if (!catalogLoaded.value) {
        // loadInstruments 会先清空提示条（它自己的失败分支要写 message），所以刚写下的
        // 任务终态提示——例如「已取消」——得先保住，否则会被这次补调吞掉。
        const notice = message.value
        await loadInstruments()
        if (notice && !message.value) message.value = notice
      }
    }
  } catch {
    // Active-job discovery is informational; the individual task poll remains authoritative.
  }
}

async function waitForJob(jobId: string): Promise<void> {
  let state: MarketSyncJobState
  try {
    state = await pollUntil(
      async () => {
        const current = await getMarketDataJob(jobId)
        marketJob.value = current
        updateActiveMarketJob(current)
        noteProgressSample(current)
        return current
      },
      {
        intervalMs: MARKET_JOB_POLL_INTERVAL_MS,
        retryableError: marketPollRetryDelay,
        until: (current) => {
          if (current.status === 'failed') {
            throw new PollFatalError(current.error ?? '行情同步失败。')
          }
          return current.status === 'completed' || current.status === 'cancelled'
        },
      },
    )
  } catch (error) {
    noteTrackingLost(marketJob.value)
    throw error
  }
  if (state.status === 'completed') {
    if (state.result?.errors) {
      message.value = `同步完成，但有 ${state.result.errors} 个错误。`
    } else {
      message.value = ''
    }
  } else {
    message.value = '行情同步已取消，已经写入的数据已保留。'
  }
}

function beginJob(jobId: string, status: string): void {
  const state: MarketSyncJobState = {
    job_id: jobId,
    status: status as MarketSyncJobState['status'],
    progress: 0,
    total_candidates: 0,
    total_scanned: 0,
    errors: 0,
    error: null,
    result: null,
    metadata: {},
  }
  clearTrackingLost(jobId)
  marketJob.value = state
  updateActiveMarketJob(state)
}

async function cancelActiveMarketJob(jobId: string): Promise<void> {
  try {
    await cancelMarketDataJob(jobId)
    message.value = `已请求取消任务 ${jobId.slice(0, 8)}…，正在等待当前请求结束。`
    await loadActiveMarketJobs()
  } catch (error) {
    message.value = apiMessage(error)
  }
}

async function syncOnlineData(): Promise<void> {
  syncLoading.value = true
  syncLabel.value = '正在同步行情…'
  message.value = ''
  syncNotice.value = ''
  try {
    // 不发 bars：这个字段从请求到配置都无人消费（区间只由 _requested_ranges 决定），
    // 界面上也没有"拉多少根"可调。
    const job = await createOnlineSync({
      universe: form.syncUniverse,
      ...syncScopePayload(),
      force: form.syncForce,
    })
    // 后端判定「这个范围已经下到了本交易日收盘」时不会建任务：没有 job_id 可跟，
    // 直接把结论说给用户，而不是让他盯一条 0/0 的进度条。
    if (job.skipped) {
      syncNotice.value = skippedSyncText(job)
      await loadStoreStatus()
      return
    }
    beginJob(job.job_id ?? '', job.status)
    await waitForJob(job.job_id ?? '')
    await loadStoreStatus()
    await loadInstruments()
  } catch (error) {
    message.value = apiMessage(error)
  } finally {
    syncLoading.value = false
    syncLabel.value = ''
  }
}

async function refreshOnlineData(): Promise<void> {
  if (
    !window.confirm(
      '将删除所选范围内标的的全部历史日线，再整段重新下载（本地导入的行也会一起删掉）。在线源已经没有数据的标的（如退市）删了就补不回来，会在结果里单独列出。确定继续？',
    )
  ) {
    return
  }
  syncLoading.value = true
  syncLabel.value = '正在删除并重下全部历史…'
  message.value = ''
  try {
    const job = await createOnlineRefresh({
      universe: form.syncUniverse,
      ...syncScopePayload(),
      confirm: true,
    })
    beginJob(job.job_id, job.status)
    await waitForJob(job.job_id)
    await loadStoreStatus()
    await loadInstruments()
    const result = marketJob.value?.result
    if (result) {
      const empty = result.empty_after_refresh ?? []
      message.value = empty.length
        ? `重刷完成：删除 ${result.deleted_bars ?? 0} 根、重写 ${result.written_bars ?? 0} 根；${empty.length} 只标的重下后仍无数据：${empty.join('、')}`
        : `重刷完成：删除 ${result.deleted_bars ?? 0} 根、重写 ${result.written_bars ?? 0} 根。`
    }
  } catch (error) {
    message.value = apiMessage(error)
  } finally {
    syncLoading.value = false
    syncLabel.value = ''
  }
}

async function detectVipdocPath(fillWhenEmpty: boolean): Promise<void> {
  form.vipdocDetecting = true
  form.vipdocDetectNote = ''
  try {
    const result = await detectLocalVipdoc()
    form.vipdocDetected = result.candidates
    form.vipdocMountHint = result.mount_hint
    form.vipdocHostPrefix = result.host_mount_prefix ?? ''
    if (!result.candidates.length) {
      form.vipdocDetectNote = '没有在容器里找到通达信行情目录。' + result.mount_hint
      return
    }
    const best = result.candidates[0]
    if (fillWhenEmpty && !form.vipdocPath.trim()) {
      form.vipdocPath = best.path
    }
    const total = result.candidates.reduce((sum, item) => sum + item.file_count, 0)
    const hostHint = form.vipdocHostPrefix
      ? `；也可以直接填电脑上 ${form.vipdocHostPrefix} 开头的路径`
      : ''
    form.vipdocDetectNote = `找到 ${result.candidates.length} 个目录、共 ${total} 个日线文件，已填入文件最多的：${best.path}${hostHint}`
  } catch (error) {
    form.vipdocDetectNote = apiMessage(error)
  } finally {
    form.vipdocDetecting = false
  }
}

async function importLocalDayFiles(): Promise<void> {
  syncLoading.value = true
  syncLabel.value = '正在导入本地行情…'
  message.value = ''
  try {
    const job = await createLocalImport({
      universe: form.syncUniverse,
      ...(form.vipdocPath.trim() ? { vipdoc_path: form.vipdocPath.trim() } : {}),
      ...localScopePayload(),
    })
    beginJob(job.job_id, job.status)
    await waitForJob(job.job_id)
    await loadStoreStatus()
    await loadInstruments()
  } catch (error) {
    message.value = apiMessage(error)
  } finally {
    syncLoading.value = false
    syncLabel.value = ''
  }
}

const undoingLocalImport = ref(false)

async function undoLocalImport(): Promise<void> {
  if (!window.confirm('将删除所有「本地导入」写入的日线，在线同步写入的数据保留。删除后需要重新导入或重新在线同步补齐。确定继续？')) {
    return
  }
  undoingLocalImport.value = true
  message.value = ''
  try {
    const result = await deleteMarketBars({ source: 'local', universe: form.syncUniverse, confirm: true })
    await loadStoreStatus()
    await loadInstruments()
    // loadInstruments() 会把 message 清空，所以提示必须放在刷新之后。
    message.value = `已删除 ${result.deleted_bars} 根本地导入的日线，请重新导入或在线同步补齐。`
  } catch (error) {
    message.value = apiMessage(error)
  } finally {
    undoingLocalImport.value = false
  }
}

/** 本地导入的报告用的是另一套键名，结果面板要单独渲染，否则全是 0。 */
function isLocalImportResult(result: Record<string, unknown> | null | undefined): boolean {
  if (!result) return false
  return result.source === 'local' || typeof result.discovered_files === 'number'
}

const healthReport = ref<MarketDataHealthReport | null>(null)
const healthChecking = ref(false)
const healthRepairing = ref(false)
const healthRepaired = ref<MarketHealthRepairOutcome | null>(null)
const healthJob = ref<MarketHealthJobState | null>(null)
const healthMessage = ref('')
/** 有问题的检查项才能送去重下；零成交重下多半还是同一份数据，不给按钮。 */
const healthIssues = computed(() =>
  (healthReport.value?.checks ?? []).filter(
    (check) => check.affected_instruments > 0 && check.repairable,
  ),
)

const healthSummary = computed(() => {
  const checks = healthReport.value?.checks ?? []
  const bad = checks.filter((check) => check.severity !== 'ok')
  if (!checks.length) return null
  return {
    total: checks.length,
    bad: bad.length,
    instruments: healthReport.value?.instrument_count ?? 0,
    days: healthReport.value?.calendar_days ?? 0,
  }
})

const healthJobTitle = computed(() => {
  const description = healthJob.value?.description
  if (description) return healthRepairing.value ? `${description}（先删后下）` : description
  return healthRepairing.value ? '正在准备修复…' : '正在扫描行情库…'
})

/**
 * 删多少、回多少 = 在线源给的就是同一批日线。
 *
 * 这时候缺口不是"没下载下来"，重下改不出任何东西。不点破的话，用户只会反复点
 * 「一键修复」，每轮删上百万行、重下几分钟、把指标缓存整体作废一遍。
 */
function repairNoChangeText(outcome: MarketHealthRepairOutcome | null): string {
  if (!outcome || !outcome.requested_codes) return ''
  const deleted = outcome.deleted_bars ?? 0
  const written = outcome.written_bars ?? 0
  if (!deleted || deleted !== written || (outcome.errors ?? 0) > 0) return ''
  return `删 ${deleted} 根、原样写回 ${written} 根：这批缺口在线源同样没有（多为长期停牌），重下改不出任何东西，别反复点。`
}

function severityText(severity: string): string {
  if (severity === 'error') return '异常'
  if (severity === 'warn') return '提示'
  return '正常'
}

function beginHealthJob(jobId: string, status: string): void {
  healthJob.value = {
    job_id: jobId,
    status: status as MarketHealthJobState['status'],
    progress: 0,
    total_candidates: 0,
    total_scanned: 0,
    errors: 0,
    error: null,
    result: null,
  }
  healthOwnedJobId.value = jobId
  clearTrackingLost(jobId)
  // 页面可能在任务开跑之后才挂上（刷新过），那时上方同步面板已经把这个任务收下了。
  // 健康面板接手后要撤掉上面那一份，否则同一个任务两条进度条，数字还会互相打脸。
  if (marketJob.value?.job_id === jobId) marketJob.value = null
}

/**
 * 等待体检/修复任务结束。
 *
 * 只有体检带预算：它通常几秒到几分钟，卡住就是后端或队列有问题。
 * 一键修复是先删后下全市场，跑几小时属于正常，所以有意不传 timeoutMs。
 * 读状态失败先退避重试而不是直接放弃——中途退出会把进度条冻在最后一次读到的数字上。
 */
async function waitForHealthJob(jobId: string, timeoutMs?: number): Promise<void> {
  let state: MarketHealthJobState
  try {
    state = await pollUntil(
      async () => {
        const current = (await getMarketDataJob(jobId)) as unknown as MarketHealthJobState
        healthJob.value = current
        noteProgressSample(current)
        return current
      },
      {
        intervalMs: MARKET_JOB_POLL_INTERVAL_MS,
        timeoutMs,
        retryableError: marketPollRetryDelay,
        until: (current) => {
          if (current.status === 'failed') {
            throw new PollFatalError(current.error ?? '行情任务失败。')
          }
          return current.status === 'completed' || current.status === 'cancelled'
        },
      },
    )
  } catch (error) {
    noteTrackingLost(healthJob.value)
    throw error
  }
  if (state.status === 'cancelled') healthMessage.value = '行情任务已取消。'
}

/**
 * 体检报告驱动着「一键修复」——那是先删后下，所以残缺负载必须报错，不能当成「一切正常」。
 *
 * 以前这里是一句 `as unknown as`：`checks` 只要是真值（哪怕是空数组）就照单全收，
 * 于是空报告会被渲染成「六项检查都没有命中」，而字段漂移会让 healthIssues 静默变空、
 * 把修复按钮悄悄禁用。后端永远会给出全部检查项，所以非空、且每项带 key / 计数 /
 * repairable 才算一份报告。
 */
function asHealthReport(value: unknown): MarketDataHealthReport | null {
  const report = value as Partial<MarketDataHealthReport> | null
  const checks = Array.isArray(report?.checks) ? report.checks : null
  if (!checks || !checks.length) return null
  const wellFormed = checks.every(
    (check) => typeof check?.key === 'string'
      && typeof check?.repairable === 'boolean'
      && Number.isFinite(Number(check?.affected_instruments)),
  )
  return wellFormed ? (report as MarketDataHealthReport) : null
}

async function runHealthCheck(): Promise<void> {
  healthChecking.value = true
  healthMessage.value = ''
  healthRepaired.value = null
  try {
    const job = await createHealthCheck()
    beginHealthJob(job.job_id, job.status)
    await waitForHealthJob(job.job_id, INTERACTIVE_JOB_TIMEOUT_MS)
    healthReport.value = asHealthReport(healthJob.value?.result)
    if (!healthReport.value) healthMessage.value = '体检没有返回报告，请重试。'
  } catch (error) {
    healthMessage.value = error instanceof PollTimeoutError
      ? '行情体检已等待超过 15 分钟，页面停止等待；任务仍在后台运行，可稍后重新发起体检。'
      : apiMessage(error)
  } finally {
    healthChecking.value = false
  }
}

/**
 * 修复确认框必须把两个代价写在点下去之前。
 *
 * ① 它独占行情库几分钟，这期间提交的全市场研究会因为打不开库而整条失败；
 * ② 缺口大多来自长期停牌，在线源同样没有那几天，重下改不出任何东西——
 * 2026-10-16 实测一批 500 只「删 1,133,865 根、写回 1,133,865 根，体检各项数字一项没变」。
 * 以前这两条都要等用户点完、跑完才看得见，于是"点了没变化"只能靠猜。
 */
function repairConfirmText(offset: number): string {
  const from = offset > 0 ? `本轮从名单第 ${offset + 1} 只开始` : '本轮从名单最前面开始'
  return `将删除命中检查项的标的的全部历史行情，再重新下载覆盖（${from}；一批修不完会在结果里报还剩多少只）。\n\n`
    + '先知道两个代价：修复会独占行情库几分钟，同时在跑的全市场研究会因为打不开库而失败；'
    + '而缺口大多来自长期停牌，在线源同样没有那几天，重下改不出任何东西'
    + '（2026-10-16 实测一批 500 只：删 1,133,865 根、写回 1,133,865 根，体检各项数字一项没变）。\n\n'
    + '在线源已经没有数据的标的（如退市）删了就补不回来，会在结果里单独列出。确定继续？'
}

async function repairHealth(checks: string[], offset = 0): Promise<void> {
  if (!checks.length) return
  if (!window.confirm(repairConfirmText(offset))) {
    return
  }
  healthRepairing.value = true
  healthMessage.value = ''
  try {
    const job = await createHealthRepair({ checks, offset, confirm: true })
    beginHealthJob(job.job_id, job.status)
    await waitForHealthJob(job.job_id)
    healthRepaired.value = healthJob.value?.result ?? null
    await loadStoreStatus()
    await loadInstruments()
    // 修复完原来的报告已经过期：重下之后问题可能还在（在线源同样缺那段）。
    healthReport.value = null
  } catch (error) {
    healthMessage.value = apiMessage(error)
  } finally {
    healthRepairing.value = false
  }
}

function repairAllHealth(): void {
  void repairHealth(healthIssues.value.map((check) => check.key))
}

/** 下一批的起点：本轮起点 + 本轮真正修掉的只数。名单按「市场+代码」排序，所以只能这样往后推。 */
function nextRepairOffset(outcome: MarketHealthRepairOutcome | null): number {
  if (!outcome) return 0
  return (outcome.offset ?? 0) + (outcome.requested_codes ?? 0)
}

function continueHealthRepair(): void {
  const outcome = healthRepaired.value
  const checks = outcome?.checked_keys?.length
    ? outcome.checked_keys
    : healthIssues.value.map((check) => check.key)
  void repairHealth(checks, nextRepairOffset(outcome))
}

const localImportSummary = computed(() => {
  const result = marketJob.value?.result as Record<string, unknown> | undefined
  if (!isLocalImportResult(result)) return null
  const value = result as Record<string, any>
  const reasons = Object.entries(value.failure_reasons ?? {}).map(
    ([reason, count]) => `${reason} × ${count}`,
  )
  return {
    discovered: value.discovered_files ?? 0,
    selected: value.selected_files ?? 0,
    filtered: value.filtered_files ?? 0,
    instruments: value.imported_instruments ?? 0,
    bars: value.imported_bars ?? 0,
    replaced: value.replaced_bars ?? 0,
    skipped: value.skipped_files ?? 0,
    range: value.range_start && value.range_end ? `${value.range_start} → ${value.range_end}` : '—',
    reasons,
  }
})

async function loadInstruments(): Promise<void> {
  const requestId = ++instrumentsRequestId
  loading.value = true
  message.value = ''
  try {
    const response = await fetchLocalInstruments({
      market: form.market,
      keyword: form.keyword.trim() || undefined,
      page: form.page,
      page_size: form.pageSize,
    })
    if (requestId !== instrumentsRequestId) return
    instruments.value = response.items
    listMeta.value = response.meta
    catalogLoaded.value = true
    const current = selected.value && response.items.find(
      (item) => item.market === selected.value?.market && item.code === selected.value?.code,
    )
    const dataBearing = response.items.find((item) => item.bars > 0 && !item.error)
    selected.value = current && current.bars > 0
      ? current
      : dataBearing ?? current ?? response.items[0] ?? null
    if (selected.value?.bars > 0 && !selected.value.error) await loadChart(selected.value)
    else chart.value = null
  } catch (error) {
    if (requestId !== instrumentsRequestId) return
    instruments.value = []
    listMeta.value = null
    selected.value = null
    chart.value = null
    message.value = apiMessage(error)
  } finally {
    if (requestId === instrumentsRequestId) loading.value = false
  }
}

// 页面级持续刷新：数据目录状态每 5 秒、活动任务每 1 秒。
// 同步进行中（syncLoading）时让路：那段时间 waitForJob 已经在盯任务状态，
// 再并发拉一遍 store 状态只会互相踩。组件卸载时 useBackgroundPoll 自停。
useBackgroundPoll(
  () => (syncLoading.value ? Promise.resolve() : loadStoreStatus()),
  { intervalMs: MARKET_STATUS_POLL_INTERVAL_MS },
)
useBackgroundPoll(() => loadActiveMarketJobs(), { intervalMs: ACTIVE_JOB_POLL_INTERVAL_MS })

// 图表与目录都按"最后一次请求才生效"应用响应：连点两只票时，先发的请求可能后到，
// 选中的是 A、图上画的却是 B（StrategyPoolPage 的 canApply 是同一个口径）。
let chartRequestId = 0
let instrumentsRequestId = 0

async function loadChart(instrument = selected.value): Promise<void> {
  const requestId = ++chartRequestId
  if (!instrument) {
    chart.value = null
    return
  }
  if (instrument.error) {
    chart.value = null
    message.value = instrument.error
    return
  }
  chartLoading.value = true
  try {
    const next = await fetchLocalChart({
      market: instrument.market,
      code: instrument.code,
      period: period.value,
      start_date: form.startDate || undefined,
      end_date: form.endDate || undefined,
    })
    if (requestId !== chartRequestId) return
    chart.value = next
    message.value = ''
  } catch (error) {
    if (requestId !== chartRequestId) return
    chart.value = null
    message.value = apiMessage(error)
  } finally {
    if (requestId === chartRequestId) chartLoading.value = false
  }
}

async function selectInstrument(instrument: LocalInstrument): Promise<void> {
  selected.value = instrument
  await loadChart(instrument)
}

async function setPeriod(next: MarketChartPeriod): Promise<void> {
  if (period.value === next) return
  period.value = next
  await loadChart()
}

function toggleMa(key: string): void {
  maKeys.value = maKeys.value.includes(key)
    ? maKeys.value.filter((current) => current !== key)
    : [...maKeys.value, key]
}

async function changePage(next: number): Promise<void> {
  if (!listMeta.value || next < 1 || next > listMeta.value.pages || next === form.page) return
  form.page = next
  await loadInstruments()
}

function formatPrice(value: number | null): string {
  return formatNumber(value)
}

onMounted(async () => {
  await loadActiveMarketJobs()
  await loadStoreStatus()
  if (!marketJob.value) await loadInstruments()
  await detectVipdocPath(true)
})

function instrumentLabel(type: LocalInstrument['instrument_type']): string {
  return { stock: '股票' }[type]
}
</script>

<template>
  <div class="screen-page market-data-page" data-testid="market-data-page">
    <AppTopbar />

    <main class="content-shell">
      <PageIntro
        eyebrow="LOCAL MARKET DATA / DUCKDB"
        title="本地行情"
        copy="行情由本机 DuckDB 保存，可在本页面一键同步最新日线。"
        note-label="数据边界"
        guide-key="market-data"
      >
        <strong>本地 DuckDB</strong>
        <small>同步结果直接写入本机数据库，图表、筛选和回测读取同一份数据</small>
      </PageIntro>

      <div v-if="message" class="notice error" role="alert" data-testid="market-data-message">{{ message }}</div>
      <div v-if="syncNotice" class="notice" data-testid="market-sync-notice">{{ syncNotice }}</div>

      <section class="panel market-store-panel" data-testid="market-store-panel">
        <div class="panel-heading"><div><span class="section-kicker">00 / DATA STORE</span><h2>行情数据仓库</h2></div><span class="required-hint">{{ syncLabel || 'DuckDB' }}</span></div>
        <div class="store-status-grid" data-testid="market-store-status">
          <span>标的 {{ storeStatus?.instrument_count ?? 0 }}</span>
          <span>K线 {{ storeStatus?.bar_count ?? 0 }}</span>
          <span>区间 {{ storeStatus?.data_start ?? '—' }} → {{ storeStatus?.data_end ?? '—' }}</span>
        </div>
        <div class="store-meta" data-testid="market-store-meta">
          <span>数据库 {{ storeStatus?.database_path ?? '—' }}</span>
          <span data-testid="market-last-online-sync">最近在线更新 {{ formatChinaTimestamp(storeStatus?.last_online_sync_at) }}</span>
          <span data-testid="market-calendar-meta">最近交易日历 {{ storeStatus?.market_calendar_latest_date ?? '—' }} · {{ storeStatus?.market_calendar_source ?? '未获取' }}<template v-if="storeStatus?.market_calendar_updated_at"> · 表更新于 {{ formatChinaTimestamp(storeStatus.market_calendar_updated_at) }}</template></span>
          <span v-if="calendarBehindBarsText" class="scope-warning" data-testid="market-calendar-behind">{{ calendarBehindBarsText }}</span>
          <span v-if="storeStatus?.last_auto_sync_check_date">自动同步最近判定「不缺数据」 {{ storeStatus.last_auto_sync_check_date }} · 截至交易日 {{ storeStatus.last_auto_sync_check_session ?? '—' }}（真补了数据的日子不更新这里）</span>
          <span v-if="lastFullSyncText" data-testid="last-full-market-sync">{{ lastFullSyncText }}</span>
        </div>
        <div class="market-sync-form">
          <div class="scope-config" data-testid="market-sync-scope">
            <div><label for="market-sync-universe">同步市场</label><select id="market-sync-universe" v-model="form.syncUniverse" data-testid="market-sync-universe"><option value="all">沪深全部</option><option value="sh">仅上海</option><option value="sz">仅深圳</option></select></div>
            <div class="scope-options"><span>证券类型</span><label v-for="option in INSTRUMENT_TYPE_OPTIONS" :key="option.key"><input type="checkbox" :data-testid="`market-sync-type-${option.key}`" :checked="form.syncInstrumentTypes.includes(option.key)" @change="toggleInstrumentType(option.key)">{{ option.label }}</label></div>
            <div class="scope-options"><span>板块</span><label v-for="option in BOARD_OPTIONS" :key="option.key"><input type="checkbox" :data-testid="`market-sync-board-${option.key}`" :checked="form.syncBoards.includes(option.key)" @change="toggleBoard(option.key)">{{ option.label }}</label></div>
            <div class="scope-options" data-testid="market-sync-force-row"><span>重复下载</span><label><input id="market-sync-force" v-model="form.syncForce" data-testid="market-sync-force" type="checkbox" :disabled="syncLoading">强制重新下载本交易日</label></div>
            <small class="scope-helper" data-testid="market-sync-helper">板块未勾选表示不限制；类型和板块同时选择时取交集。在线同步下载不复权日线（raw）；除权跳空由指标计算时按分红送转事件校正，成交价始终是真实价。<b>同步行情</b>按每只标的自己的覆盖决定拉哪段：库里没有这只标的就从 2015-01-01 全量下载；已有行情就补到最新交易日，并把它最近 10 天一起重下用作对照——逐值比过没变就不写库，也不会因此重算指标，只有上游改写了近期数据才会覆盖。已经拉到本交易日收盘的那几只当天不再问一次；同一范围在同一个交易日里第二次点「同步行情」会直接回「本地已覆盖 X 收盘，无需下载」，什么都不下载（要重下就勾「强制重新下载」，怀疑上游改写了近期数据时也用它）。<b>删除并重下全部历史</b>先删掉所选范围内标的的全部日线（含本地导入）再整段重下，只有它能修正已经写歪的中间那段，耗时最久。</small>
          </div>
          <div class="market-sync-actions">
            <button class="primary-button" data-testid="market-sync-online" type="button" :disabled="syncLoading" @click="syncOnlineData">同步行情</button>
            <button class="secondary-button" data-testid="market-online-refresh" type="button" :disabled="syncLoading" @click="refreshOnlineData">{{ syncLoading ? '重下中…' : '删除并重下全部历史' }}</button>
          </div>
          <div class="local-import-row">
            <div class="local-import-field">
              <label for="market-local-import-path">通达信行情目录<TermHint term="vipdoc" /></label>
              <input id="market-local-import-path" v-model="form.vipdocPath" data-testid="market-local-import-path" type="text" :placeholder="form.vipdocDetected.length ? form.vipdocDetected[0].path : '/data/vipdoc（容器内路径）'" :list="'vipdoc-candidates'">
              <datalist id="vipdoc-candidates">
                <option v-for="item in form.vipdocDetected" :key="item.path" :value="item.path">{{ item.file_count }} 个日线文件 · {{ item.markets.join('/') }}</option>
              </datalist>
            </div>
            <button class="secondary-button" data-testid="market-local-import-detect" type="button" :disabled="form.vipdocDetecting" @click="detectVipdocPath(false)">{{ form.vipdocDetecting ? '检测中…' : '自动检测' }}</button>
            <button class="secondary-button" data-testid="market-local-import" type="button" :disabled="syncLoading" @click="importLocalDayFiles">导入通达信行情</button>
            <button class="text-button" data-testid="market-local-import-undo" type="button" :disabled="undoingLocalImport" @click="undoLocalImport">{{ undoingLocalImport ? '清理中…' : '撤销本地导入' }}</button>
          </div>
          <div class="scope-options local-import-scope" data-testid="market-local-import-scope"><span>导入范围</span><label v-for="option in INSTRUMENT_TYPE_OPTIONS" :key="option.key"><input type="checkbox" :data-testid="`market-local-type-${option.key}`" :checked="form.localInstrumentTypes.includes(option.key)" @change="toggleLocalInstrumentType(option.key)">{{ option.label }}</label><label v-for="option in BOARD_OPTIONS" :key="`board-${option.key}`"><input type="checkbox" :data-testid="`market-local-board-${option.key}`" :checked="form.localBoards.includes(option.key)" @change="toggleLocalBoard(option.key)">{{ option.label }}</label><small>全部不勾 = 导入目录里所有股票（默认）。目录里的非股票文件会自动跳过。</small></div>
          <small v-if="form.vipdocDetectNote" class="scope-helper" data-testid="market-local-import-note">{{ form.vipdocDetectNote }}</small>
          <small v-if="!form.vipdocDetected.length && form.vipdocMountHint" class="scope-warning">{{ form.vipdocMountHint }}在宿主机上执行：<code>TDX_VIPDOC_PATH=/你的/通达信/vipdoc docker compose -f docker-compose.yml -f docker-compose.tdx.yml up -d</code>，然后重建镜像 <code>docker compose build backend</code>。</small>
          <small class="scope-helper">先在通达信里「盘后数据下载」下载日线，再点「自动检测」让后端找目录<template v-if="form.vipdocHostPrefix">；也可以直接填电脑上 {{ form.vipdocHostPrefix }} 开头的路径</template>。导入会按同一只股票同一天覆盖库里已有的行情，目录里没有的日期保持原样。注意：通达信日线是不复权原始价（raw），与在线同步同口径，两者可以直接互相覆盖；指标计算时会按分红送转事件做除权校正，成交价则用这里的真实价。</small>
          </div>
          <div v-if="marketJob" class="sync-progress" data-testid="market-data-progress">
          <div class="sync-progress-heading"><span>{{ marketJobTitle }}</span><div class="sync-progress-actions"><button v-if="marketJob.status === 'queued' || marketJob.status === 'running'" class="text-button" data-testid="market-sync-cancel" type="button" :disabled="marketJob.cancel_requested || marketJob.phase === 'cancelling'" @click="cancelActiveMarketJob(marketJob.job_id)">{{ marketJob.cancel_requested || marketJob.phase === 'cancelling' ? '取消中…' : '取消任务' }}</button><strong>{{ Math.round(marketJob.progress * 100) }}%</strong></div></div>
          <div class="progress-track sync-progress-track" :class="{ indeterminate: marketProgressIndeterminate(marketJob) }"><span :style="{ width: `${Math.round(marketJob.progress * 100)}%` }"></span></div>
          <div class="sync-progress-stats"><span>{{ marketProgressSummary(marketJob) }}</span><span>worker <strong>{{ progressWorkerCount(marketJob) }}</strong></span><span :class="{ negative: marketJob.errors > 0 }">错误 <strong>{{ marketJob.errors }}</strong></span></div>
          <p v-if="trackingLostText(marketJob.job_id)" class="scope-warning" data-testid="market-sync-stalled">{{ trackingLostText(marketJob.job_id) }}</p>
          <div v-if="isLiveMarketJob(marketJob)" class="sync-stage-status" data-testid="market-sync-stage"><strong>{{ progressPhaseText(marketJob) || '任务进行中' }}</strong><span>{{ progressStageNote(marketJob) }}</span></div>
          <div v-if="currentSyncDetail(marketJob)" class="sync-current-instrument" data-testid="market-sync-current">
            <div class="sync-current-heading"><span>{{ currentSyncDetailLabel(marketJob) }}</span><strong>{{ currentSyncInstrumentText(marketJob) }}</strong><span>{{ currentSyncStatusText(marketJob) }}</span></div>
            <div class="sync-current-meta"><span>同步区间 <strong>{{ currentSyncRangeText(marketJob) }}</strong></span><span v-if="currentSyncBarCount(marketJob) !== null">{{ currentSyncBarCount(marketJob) }} 根日线</span><span v-if="currentSyncRetry(marketJob) > 0">重试 {{ currentSyncRetry(marketJob) }} 次</span><span v-if="currentSyncWorkerText(marketJob)">{{ currentSyncWorkerText(marketJob) }}</span></div>
          </div>
          <div v-if="localImportSummary" class="sync-result-stats" data-testid="local-import-result">
            <span>发现文件 {{ localImportSummary.discovered }}</span>
            <span>符合范围 {{ localImportSummary.selected }}</span>
            <span>范围外跳过 {{ localImportSummary.filtered }}</span>
            <span>写入标的 {{ localImportSummary.instruments }}</span>
            <span>写入 K 线 {{ localImportSummary.bars }}</span>
            <span>覆盖旧数据 {{ localImportSummary.replaced }}</span>
            <span :class="{ negative: localImportSummary.skipped > 0 }">跳过文件 {{ localImportSummary.skipped }}</span>
            <span>覆盖区间 {{ localImportSummary.range }}</span>
            <span v-for="reason in localImportSummary.reasons" :key="reason" class="negative">{{ reason }}</span>
          </div>
          <div v-else-if="marketJob.result" class="sync-result-stats">
            <span v-if="marketJob.result.execution_mode === 'process_pool_staging'">执行模式 {{ marketJob.result.worker_count ?? 0 }} 进程分片</span>
            <span v-if="marketJob.result.staged_rows !== undefined">暂存 {{ marketJob.result.staged_rows }} 根</span>
            <span v-if="marketJob.result.merged_rows !== undefined">合并 {{ marketJob.result.merged_rows }} 根</span>
            <span>证券列表 {{ marketJob.result.listed_instruments ?? marketJob.result.total_candidates ?? marketJob.result.total_targets ?? 0 }}</span><span>新增标的 {{ marketJob.result.new_instruments ?? 0 }}</span><span>更新标的 {{ marketJob.result.updated_files ?? marketJob.result.completed ?? 0 }}</span><span>写入 K 线 {{ marketJob.result.written_bars ?? marketJob.result.rows ?? 0 }}</span><span>无变化 {{ marketJob.result.unchanged_files ?? marketJob.result.skipped ?? 0 }}</span><span v-if="marketJob.result.actual_start || marketJob.result.actual_end">实际覆盖 {{ marketJob.result.actual_start ?? '—' }} → {{ marketJob.result.actual_end ?? '—' }}</span><span v-if="marketJob.result.skipped_current" :title="`本交易日（${marketJob.result.covered_session ?? '—'}）已经有已收盘日线的标的，一只请求都没发`">已覆盖本交易日 {{ marketJob.result.skipped_current }} 只</span>
            <span v-if="catalogCountsText">分类 {{ catalogCountsText }}</span>
            <span :class="{ negative: (marketJob.result.errors ?? 0) > 0 }">失败 {{ marketJob.result.errors ?? 0 }}</span>
          </div>
        </div>
      </section>

      <section class="panel market-health-panel" data-testid="market-health-panel">
        <div class="panel-heading">
          <div><span class="section-kicker">01 / HEALTH CHECK</span><h2>行情健康检查</h2></div>
          <span class="required-hint">{{ healthChecking ? '检查中…' : healthSummary ? `${healthSummary.bad} / ${healthSummary.total} 项有问题` : '只读体检' }}</span>
        </div>
        <div class="market-sync-actions">
          <button class="primary-button" data-testid="market-health-check" type="button" :disabled="healthChecking || healthRepairing" @click="runHealthCheck">{{ healthChecking ? '检查中…' : '开始检查' }}</button>
          <button class="secondary-button" data-testid="market-health-repair" type="button" :disabled="healthChecking || healthRepairing || !healthIssues.length" @click="repairAllHealth">{{ healthRepairing ? '修复中…' : healthIssues.length ? `一键修复 ${healthIssues.length} 项` : '一键修复' }}</button>
        </div>
        <small class="scope-helper">检查项：行情缺口、连续缺失段、价格结构错误、异常跳变、零成交日、数据陈旧。只出报告不改数据；「一键修复」是删掉命中标的的全部历史再全量重下覆盖。</small>

        <div v-if="healthSummary" class="store-status-grid" data-testid="market-health-summary">
          <span>标的 {{ healthSummary.instruments }}</span>
          <span>交易日 {{ healthSummary.days }}</span>
          <span>耗时 {{ (healthReport?.duration_seconds ?? 0).toFixed(1) }} 秒</span>
        </div>

        <div v-if="healthChecking || isLiveMarketJob(healthJob)" class="sync-progress" data-testid="market-health-progress">
          <div class="sync-progress-heading"><span>{{ healthJobTitle }}</span><div class="sync-progress-actions"><button class="text-button" data-testid="market-health-cancel" type="button" :disabled="!healthJob || healthJob.cancel_requested" @click="cancelActiveMarketJob(healthJob?.job_id ?? '')">{{ healthJob?.cancel_requested ? '取消中…' : '取消任务' }}</button><strong>本阶段 {{ Math.round((healthJob?.progress ?? 0) * 100) }}%</strong></div></div>
          <div class="progress-track sync-progress-track" :class="{ indeterminate: marketProgressIndeterminate(healthJob) }"><span :style="{ width: `${Math.round((healthJob?.progress ?? 0) * 100)}%` }"></span></div>
          <div v-if="healthJob" class="sync-progress-stats"><span>{{ marketProgressSummary(healthJob) }}</span><span>worker <strong>{{ progressWorkerCount(healthJob) }}</strong></span><span :class="{ negative: healthJob.errors > 0 }">错误 <strong>{{ healthJob.errors }}</strong></span></div>
          <p v-if="trackingLostText(healthJob?.job_id)" class="scope-warning" data-testid="market-health-stalled">{{ trackingLostText(healthJob?.job_id) }}</p>
          <div v-if="healthJob" class="sync-stage-status" data-testid="market-health-stage"><strong>{{ progressPhaseText(healthJob) || '正在扫描行情库（只读，不改数据）…' }}</strong><span>{{ progressStageNote(healthJob) }}</span></div>
          <div v-if="currentSyncDetail(healthJob)" class="sync-current-instrument" data-testid="market-health-current">
            <div class="sync-current-heading"><span>{{ currentSyncDetailLabel(healthJob) }}</span><strong>{{ currentSyncInstrumentText(healthJob) }}</strong><span>{{ currentSyncStatusText(healthJob) }}</span></div>
            <div class="sync-current-meta"><span>同步区间 <strong>{{ currentSyncRangeText(healthJob) }}</strong></span><span v-if="currentSyncWorkerText(healthJob)">{{ currentSyncWorkerText(healthJob) }}</span></div>
          </div>
        </div>

        <div v-if="healthRepaired" class="store-meta" data-testid="market-health-repair-result">
          <span>删除 {{ healthRepaired.deleted_bars ?? 0 }} 根</span>
          <span>重写 {{ healthRepaired.written_bars ?? 0 }} 根</span>
          <span :class="{ negative: (healthRepaired.errors ?? 0) > 0 }">失败 {{ healthRepaired.errors ?? 0 }}</span>
          <span v-if="healthRepaired.remaining" data-testid="market-health-remaining">名单共 {{ healthRepaired.flagged_total ?? 0 }} 只，本轮修的是第 {{ (healthRepaired.offset ?? 0) + 1 }} ~ {{ (healthRepaired.offset ?? 0) + (healthRepaired.requested_codes ?? 0) }} 只，还剩 {{ healthRepaired.remaining }} 只没轮到</span>
          <span v-else-if="healthRepaired.requested_codes">本轮 {{ healthRepaired.requested_codes }} 只都已重下（名单共 {{ healthRepaired.flagged_total ?? healthRepaired.requested_codes }} 只）</span>
          <span v-if="repairNoChangeText(healthRepaired)" class="scope-warning" data-testid="market-health-no-change">{{ repairNoChangeText(healthRepaired) }}</span>
          <span v-if="healthRepaired.message">{{ healthRepaired.message }}</span>
          <span v-if="healthRepaired.empty_after_repair?.length" class="negative" data-testid="market-health-empty-codes">重下后仍无数据 {{ healthRepaired.empty_after_repair.join('、') }}</span>
          <button v-if="healthRepaired.remaining" class="secondary-button" data-testid="market-health-repair-next" type="button" :disabled="healthChecking || healthRepairing" @click="continueHealthRepair">从第 {{ nextRepairOffset(healthRepaired) + 1 }} 只继续修 {{ Math.min(healthRepaired.remaining, healthRepaired.requested_codes ?? 0) }} 只</button>
        </div>

        <div v-if="!healthReport" class="inline-empty" data-testid="market-health-empty">{{ healthChecking ? '正在扫描行情库，全库约 600 万行时需要十秒左右…' : '还没有检查结果，点「开始检查」跑一次只读体检。' }}</div>
        <div v-else class="table-wrap">
          <table data-testid="market-health-table">
            <caption class="sr-only">行情健康检查结果</caption>
            <thead>
              <tr>
                <th>检查项</th>
                <th>状态</th>
                <th>影响标的</th>
                <th>次数</th>
                <th>证据示例</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="check in healthReport.checks" :key="check.key" :data-testid="`market-health-row-${check.key}`">
                <td>{{ check.label }}<TermHint v-if="check.key === 'gap'" term="dataGap" /><TermHint v-else-if="check.key === 'price_jump'" term="priceJump" /><TermHint v-else-if="check.key === 'stale'" term="staleBars" /><TermHint v-else-if="check.key === 'missing_action'" term="missingAction" /></td>
                <td :class="{ negative: check.severity === 'error', positive: check.severity === 'ok' }">{{ severityText(check.severity) }}</td>
                <td>{{ check.affected_instruments }}</td>
                <td>{{ check.occurrences }}</td>
                <td class="health-evidence">
                  <span v-if="!check.examples.length">—</span>
                  <span v-for="example in check.examples.slice(0, 2)" :key="`${example.market}-${example.code}-${example.trade_date}`" class="health-evidence-line">{{ example.code }}<template v-if="example.name"> {{ example.name }}</template> · {{ example.detail }}</span>
                </td>
                <td><button v-if="check.repairable && check.affected_instruments > 0" class="text-button" :data-testid="`market-health-fix-${check.key}`" type="button" :disabled="healthRepairing" @click="repairHealth([check.key])">修复</button><span v-else>—</span></td>
              </tr>
            </tbody>
          </table>
        </div>
        <small v-if="healthReport && !healthIssues.length" class="scope-helper" data-testid="market-health-clean">六项检查都没有命中，行情库当前看不出洞。</small>
        <small v-if="healthMessage" class="scope-warning" data-testid="market-health-message">{{ healthMessage }}</small>
      </section>

      <div class="market-data-grid">
        <section class="panel local-instruments-panel" data-testid="local-instruments">
          <div class="panel-heading"><div><span class="section-kicker">02 / INSTRUMENTS</span><h2>本地品种</h2></div><span class="required-hint">{{ listMeta?.total ?? 0 }} 个标的</span></div>
          <form class="market-filter-form" @submit.prevent="form.page = 1; loadInstruments()">
            <div class="compact-fields">
              <div><label for="market-scope">市场</label><select id="market-scope" v-model="form.market" data-testid="market-scope"><option value="all">沪深全部品种</option><option value="SH">仅上海</option><option value="SZ">仅深圳</option></select></div>
              <div><label for="market-keyword">搜索代码</label><input id="market-keyword" v-model="form.keyword" data-testid="market-keyword" type="search" placeholder="例如 600000"></div>
            </div>
            <button class="secondary-button" data-testid="market-refresh" type="submit" :disabled="loading">{{ loading ? '读取中…' : '刷新本地列表' }}</button>
          </form>

          <div v-if="!instruments.length" class="inline-empty" data-testid="market-instruments-empty">{{ loading ? '正在读取 DuckDB 行情…' : '数据库中暂无行情，请先在上方同步在线数据。' }}</div>
          <div v-else class="local-instrument-list">
            <button v-for="instrument in instruments" :key="`${instrument.market}-${instrument.code}`" class="local-instrument" :class="{ selected: selected?.market === instrument.market && selected?.code === instrument.code }" :data-testid="`instrument-${instrument.market}-${instrument.code}`" type="button" @click="selectInstrument(instrument)">
              <span class="instrument-topline"><span class="instrument-identity"><strong>{{ instrument.code }}</strong><span class="instrument-name">{{ instrument.name || '—' }}</span></span><span class="market-pill" :class="instrument.market.toLowerCase()">{{ instrument.market }}</span></span>
              <span class="instrument-meta"><span>{{ instrumentLabel(instrument.instrument_type) }} · {{ instrument.bars ? `${instrument.bars} 根` : instrument.error ?? '无数据' }}</span><span>{{ instrument.last_close === null ? '—' : `¥${formatPrice(instrument.last_close)}` }}</span></span>
              <span class="instrument-range">{{ instrument.data_start ?? '—' }} → {{ instrument.data_end ?? '—' }}</span>
            </button>
          </div>
          <div v-if="listMeta && listMeta.pages > 1" class="pagination" data-testid="market-pagination"><button type="button" :disabled="form.page <= 1" @click="changePage(form.page - 1)">上一页</button><span>{{ form.page }} / {{ listMeta.pages }}</span><button type="button" :disabled="form.page >= listMeta.pages" @click="changePage(form.page + 1)">下一页</button></div>
        </section>

        <section class="panel market-chart-panel" data-testid="market-chart-panel">
          <div class="panel-heading"><div><span class="section-kicker">03 / CHART</span><h2>{{ selectedTitle }}</h2></div><span v-if="chart" class="required-hint">{{ chart.data_start }} → {{ chart.data_end }}</span></div>
          <template v-if="selected">
            <div class="chart-toolbar">
              <div class="period-switch" role="group" aria-label="K线周期"><button v-for="item in [{ key: 'daily', label: '日线' }, { key: 'monthly', label: '月线' }, { key: 'yearly', label: '年线' }]" :key="item.key" :class="{ active: period === item.key }" :data-testid="`chart-period-${item.key}`" type="button" @click="setPeriod(item.key as MarketChartPeriod)">{{ item.label }}</button></div>
              <div class="chart-date-range"><label for="market-chart-start">开始</label><input id="market-chart-start" v-model="form.startDate" data-testid="market-chart-start" type="date"><label for="market-chart-end">结束</label><input id="market-chart-end" v-model="form.endDate" data-testid="market-chart-end" type="date"><button class="secondary-button" data-testid="market-chart-refresh" type="button" :disabled="chartLoading" @click="loadChart()">刷新</button></div>
            </div>
            <div class="indicator-switches"><span>显示：</span><label v-for="option in MA_OPTIONS" :key="option.key"><input :data-testid="`toggle-${option.key}`" type="checkbox" :checked="maKeys.includes(option.key)" @change="toggleMa(option.key)">{{ option.label }}</label><label><input v-model="showRsi" data-testid="toggle-rsi" type="checkbox">RSI14</label><label><input v-model="showMacd" data-testid="toggle-macd" type="checkbox">MACD</label></div>
            <div v-if="chartLoading" class="chart-loading" data-testid="market-chart-loading">正在生成图表…</div>
            <MarketCandlestickChart v-else-if="chart" :candles="chart.candles" :ma-keys="maKeys" :formula-panels="chart.formula_panels ?? []" :period="period" :show-macd="showMacd" :show-rsi="showRsi" />
            <div v-else class="market-chart-empty" data-testid="market-chart-empty">暂无该品种在所选区间内的行情数据。</div>
            <div v-if="chart" class="chart-data-summary" data-testid="market-chart-summary"><span>本地日线 {{ chart.total_daily_bars }} 根</span><span>当前 {{ chart.bars }} 根{{ period === 'daily' ? '日' : period === 'monthly' ? '月' : '年' }}线</span><span>可用区间 {{ chart.available_data_start }} → {{ chart.available_data_end }}</span></div>
          </template>
          <div v-else class="market-chart-empty" data-testid="market-chart-empty">请选择品种生成 K 线图。</div>
        </section>
      </div>
    </main>
  </div>
</template>
