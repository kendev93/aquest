<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'

import { FormulaScreenApiError } from '../api/formulaScreen'
import {
  cancelMarketDataJob,
  createLocalImport,
  createAkShareBackfill,
  createOnlineSync,
  fetchActiveMarketDataJobs,
  fetchMarketDataCapabilities,
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
  MarketDataCapabilities,
  MarketDataProvider,
  MarketChartPeriod,
  MarketSyncJobState,
} from '../types'
import MarketCandlestickChart from '../components/MarketCandlestickChart.vue'

const MA_OPTIONS = [
  { key: 'ma5', label: 'MA5' },
  { key: 'ma10', label: 'MA10' },
  { key: 'ma20', label: 'MA20' },
  { key: 'ma60', label: 'MA60' },
]

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

const form = reactive({
  vipdocPath: '/data/vipdoc',
  syncUniverse: 'all' as 'all' | 'sh' | 'sz',
  syncProvider: 'akshare' as MarketDataProvider,
  syncInstrumentTypes: [] as InstrumentType[],
  syncBoards: [] as InstrumentBoard[],
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
const providerCapabilities = ref<MarketDataCapabilities | null>(null)
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
  instrument_discovery: `正在刷新${syncProviderLabel.value}证券列表…`,
  local_import: '正在导入本地行情…',
  online_bars: '正在同步在线日线…',
}[marketJob.value?.phase ?? ''] ?? null))

const syncProviderLabel = computed(() => {
  const provider = marketJob.value?.metadata?.provider ?? form.syncProvider
  return provider === 'tdx' ? ' TDX ' : ' AKShare '
})

const marketJobTitle = computed(() => {
  if (syncLoading.value) return syncPhaseText.value || syncLabel.value || '后台任务进行中…'
  if (marketJob.value?.status === 'queued') return '等待后台任务…'
  if (marketJob.value?.status === 'running') return '后台任务进行中…'
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
  const name = typeof detail.name === 'string' && detail.name ? detail.name : ''
  const identity = [market, code].filter(Boolean).join(' ')
  return [identity, name].filter(Boolean).join(' · ') || '—'
})

