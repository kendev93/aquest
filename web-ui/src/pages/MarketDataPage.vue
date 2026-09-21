<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'

import { FormulaScreenApiError } from '../api/formulaScreen'
import {
  cancelMarketDataJob,
  createLocalImport,
  createOnlineBackfill,
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
  MarketSyncJobState,
} from '../types'
import MarketCandlestickChart from '../components/MarketCandlestickChart.vue'
import TermHint from '../components/TermHint.vue'

const MA_OPTIONS = [
  { key: 'ma5', label: 'MA5' },
  { key: 'ma10', label: 'MA10' },
  { key: 'ma20', label: 'MA20' },
  { key: 'ma60', label: 'MA60' },
]

// 本系统只研究股票：基金（含 ETF）、指数和 B 股既不在线同步也不本地导入，
// 所以不再作为可选项出现。在线通道与本地导入的范围现在是同一套。
const INSTRUMENT_TYPE_OPTIONS: { key: InstrumentType; label: string }[] = [
  { key: 'stock', label: '股票' },
]

const BOARD_OPTIONS: { key: InstrumentBoard; label: string }[] = [
  { key: 'main', label: '主板' },
  { key: 'star', label: '科创板' },
  { key: 'chinext', label: '创业板' },
]


const form = reactive({
  syncUniverse: 'all' as 'all' | 'sh' | 'sz',
  syncInstrumentTypes: ['stock'] as InstrumentType[],
  syncBoards: [] as InstrumentBoard[],
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
const marketJob = ref<MarketSyncJobState | null>(null)
const activeMarketJobs = ref<MarketSyncJobState[]>([])
const MARKET_JOB_POLL_INTERVAL_MS = 500
const MARKET_STATUS_POLL_INTERVAL_MS = 5000
const ACTIVE_JOB_POLL_INTERVAL_MS = 1000
let storeStatusPollTimer: number | null = null
let activeJobPollTimer: number | null = null

const selectedTitle = computed(() => (
  selected.value ? `${selected.value.market} ${selected.value.code}` : '请选择品种'
))

const autoSyncActive = computed(() => Boolean(
  storeStatus.value?.auto_sync_enabled
  && storeStatus.value.auto_sync_job_id
  && marketJob.value?.job_id === storeStatus.value.auto_sync_job_id
  && syncLoading.value,
))

const autoSyncStatusText = computed(() => {
  if (storeStatus.value?.auto_sync_enabled === false) return '自动同步已关闭'
  if (autoSyncActive.value) return syncLabel.value || '正在自动同步最新行情…'
  if (storeStatus.value?.auto_sync_enabled) return '自动同步已开启'
  return '自动同步状态读取中…'
})

const autoSyncScheduleText = computed(() => {
  const seconds = storeStatus.value?.auto_sync_interval_seconds
  if (typeof seconds !== 'number' || !Number.isFinite(seconds)) {
    return '启动后检查；每天至少同步一次'
  }
  return `启动后检查；每 ${Math.max(Math.round(seconds / 60), 1)} 分钟检查一次，当天至少同步一次`
})

const syncPhaseText = computed(() => ({
  planning: '正在规划同步区间…',
  fetching: '正在并发下载在线日线…',
  instrument_discovery: '正在刷新证券列表…',
  online_bars: '正在同步在线日线…',
  merging: '正在合并行情分片…',
  cleanup: '正在清理临时分片…',
}[marketJob.value?.phase ?? ''] ?? null))

const marketJobTitle = computed(() => {
  if (marketJob.value?.status === 'queued' || marketJob.value?.status === 'running') {
    return activeJobDescription(marketJob.value)
  }
  if (syncLoading.value) return syncPhaseText.value || syncLabel.value || '后台任务进行中…'
  return '最近一次任务'
})

const currentSyncDetail = computed<Record<string, unknown> | null>(() => {
  const value = marketJob.value?.metadata?.current_instrument
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  return value as Record<string, unknown>
})

const currentSyncInstrumentText = computed(() => {
  const detail = currentSyncDetail.value
  if (!detail) return '—'
  const market = typeof detail.market === 'string' ? detail.market : ''
  const code = typeof detail.code === 'string' ? detail.code : ''
  const symbol = typeof detail.symbol === 'string' ? detail.symbol : ''
  const name = typeof detail.name === 'string' && detail.name ? detail.name : ''
  const assetType = typeof detail.asset_type === 'string' ? detail.asset_type : ''
  const identity = [market, code].filter(Boolean).join(' ') || symbol
  return [identity, name, assetType].filter(Boolean).join(' · ') || '—'
})

const currentSyncRangeText = computed(() => {
  const metadata = marketJob.value?.metadata
  if (!metadata) return '—'
  const start = typeof metadata.range_start === 'string'
    ? metadata.range_start
    : typeof metadata.requested_start === 'string' ? metadata.requested_start : ''
  const end = typeof metadata.range_end === 'string'
    ? metadata.range_end
    : typeof metadata.requested_end === 'string' ? metadata.requested_end : ''
  if (start && end) return `${start} → ${end}`
  return typeof metadata.range_label === 'string' ? metadata.range_label : '正在读取日期范围…'
})

const currentSyncStatusText = computed(() => {
  const status = marketJob.value?.metadata?.current_status
  return {
    fetching: '正在请求',
    imported: '已导入',
    updated: '已更新',
    unchanged: '无变化',
    error: '失败',
  }[typeof status === 'string' ? status : ''] ?? '处理中'
})

const currentSyncDetailLabel = computed(() => (
  marketJob.value?.status === 'running' ? '当前同步' : '最近同步'
))

const currentSyncBarCount = computed(() => {
  const value = marketJob.value?.metadata?.bar_count
  return typeof value === 'number' && Number.isFinite(value) ? value : null
})

const currentSyncRetry = computed(() => {
  const value = marketJob.value?.metadata?.retry
  return typeof value === 'number' && Number.isFinite(value) ? value : 0
})

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
  return `上次全市场同步：完成 ${completed} · 跳过 ${skipped} · 失败 ${failed}${finishedAt ? ` · ${finishedAt}` : ''}`
})

const discoveryWaiting = computed(() => Boolean(
  marketJob.value?.status === 'running'
  && marketJob.value.phase === 'instrument_discovery'
  && !currentSyncDetail.value,
))

const marketProgressSummary = computed(() => {
  const job = marketJob.value
  if (!job) return '已处理 0 / 0'
  const metadata = job.metadata ?? {}
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
})

const marketProgressIndeterminate = computed(() => Boolean(
  marketJob.value?.status === 'running'
  && marketJob.value.total_candidates === 0,
))

function activeJobDescription(job: MarketSyncJobState): string {
  return job.description || '行情同步'
}

function apiMessage(error: unknown): string {
  if (error instanceof FormulaScreenApiError) return error.message
  return '行情数据读取失败，请检查数据导入状态后重试。'
}

function hasActiveMarketDataJob(): boolean {
  return syncLoading.value
    || activeMarketJobs.value.some((job) => job.status === 'queued' || job.status === 'running')
    || marketJob.value?.status === 'queued'
    || marketJob.value?.status === 'running'
}

function isTransientStoreStatusError(error: unknown): boolean {
  return error instanceof FormulaScreenApiError
    && error.status === 503
    && hasActiveMarketDataJob()
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
    const jobs = (await fetchActiveMarketDataJobs()) ?? []
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
    }
  } catch {
    // Active-job discovery is informational; the individual task poll remains authoritative.
  }
}