const currentSyncRangeText = computed(() => {
  const metadata = marketJob.value?.metadata
  if (!metadata) return '—'
  const start = typeof metadata.range_start === 'string' ? metadata.range_start : ''
  const end = typeof metadata.range_end === 'string' ? metadata.range_end : ''
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

const currentSyncProviderText = computed(() => {
  const provider = marketJob.value?.metadata?.provider ?? form.syncProvider
  return provider === 'tdx' ? 'TDX' : 'AKShare'
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

const currentSyncHistorySource = computed(() => {
  const value = marketJob.value?.metadata?.history_source
  return typeof value === 'string' ? value : ''
})

const historySourcesText = computed(() => {
  const sources = marketJob.value?.result?.history_sources
  if (!sources) return ''
  return Object.entries(sources).map(([source, count]) => `${source} × ${count}`).join('、')
})

const discoveryWaiting = computed(() => Boolean(
  marketJob.value?.status === 'running'
  && marketJob.value.phase === 'instrument_discovery'
  && !currentSyncDetail.value,
))

const discoveryProviderText = computed(() => (
  currentSyncProviderText.value === 'TDX' ? '等待 TDX 返回证券列表…' : '等待 AKShare 返回证券列表…'
))

const marketProgressIndeterminate = computed(() => Boolean(
  marketJob.value?.status === 'running'
  && marketJob.value.total_candidates === 0,
))

function activeJobInstrumentText(job: MarketSyncJobState): string {
  const value = job.metadata?.current_instrument
  if (!value || typeof value !== 'object' || Array.isArray(value)) return '等待第一个标的…'
  const detail = value as Record<string, unknown>
  const identity = [detail.market, detail.code]
    .filter((item): item is string => typeof item === 'string' && item.length > 0)
    .join(' ')
  const name = typeof detail.name === 'string' && detail.name ? detail.name : ''
  return [identity, name].filter(Boolean).join(' · ') || '处理中…'
}

function activeJobRangeText(job: MarketSyncJobState): string {
  const start = typeof job.metadata?.range_start === 'string' ? job.metadata.range_start : ''
  const end = typeof job.metadata?.range_end === 'string' ? job.metadata.range_end : ''
  if (start && end) return `${start} → ${end}`
  return typeof job.metadata?.range_label === 'string' ? job.metadata.range_label : '读取日期范围…'
}

function activeJobProviderText(job: MarketSyncJobState): string {
  if (job.metadata?.provider === 'tdx') return 'TDX'
  if (job.metadata?.provider === 'akshare') return 'AKShare'
  return job.description?.includes('本地') ? '本地 vipdoc' : '—'
}

function activeJobDescription(job: MarketSyncJobState): string {
  return job.description || (job.metadata?.provider === 'tdx' ? 'TDX 行情同步' : 'AKShare 行情同步')
}

function apiMessage(error: unknown): string {
  if (error instanceof FormulaScreenApiError) return error.message
  return '行情数据读取失败，请检查数据导入状态后重试。'
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

function toggleInstrumentType(type: InstrumentType): void {
  form.syncInstrumentTypes = form.syncInstrumentTypes.includes(type)
    ? form.syncInstrumentTypes.filter((item) => item !== type)
    : [...form.syncInstrumentTypes, type]
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
    const pendingJob = status.startup_import_job_id
      ? { id: status.startup_import_job_id, label: '正在自动导入本地行情…' }
      : status.auto_sync_job_id
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
    metadata: { provider: form.syncProvider },
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

async function loadProviderCapabilities(): Promise<void> {
  try {
    providerCapabilities.value = await fetchMarketDataCapabilities()
  } catch {
    // Capability discovery is informational; the sync endpoint remains the source of truth.
    providerCapabilities.value = null
  }
}

async function importLocalData(): Promise<void> {
  const vipdocPath = form.vipdocPath.trim()
  if (!vipdocPath) {
    message.value = '请输入通达信 vipdoc 数据目录。'
    return
  }
  syncLoading.value = true
  syncLabel.value = '正在导入本地行情…'
  message.value = ''
  try {
    const job = await createLocalImport({
      vipdoc_path: vipdocPath,
      universe: form.syncUniverse,
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

async function syncOnlineData(): Promise<void> {
  syncLoading.value = true
  syncLabel.value = '正在同步在线行情…'
  message.value = ''
  try {
    const job = await createOnlineSync({
      provider: form.syncProvider,
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

async function backfillAkShareData(): Promise<void> {
  syncLoading.value = true
  syncLabel.value = '正在进行 AKShare 全量回填…'
  message.value = ''
  try {
    const job = await createAkShareBackfill({
      universe: form.syncUniverse,
      ...(form.syncBoards.length > 0 ? { boards: [...form.syncBoards] } : {}),
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
  await Promise.all([loadProviderCapabilities(), loadStoreStatus()])
  await loadActiveMarketJobs()
  if (!marketJob.value) await loadInstruments()
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
  return { stock: '股票', fund: '基金/ETF', index: '指数', bond: '债券' }[type]
}
</script>

<template>
  <div class="screen-page market-data-page" data-testid="market-data-page">
    <header class="topbar">
      <a class="brand" href="/indicator-lab" aria-label="指标实验室首页"><img class="brand-mark" src="/indicator-lab-mark.png" alt="" aria-hidden="true" width="34" height="34"><span><strong>Indicator Lab</strong><small>指标实验室</small></span></a>
      <nav aria-label="主导航">
        <a class="nav-link" href="/indicator-lab">公式选股</a>
        <a class="nav-link" href="/backtest">单股回测</a>
        <a class="nav-link" href="/portfolio-backtest">组合回测</a>
        <a class="nav-link" href="/strategy-lab">策略实验室</a>
        <a class="nav-link" href="/strategy-fitness">策略适配性</a>
        <a class="nav-link active" href="/market-data" aria-current="page">本地行情</a>
      </nav>
      <div class="topbar-status"><span class="status-dot" aria-hidden="true"></span> 本地数据模式</div>
    </header>

    <main class="content-shell">
      <section class="page-intro">
        <div><p class="eyebrow">LOCAL MARKET DATA / DUCKDB</p><h1>本地行情</h1><p class="intro-copy">行情统一存储在本地 DuckDB，默认使用 AKShare 更新股票日线，也可从通达信 vipdoc 导入。</p></div>
        <div class="intro-note"><span class="note-label">数据边界</span><strong>本地 DuckDB</strong><small>源文件只读；图表、筛选和回测统一查询数据库</small></div>
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
          <span>最近本地导入 {{ storeStatus?.last_local_import_at ?? '—' }}</span>
          <span>最近在线更新 {{ storeStatus?.last_online_sync_at ?? '—' }} · {{ storeStatus?.last_online_provider ?? '—' }}</span>
        </div>
        <div v-if="storeStatus?.auto_sync_enabled !== undefined" class="auto-sync-status" :class="{ active: autoSyncActive }" data-testid="auto-sync-status">
          <span class="auto-sync-dot" aria-hidden="true"></span>
          <div><strong>{{ autoSyncStatusText }}</strong><small>{{ autoSyncScheduleText }}</small></div>
        </div>
        <div class="market-sync-form">
          <label for="market-vipdoc-path">通达信 vipdoc 导入源</label>
          <input id="market-vipdoc-path" v-model="form.vipdocPath" data-testid="market-vipdoc-path" type="text" placeholder="/data/vipdoc">
          <div class="scope-config" data-testid="market-sync-scope">
            <div><label for="market-sync-universe">同步市场</label><select id="market-sync-universe" v-model="form.syncUniverse" data-testid="market-sync-universe"><option value="all">沪深全部</option><option value="sh">仅上海</option><option value="sz">仅深圳</option></select></div>
            <div class="scope-options"><span>证券类型</span><label v-for="option in INSTRUMENT_TYPE_OPTIONS" :key="option.key"><input type="checkbox" :data-testid="`market-sync-type-${option.key}`" :checked="form.syncInstrumentTypes.includes(option.key)" @change="toggleInstrumentType(option.key)">{{ option.label }}</label></div>
            <div class="scope-options"><span>板块</span><label v-for="option in BOARD_OPTIONS" :key="option.key"><input type="checkbox" :data-testid="`market-sync-board-${option.key}`" :checked="form.syncBoards.includes(option.key)" @change="toggleBoard(option.key)">{{ option.label }}</label></div>
            <div class="scope-options provider-options"><span>行情源</span><label><input v-model="form.syncProvider" type="radio" value="akshare" data-testid="market-provider-akshare">AKShare（默认）</label><label><input v-model="form.syncProvider" type="radio" value="tdx" data-testid="market-provider-tdx">TDX（遗留）</label></div>
            <small class="scope-helper">类型或板块未勾选表示不限制；两者同时选择时取交集。一键同步只刷新证券列表并补充在线日线，不读取 vipdoc；只有“导入本地行情”才会读取 .day 文件。主板、科创板和创业板股票默认使用 AKShare；全量补齐按钮会重新读取 AKShare 可提供的完整历史。</small>
            <small v-if="providerCapabilities?.providers.akshare?.history_sources?.length" class="scope-helper">AKShare 备用接口：{{ providerCapabilities.providers.akshare.history_sources.join(' → ') }}</small>
            <small v-if="providerCapabilities?.providers.akshare && !providerCapabilities.providers.akshare.available" class="scope-warning">{{ providerCapabilities.providers.akshare.error || 'AKShare 当前不可用，请重新构建 Docker 镜像。' }}</small>
          </div>
          <div class="market-sync-actions">
            <button class="primary-button" data-testid="market-import-local" type="button" :disabled="syncLoading" @click="importLocalData">{{ syncLoading ? '同步进行中…' : '导入本地行情' }}</button>
            <button class="secondary-button" data-testid="market-sync-online" type="button" :disabled="syncLoading" @click="syncOnlineData">一键同步行情</button>
            <button v-if="form.syncProvider === 'akshare'" class="secondary-button" data-testid="market-akshare-backfill" type="button" :disabled="syncLoading" @click="backfillAkShareData">{{ syncLoading ? '回填进行中…' : 'AKShare 全量补齐' }}</button>
          </div>
          </div>
          <div v-if="activeMarketJobs.length" class="active-sync-jobs" data-testid="market-active-jobs">
            <div class="active-sync-heading"><strong>正在运行的行情任务</strong><span>{{ activeMarketJobs.length }} 个任务</span></div>
            <article v-for="job in activeMarketJobs" :key="job.job_id" class="active-sync-job" :data-testid="`market-active-job-${job.job_id}`">
              <div class="active-sync-job-heading"><strong>{{ activeJobDescription(job) }}</strong><span>{{ activeJobProviderText(job) }} · {{ job.phase === 'cancelling' || job.cancel_requested ? '取消中' : '运行中' }}</span><b>{{ Math.round(job.progress * 100) }}%</b><button class="text-button" type="button" :disabled="job.cancel_requested || job.phase === 'cancelling'" @click="cancelActiveMarketJob(job.job_id)">{{ job.cancel_requested || job.phase === 'cancelling' ? '取消中…' : '取消任务' }}</button></div>
              <div class="progress-track active-sync-track"><span :style="{ width: `${Math.round(job.progress * 100)}%` }"></span></div>
              <div class="active-sync-job-meta"><span>已处理 {{ job.total_scanned }} / {{ job.total_candidates }}</span><span>worker {{ job.worker_count ?? 8 }}</span><span>当前 {{ activeJobInstrumentText(job) }}</span><span>{{ activeJobRangeText(job) }}</span></div>
            </article>
          </div>
          <div v-if="marketJob" class="sync-progress" data-testid="market-data-progress">
          <div class="sync-progress-heading"><span>{{ marketJobTitle }}</span><strong>{{ Math.round(marketJob.progress * 100) }}%</strong></div>
          <div class="progress-track sync-progress-track" :class="{ indeterminate: marketProgressIndeterminate }"><span :style="{ width: `${Math.round(marketJob.progress * 100)}%` }"></span></div>
          <div class="sync-progress-stats"><span>已处理 <strong>{{ marketJob.total_scanned }}</strong> / {{ marketJob.total_candidates }}</span><span>worker <strong>{{ marketJob.worker_count ?? 8 }}</strong></span><span :class="{ negative: marketJob.errors > 0 }">错误 <strong>{{ marketJob.errors }}</strong></span></div>
          <div v-if="discoveryWaiting" class="sync-stage-status" data-testid="market-sync-stage"><strong>正在刷新证券列表</strong><span>{{ discoveryProviderText }}</span></div>
          <div v-if="currentSyncDetail" class="sync-current-instrument" data-testid="market-sync-current">
            <div class="sync-current-heading"><span>{{ currentSyncDetailLabel }}</span><strong>{{ currentSyncInstrumentText }}</strong><span>{{ currentSyncProviderText }} · {{ currentSyncStatusText }}</span></div>
            <div class="sync-current-meta"><span>同步区间 <strong>{{ currentSyncRangeText }}</strong></span><span v-if="currentSyncBarCount !== null">{{ currentSyncBarCount }} 根日线</span><span v-if="currentSyncRetry > 0">重试 {{ currentSyncRetry }} 次</span></div>
            <div v-if="currentSyncHistorySource" class="sync-current-source">本次接口 <strong>{{ currentSyncHistorySource }}</strong></div>
          </div>
          <div v-if="marketJob.result" class="sync-result-stats">
            <template v-if="marketJob.result.source === 'local'"><span>导入文件 {{ marketJob.result.imported_files ?? 0 }}</span><span>替换标的 {{ marketJob.result.replaced_instruments ?? 0 }}</span><span>写入 K 线 {{ marketJob.result.imported_bars ?? 0 }}</span><span>跳过 {{ marketJob.result.skipped_files ?? 0 }}</span></template>
            <template v-else-if="marketJob.result.source === 'combined'"><span>本地导入 {{ marketJob.result.local_import?.imported_files ?? 0 }} 个文件</span><span>本地写入 {{ marketJob.result.local_import?.imported_bars ?? 0 }} 根</span><span>在线证券列表 {{ marketJob.result.online_sync?.listed_instruments ?? marketJob.result.online_sync?.total_candidates ?? 0 }}</span><span>新增标的 {{ marketJob.result.online_sync?.new_instruments ?? 0 }}</span><span>在线更新 {{ marketJob.result.online_sync?.updated_files ?? 0 }} 个标的</span><span>在线写入 {{ marketJob.result.online_sync?.written_bars ?? 0 }} 根</span></template>
            <template v-else><span>数据源 {{ marketJob.result.provider ?? '—' }}</span><span>证券列表 {{ marketJob.result.listed_instruments ?? marketJob.result.total_candidates ?? 0 }}</span><span>新增标的 {{ marketJob.result.new_instruments ?? 0 }}</span><span>更新标的 {{ marketJob.result.updated_files ?? 0 }}</span><span>写入 K 线 {{ marketJob.result.written_bars ?? 0 }}</span><span>无变化 {{ marketJob.result.unchanged_files ?? 0 }}</span></template>
            <span v-if="historySourcesText">日线接口 {{ historySourcesText }}</span><span :class="{ negative: marketJob.result.errors > 0 }">失败 {{ marketJob.result.errors }}</span>
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

          <div v-if="!instruments.length" class="inline-empty" data-testid="market-instruments-empty">{{ loading ? '正在读取 DuckDB 行情…' : '数据库中暂无行情，请先导入本地数据。' }}</div>
          <div v-else class="local-instrument-list">
            <button v-for="instrument in instruments" :key="`${instrument.market}-${instrument.code}`" class="local-instrument" :class="{ selected: selected?.market === instrument.market && selected?.code === instrument.code }" :data-testid="`instrument-${instrument.market}-${instrument.code}`" type="button" @click="selectInstrument(instrument)">
              <span class="instrument-topline"><span class="instrument-identity"><strong>{{ instrument.code }}</strong><span class="instrument-name">{{ instrument.name || '—' }}</span></span><span class="market-pill" :class="instrument.market.toLowerCase()">{{ instrument.market }}</span></span>
              <span class="instrument-meta"><span>{{ instrumentLabel(instrument.instrument_type) }} · {{ instrument.bars ? `${instrument.bars} 根` : instrument.error ?? '无数据' }}</span><span>{{ instrument.provider || '—' }} · {{ instrument.last_close === null ? '—' : `¥${formatPrice(instrument.last_close)}` }}</span></span>
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