async function waitForJob(jobId: string): Promise<void> {
  for (;;) {
    const state = await getMarketDataJob(jobId)
    marketJob.value = state
    updateActiveMarketJob(state)
    if (state.status === 'completed') {
      if (state.result?.errors) {
        message.value = `同步完成，但有 ${state.result.errors} 个错误。`
      } else {
        message.value = ''
      }
      return
    }
    if (state.status === 'cancelled') {
      message.value = '行情同步已取消，已经写入的数据已保留。'
      return
    }
    if (state.status === 'failed') {
      throw new FormulaScreenApiError(state.error ?? '行情同步失败。', 500)
    }
    await new Promise((resolve) => window.setTimeout(resolve, MARKET_JOB_POLL_INTERVAL_MS))
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
  syncLabel.value = '正在同步在线行情…'
  message.value = ''
  try {
    const job = await createOnlineSync({
      universe: form.syncUniverse,
      bars: 800,
      ...syncScopePayload(),
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

async function backfillOnlineData(): Promise<void> {
  syncLoading.value = true
  syncLabel.value = '正在进行全量回填…'
  message.value = ''
  try {
    const job = await createOnlineBackfill({
      universe: form.syncUniverse,
      // 回填曾经只发股票，现在把类型一起传过去。
      ...syncScopePayload(),
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
  loading.value = true
  message.value = ''
  try {
    const response = await fetchLocalInstruments({
      market: form.market,
      keyword: form.keyword.trim() || undefined,
      page: form.page,
      page_size: form.pageSize,
    })
    instruments.value = response.items
    listMeta.value = response.meta
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
    instruments.value = []
    listMeta.value = null
    selected.value = null
    chart.value = null
    message.value = apiMessage(error)
  } finally {
    loading.value = false
  }
}

function scheduleStoreStatusPoll(): void {
  if (storeStatusPollTimer !== null) window.clearTimeout(storeStatusPollTimer)
  storeStatusPollTimer = window.setTimeout(async () => {
    storeStatusPollTimer = null
    if (!syncLoading.value) await loadStoreStatus()
    scheduleStoreStatusPoll()
  }, MARKET_STATUS_POLL_INTERVAL_MS)
}

function scheduleActiveJobPoll(): void {
  if (activeJobPollTimer !== null) window.clearTimeout(activeJobPollTimer)
  activeJobPollTimer = window.setTimeout(async () => {
    activeJobPollTimer = null
    await loadActiveMarketJobs()
    scheduleActiveJobPoll()
  }, ACTIVE_JOB_POLL_INTERVAL_MS)
}

async function loadChart(instrument = selected.value): Promise<void> {
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
    chart.value = await fetchLocalChart({
      market: instrument.market,
      code: instrument.code,
      period: period.value,
      start_date: form.startDate || undefined,
      end_date: form.endDate || undefined,
    })
    message.value = ''
  } catch (error) {
    chart.value = null
    message.value = apiMessage(error)
  } finally {
    chartLoading.value = false
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
  return value === null ? '—' : value.toFixed(2)
}

onMounted(async () => {
  await loadActiveMarketJobs()
  await loadStoreStatus()
  if (!marketJob.value) await loadInstruments()
  await detectVipdocPath(true)
  scheduleStoreStatusPoll()
  scheduleActiveJobPoll()
})

onUnmounted(() => {
  if (storeStatusPollTimer !== null) window.clearTimeout(storeStatusPollTimer)
  if (activeJobPollTimer !== null) window.clearTimeout(activeJobPollTimer)
  storeStatusPollTimer = null
  activeJobPollTimer = null
})

function instrumentLabel(type: LocalInstrument['instrument_type']): string {
  return { stock: '股票' }[type]
}
</script>

<template>
  <div class="screen-page market-data-page" data-testid="market-data-page">
    <header class="topbar">
      <a class="brand" href="/indicator-lab" aria-label="指标实验室首页"><img class="brand-mark" src="/indicator-lab-mark.png" alt="" aria-hidden="true" width="34" height="34"><span><strong>Indicator Lab</strong><small>指标实验室</small></span></a>
      <nav aria-label="主导航">
        <a class="nav-link" href="/indicator-lab">公式选股</a>
                <a class="nav-link" href="/portfolio-backtest">组合回测</a>
        <a class="nav-link" href="/strategy-lab">策略实验室</a>
        <a class="nav-link" href="/condition-research">买卖条件研究</a>
        <a class="nav-link" href="/strategy-fitness">策略适配性</a>
        <a class="nav-link active" href="/market-data" aria-current="page">本地行情</a>
        <a class="nav-link" href="/swing-research">波段研究</a>
      </nav>
      <div class="topbar-status"><span class="status-dot" aria-hidden="true"></span> 本地数据模式</div>
    </header>

    <main class="content-shell">
      <section class="page-intro">
        <div><p class="eyebrow">LOCAL MARKET DATA / DUCKDB</p><h1>本地行情</h1><p class="intro-copy">行情由本机 DuckDB 保存，可在本页面一键同步最新日线。</p></div>
        <div class="intro-note"><span class="note-label">数据边界</span><strong>本地 DuckDB</strong><small>同步结果直接写入本机数据库，图表、筛选和回测读取同一份数据</small></div>
      </section>

      <div v-if="message" class="notice error" role="alert" data-testid="market-data-message">{{ message }}</div>

      <section class="panel market-store-panel" data-testid="market-store-panel">
        <div class="panel-heading"><div><span class="section-kicker">00 / DATA STORE</span><h2>行情数据仓库</h2></div><span class="required-hint">{{ syncLabel || 'DuckDB' }}</span></div>
        <div class="store-status-grid" data-testid="market-store-status">
          <span>标的 {{ storeStatus?.instrument_count ?? 0 }}</span>
          <span>K线 {{ storeStatus?.bar_count ?? 0 }}</span>
          <span>区间 {{ storeStatus?.data_start ?? '—' }} → {{ storeStatus?.data_end ?? '—' }}</span>
        </div>
        <div class="store-meta" data-testid="market-store-meta">
          <span>数据库 {{ storeStatus?.database_path ?? '—' }}</span>
          <span>最近在线更新 {{ storeStatus?.last_online_sync_at ?? '—' }}</span>
          <span v-if="lastFullSyncText" data-testid="last-full-market-sync">{{ lastFullSyncText }}</span>
        </div>
        <div class="market-sync-form">
          <div class="scope-config" data-testid="market-sync-scope">
            <div><label for="market-sync-universe">同步市场</label><select id="market-sync-universe" v-model="form.syncUniverse" data-testid="market-sync-universe"><option value="all">沪深全部</option><option value="sh">仅上海</option><option value="sz">仅深圳</option></select></div>
            <div class="scope-options"><span>证券类型</span><label v-for="option in INSTRUMENT_TYPE_OPTIONS" :key="option.key"><input type="checkbox" :data-testid="`market-sync-type-${option.key}`" :checked="form.syncInstrumentTypes.includes(option.key)" @change="toggleInstrumentType(option.key)">{{ option.label }}</label></div>
            <div class="scope-options"><span>板块</span><label v-for="option in BOARD_OPTIONS" :key="option.key"><input type="checkbox" :data-testid="`market-sync-board-${option.key}`" :checked="form.syncBoards.includes(option.key)" @change="toggleBoard(option.key)">{{ option.label }}</label></div>
            <small class="scope-helper">板块未勾选表示不限制；类型和板块同时选择时取交集。全量补齐按钮只请求缺失历史区间。</small>
          </div>
          <div class="market-sync-actions">
            <button class="primary-button" data-testid="market-sync-online" type="button" :disabled="syncLoading" @click="syncOnlineData">一键同步行情</button>
            <button class="secondary-button" data-testid="market-online-backfill" type="button" :disabled="syncLoading" @click="backfillOnlineData">{{ syncLoading ? '回填进行中…' : '全量补齐' }}</button>
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
          <small class="scope-helper">先在通达信里「盘后数据下载」下载日线，再点「自动检测」让后端找目录<template v-if="form.vipdocHostPrefix">；也可以直接填电脑上 {{ form.vipdocHostPrefix }} 开头的路径</template>。导入会按同一只股票同一天覆盖库里已有的行情，目录里没有的日期保持原样。</small>
          </div>
          <div v-if="marketJob" class="sync-progress" data-testid="market-data-progress">
          <div class="sync-progress-heading"><span>{{ marketJobTitle }}</span><div class="sync-progress-actions"><button v-if="marketJob.status === 'queued' || marketJob.status === 'running'" class="text-button" data-testid="market-sync-cancel" type="button" :disabled="marketJob.cancel_requested || marketJob.phase === 'cancelling'" @click="cancelActiveMarketJob(marketJob.job_id)">{{ marketJob.cancel_requested || marketJob.phase === 'cancelling' ? '取消中…' : '取消任务' }}</button><strong>{{ Math.round(marketJob.progress * 100) }}%</strong></div></div>
          <div class="progress-track sync-progress-track" :class="{ indeterminate: marketProgressIndeterminate }"><span :style="{ width: `${Math.round(marketJob.progress * 100)}%` }"></span></div>
          <div class="sync-progress-stats"><span>{{ marketProgressSummary }}</span><span>worker <strong>{{ typeof marketJob.metadata?.worker_count === 'number' ? marketJob.metadata.worker_count : marketJob.worker_count ?? 8 }}</strong></span><span :class="{ negative: marketJob.errors > 0 }">错误 <strong>{{ marketJob.errors }}</strong></span></div>
          <div v-if="discoveryWaiting" class="sync-stage-status" data-testid="market-sync-stage"><strong>正在刷新证券列表</strong><span>等待在线证券列表返回…</span></div>
          <div v-if="currentSyncDetail" class="sync-current-instrument" data-testid="market-sync-current">
            <div class="sync-current-heading"><span>{{ currentSyncDetailLabel }}</span><strong>{{ currentSyncInstrumentText }}</strong><span>{{ currentSyncStatusText }}</span></div>
            <div class="sync-current-meta"><span>同步区间 <strong>{{ currentSyncRangeText }}</strong></span><span v-if="currentSyncBarCount !== null">{{ currentSyncBarCount }} 根日线</span><span v-if="currentSyncRetry > 0">重试 {{ currentSyncRetry }} 次</span></div>
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
            <span>证券列表 {{ marketJob.result.listed_instruments ?? marketJob.result.total_candidates ?? marketJob.result.total_targets ?? 0 }}</span><span>新增标的 {{ marketJob.result.new_instruments ?? 0 }}</span><span>更新标的 {{ marketJob.result.updated_files ?? marketJob.result.completed ?? 0 }}</span><span>写入 K 线 {{ marketJob.result.written_bars ?? marketJob.result.rows ?? 0 }}</span><span>无变化 {{ marketJob.result.unchanged_files ?? marketJob.result.skipped ?? 0 }}</span><span v-if="marketJob.result.actual_start || marketJob.result.actual_end">实际覆盖 {{ marketJob.result.actual_start ?? '—' }} → {{ marketJob.result.actual_end ?? '—' }}</span>
            <span v-if="catalogCountsText">分类 {{ catalogCountsText }}</span>
            <span :class="{ negative: (marketJob.result.errors ?? 0) > 0 }">失败 {{ marketJob.result.errors ?? 0 }}</span>
          </div>
        </div>
      </section>

      <div class="market-data-grid">
        <section class="panel local-instruments-panel" data-testid="local-instruments">
          <div class="panel-heading"><div><span class="section-kicker">01 / INSTRUMENTS</span><h2>本地品种</h2></div><span class="required-hint">{{ listMeta?.total ?? 0 }} 个标的</span></div>
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
          <div class="panel-heading"><div><span class="section-kicker">02 / CHART</span><h2>{{ selectedTitle }}</h2></div><span v-if="chart" class="required-hint">{{ chart.data_start }} → {{ chart.data_end }}</span></div>
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
