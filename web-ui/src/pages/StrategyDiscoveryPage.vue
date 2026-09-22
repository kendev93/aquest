<script setup lang="ts">
import PageGuide from '../components/PageGuide.vue'
import { computed, onMounted, reactive, ref, watch } from 'vue'

import { FormulaScreenApiError } from '../api/formulaScreen'
import { fetchActiveJobs } from '../api/jobs'
import {
  createStrategyDiscovery,
  createStrategyDiscoveryRefinement,
  cancelStrategyDiscovery,
  deleteStrategyDiscoveryHistory,
  fetchStrategyDiscoveryCapabilities,
  fetchStrategyDiscoveryHistory,
  getStrategyDiscovery,
  getStrategyDiscoveryHistory,
  getStrategyDiscoveryResults,
  createStrategyReplay,
  getStrategyReplay,
  getStrategyReplayResults,
  cancelStrategyReplay,
  getStrategyReplayHistory,
} from '../api/strategyDiscovery'
import DateRangeSelector from '../components/DateRangeSelector.vue'
import RuleGroupEditor from '../components/RuleGroupEditor.vue'
import StrategyReplayPanel from '../components/StrategyReplayPanel.vue'
import { formatChinaTimestamp } from '../utils/time'
import type {
  ConditionOperator,
  RuleGroupPayload,
  ScreenCondition,
  StrategyDiscoveryCandidate,
  StrategyDiscoveryJobState,
  StrategyDiscoveryPayload,
  StrategyDiscoveryResult,
  StrategyDiscoveryLeaderboard,
  StrategyReplayCandidate,
  StrategyReplayJobState,
  StrategyReplayResult,
  InstrumentBoard,
  InstrumentType,
  DateRangeSelection,
  StrategyDiscoveryDateMode,
  Universe,
  ValueDefinition,
} from '../types'

const APPLY_STORAGE_KEY = 'indicator-lab.strategy-discovery.apply'
const APPLY_METADATA_KEY = 'indicator-lab.strategy-discovery.apply-meta'

const INSTRUMENT_TYPE_OPTIONS: { key: InstrumentType; label: string }[] = [
  { key: 'stock', label: '股票' },
]

const BOARD_OPTIONS: { key: InstrumentBoard; label: string }[] = [
  { key: 'main', label: '主板' },
  { key: 'star', label: '科创板' },
  { key: 'chinext', label: '创业板' },
]

function emptyRule(): RuleGroupPayload {
  return { signals: [], conditions: [], combine_mode: 'all', minimum_matches: null }
}

const form = reactive({
  universe: 'all' as Universe,
  universeFile: '',
  instrumentTypes: [] as InstrumentType[],
  boards: [] as InstrumentBoard[],
  dateMode: 'recent5' as StrategyDiscoveryDateMode,
  startDate: '',
  endDate: '',
  requiredBuyRule: emptyRule(),
  requiredSellRule: emptyRule(),
  maxPositions: 5,
  searchLevel: 'low',
})
const capabilities = ref<Record<string, unknown> | null>(null)
const history = ref<Array<Record<string, unknown>>>([])
const job = ref<StrategyDiscoveryJobState | null>(null)
const result = ref<StrategyDiscoveryResult | null>(null)
const activeBoard = ref<'sharpe' | 'return'>('sharpe')
const selectedCandidate = ref<StrategyDiscoveryCandidate | null>(null)
const replayJob = ref<StrategyReplayJobState | null>(null)
const replayResult = ref<StrategyReplayResult | null>(null)
const replayLoading = ref(false)
const refinementJob = ref<StrategyDiscoveryJobState | null>(null)
const refinementLoading = ref(false)
const replayPeriod = ref<'overall' | 'ytd' | 'one_year' | 'two_year' | 'annual'>('overall')
const selectedReplayCandidateHash = ref<string | null>(null)
const loading = ref(false)
const message = ref('')
const errors = ref<Record<string, string>>({})

const boards = computed(() => activeBoard.value === 'sharpe'
  ? result.value?.results_by_sharpe ?? []
  : result.value?.results_by_return ?? [])
const experimentId = computed(() => result.value?.history_id ?? job.value?.job_id ?? null)
const availableValues = computed<ValueDefinition[]>(() => {
  const raw = capabilities.value?.indicators
  if (!Array.isArray(raw)) return []
  return raw.flatMap((item) => {
    if (!item || typeof item !== 'object') return []
    const value = item as Record<string, unknown>
    if (
      value.rankable === false
      || typeof value.id !== 'string'
      || typeof value.display_name !== 'string'
    ) return []
    return [{
      id: value.id,
      display_name: value.display_name,
      description: typeof value.description === 'string' ? value.description : '',
    }]
  })
})
const requiredBuySlots = computed(() => Math.max(0, 3 - form.requiredBuyRule.conditions.length))
const requiredSellSlots = computed(() => Math.max(0, 3 - form.requiredSellRule.conditions.length))
const canSubmit = computed(() => (
  !loading.value
  && Number.isInteger(form.maxPositions)
  && form.maxPositions >= 1
  && form.maxPositions <= 100
  && capabilities.value?.enabled !== false
))
const dateRange = computed<DateRangeSelection>({
  get: () => ({ mode: form.dateMode, startDate: form.startDate, endDate: form.endDate }),
  set: (value) => {
    form.dateMode = value.mode
    form.startDate = value.startDate
    form.endDate = value.endDate
  },
})

function validate(): Record<string, string> {
  const next: Record<string, string> = {}
  if (!Number.isInteger(form.maxPositions) || form.maxPositions < 1 || form.maxPositions > 100) {
    next.maxPositions = '持仓数量必须是 1 到 100 的整数。'
  }
  if (form.universe === 'custom' && !form.universeFile.trim()) next.universeFile = '请输入自定义品种列表文件。'
  if (form.dateMode === 'custom') {
    if (!form.startDate) next.startDate = '请选择开始日期。'
    if (!form.endDate) next.endDate = '请选择结束日期。'
    if (form.startDate && form.endDate && form.startDate > form.endDate) {
      next.dateRange = '开始日期不能晚于结束日期。'
    } else if (
      form.startDate
      && form.endDate
      && dateDistance(form.startDate, form.endDate) < 2
    ) {
      next.dateRange = '策略实验时间范围至少需要 3 天。'
    }
  }
  return next
}

function dateDistance(start: string, end: string): number {
  const startTime = Date.parse(`${start}T00:00:00Z`)
  const endTime = Date.parse(`${end}T00:00:00Z`)
  if (!Number.isFinite(startTime) || !Number.isFinite(endTime)) return -1
  return Math.floor((endTime - startTime) / (24 * 60 * 60 * 1000))
}

function buildPayload(): StrategyDiscoveryPayload {
  const trialCount = form.searchLevel === 'high' ? 3000 : form.searchLevel === 'medium' ? 2000 : 1000
  return {
    universe: form.universe,
    universe_file: form.universe === 'custom' ? form.universeFile.trim() : null,
    date_mode: form.dateMode,
    ...(form.dateMode === 'custom'
      ? { start_date: form.startDate, end_date: form.endDate }
      : {}),
    ...(form.instrumentTypes.length > 0 ? { instrument_types: [...form.instrumentTypes] } : {}),
    ...(form.boards.length > 0 ? { boards: [...form.boards] } : {}),
    ...(form.requiredBuyRule.conditions.length > 0
      ? { required_buy_conditions: form.requiredBuyRule.conditions.map((condition) => ({ ...condition })) }
      : {}),
    ...(form.requiredSellRule.conditions.length > 0
      ? { required_sell_conditions: form.requiredSellRule.conditions.map((condition) => ({ ...condition })) }
      : {}),
    max_positions: form.maxPositions,
    max_trials: trialCount,
    timeout_seconds: 7200,
    validation_candidates: 50,
    holdout_candidates: 20,
    min_trades: 5,
    max_drawdown: 0.3,
    max_complexity: 12,
    multifidelity_enabled: true,
  }
}

function refinementMaxTrials(): number {
  return form.searchLevel === 'high' ? 3000 : form.searchLevel === 'medium' ? 2000 : 1000
}

function apiMessage(error: unknown): string {
  if (error instanceof FormulaScreenApiError) return error.message
  if (error instanceof Error && error.message) return `策略实验失败：${error.message}`
  return '策略实验失败，请检查本地 DuckDB 数据和配置后重试。'
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

function setRequiredRule(side: 'buy' | 'sell', rule: RuleGroupPayload): void {
  const normalized: RuleGroupPayload = {
    signals: [],
    conditions: rule.conditions.map((condition) => ({ ...condition })),
    combine_mode: 'all',
    minimum_matches: null,
  }
  if (side === 'buy') form.requiredBuyRule = normalized
  else form.requiredSellRule = normalized
}

async function submit(): Promise<void> {
  errors.value = validate()
  message.value = ''
  if (Object.values(errors.value).some(Boolean)) return
  loading.value = true
  result.value = null
  selectedCandidate.value = null
  replayJob.value = null
  replayResult.value = null
  selectedReplayCandidateHash.value = null
  refinementJob.value = null
  try {
    const created = await createStrategyDiscovery(buildPayload())
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
    await pollDiscoveryJob(created.job_id, 'discovery')
  } catch (error) {
    message.value = apiMessage(error)
  } finally {
    loading.value = false
  }
}

type DiscoveryJobKind = 'discovery' | 'refinement'

async function pollDiscoveryJob(jobId: string, kind: DiscoveryJobKind): Promise<void> {
  for (;;) {
    const state = await getStrategyDiscovery(jobId)
    if (kind === 'refinement') refinementJob.value = state
    else job.value = state
    if (state.status === 'completed') {
      result.value = await getStrategyDiscoveryResults(jobId)
      if (kind === 'refinement') {
        selectedCandidate.value = null
        activeBoard.value = 'sharpe'
        message.value = `二次优化完成：夏普榜 ${result.value.results_by_sharpe.length} 组，收益榜 ${result.value.results_by_return.length} 组${result.value.refinement?.data_changed ? '（行情已更新，已按最新本地行情重新优化）' : ''}。`
      } else {
        message.value = `策略实验完成：夏普榜 ${result.value.results_by_sharpe.length} 组，收益榜 ${result.value.results_by_return.length} 组。`
      }
      await loadHistory()
      return
    }
    if (state.status === 'failed') {
      message.value = kind === 'refinement'
        ? state.error ?? '二次优化失败，请检查父候选和本地行情。'
        : state.error ?? '策略实验任务失败，请检查配置和服务状态后重试。'
      return
    }
    if (state.status === 'cancelled') {
      message.value = kind === 'refinement'
        ? state.error ?? '二次优化已取消。'
        : state.error ?? '策略实验已取消。'
      return
    }
    await new Promise((resolve) => window.setTimeout(resolve, 220))
  }
}

async function startRefinement(candidate: StrategyDiscoveryCandidate): Promise<void> {
  const historyId = experimentId.value
  if (!historyId || refinementLoading.value) return
  refinementLoading.value = true
  refinementJob.value = null
  message.value = ''
  try {
    const created = await createStrategyDiscoveryRefinement({
      parent_history_id: historyId,
      parent_candidate_hash: candidate.candidate_hash,
      refinement_radius: 'medium',
      max_trials: refinementMaxTrials(),
    })
    refinementJob.value = {
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
    await pollDiscoveryJob(created.job_id, 'refinement')
  } catch (error) {
    message.value = apiMessage(error)
  } finally {
    refinementLoading.value = false
  }
}

async function resumeActiveDiscoveryJob(): Promise<void> {
  try {
    const activeJobs = await fetchActiveJobs()
    const active = activeJobs.find((item) => (
      item.description === '策略实验室' || item.description === '策略二次优化'
    ))
    if (active) {
      const kind: DiscoveryJobKind = active.description === '策略二次优化'
        ? 'refinement'
        : 'discovery'
      if (kind === 'refinement') {
        refinementJob.value = active as unknown as StrategyDiscoveryJobState
        refinementLoading.value = true
      } else {
        job.value = active as unknown as StrategyDiscoveryJobState
        loading.value = true
      }
      await pollDiscoveryJob(active.job_id, kind)
      return
    }
    const replay = activeJobs.find((item) => item.description === '策略前十全量复核')
    if (replay) {
      await resumeActiveReplay(replay as unknown as StrategyReplayJobState)
    }
  } catch (error) {
    message.value = apiMessage(error)
  } finally {
    loading.value = false
    refinementLoading.value = false
    replayLoading.value = false
  }
}

async function cancel(): Promise<void> {
  if (!job.value || !loading.value) return
  try {
    await cancelStrategyDiscovery(job.value.job_id)
    message.value = '已请求取消策略实验，正在等待当前候选结束。'
  } catch (error) {
    message.value = apiMessage(error)
  }
}

function replayApiMessage(error: unknown): string {
  if (error instanceof FormulaScreenApiError) return `策略复核失败：${error.message}`
  if (error instanceof Error && error.message) return `策略复核失败：${error.message}`
  return '策略复核失败，请检查本地 DuckDB 数据和候选配置后重试。'
}

function replayProgressValue(state: StrategyReplayJobState | null): number {
  const overall = state?.metadata?.overall_progress
  const value = typeof overall === 'number' ? overall : state?.progress ?? 0
  return Math.min(1, Math.max(0, value))
}

function replayProgressPercent(state: StrategyReplayJobState | null): string {
  return `${Math.round(replayProgressValue(state) * 100)}%`
}

function replayPhase(state: StrategyReplayJobState | null): string {
  if (!state) return '准备中'
  if (state.status === 'completed') return '已完成'
  if (state.status === 'failed') return '复核失败'
  if (state.status === 'cancelled') return '已取消'
  const label = state.metadata?.phase_label
  return typeof label === 'string' && label ? label : state.phase ?? '准备中'
}

function replayCount(state: StrategyReplayJobState | null): number {
  if (state?.status === 'completed') {
    const candidateCount = state.metadata?.candidate_count
    return typeof candidateCount === 'number' ? candidateCount : state.total_candidates
  }
  if (state?.phase !== 'replaying') return 0
  const current = state?.metadata?.phase_current
  if (typeof current === 'number') return current
  return state?.total_scanned ?? 0
}

function replayTotal(state: StrategyReplayJobState | null): number {
  const candidateCount = state?.metadata?.candidate_count
  if (typeof candidateCount === 'number' && candidateCount > 0) return candidateCount
  if (state?.total_candidates && state.total_candidates > 0) return state.total_candidates
  return Math.min(10, boards.value.length)
}

async function pollReplayJob(jobId: string): Promise<void> {
  for (;;) {
    const state = await getStrategyReplay(jobId)
    replayJob.value = state
    if (state.status === 'completed') {
      replayResult.value = await getStrategyReplayResults(jobId)
      message.value = `全量复核完成：${replayResult.value.candidate_count} 个策略。`
      return
    }
    if (state.status === 'failed') {
      message.value = state.error ?? '策略复核失败，请检查本地行情和候选配置后重试。'
      return
    }
    if (state.status === 'cancelled') {
      message.value = state.error ?? '策略复核已取消。'
      return
    }
    await new Promise((resolve) => window.setTimeout(resolve, 220))
  }
}

async function startReplay(): Promise<void> {
  const id = experimentId.value
  if (!id || !boards.value.length || replayLoading.value) return
  replayLoading.value = true
  replayResult.value = null
  selectedReplayCandidateHash.value = null
  message.value = ''
  try {
    const created = await createStrategyReplay(id, activeBoard.value)
    replayJob.value = {
      job_id: created.job_id,
      status: 'queued',
      progress: 0,
      total_candidates: Math.min(10, boards.value.length),
      total_scanned: 0,
      errors: 0,
      skipped: 0,
      error: null,
      result: null,
      phase: 'queued',
      metadata: {},
    }
    await pollReplayJob(created.job_id)
  } catch (error) {
    message.value = replayApiMessage(error)
  } finally {
    replayLoading.value = false
  }
}

async function resumeActiveReplay(active: StrategyReplayJobState): Promise<void> {
  const rawExperimentId = active.metadata?.experiment_id
  if (typeof rawExperimentId === 'string' && rawExperimentId && !result.value) {
    result.value = await getStrategyDiscoveryHistory(rawExperimentId)
  }
  replayJob.value = active
  replayLoading.value = true
  await pollReplayJob(active.job_id)
}

async function cancelReplay(): Promise<void> {
  if (!replayJob.value || !replayLoading.value) return
  try {
    await cancelStrategyReplay(replayJob.value.job_id)
    message.value = '已请求取消全量复核，正在等待当前候选结束。'
  } catch (error) {
    message.value = replayApiMessage(error)
  }
}

async function loadSavedReplay(
  historyId: string,
  leaderboard: StrategyDiscoveryLeaderboard,
): Promise<void> {
  try {
    const saved = await getStrategyReplayHistory(historyId, leaderboard)
    replayResult.value = saved.result
    replayJob.value = {
      job_id: saved.replay_id,
      status: 'completed',
      progress: 1,
      total_candidates: saved.result.candidate_count,
      total_scanned: saved.result.candidate_count,
      errors: Object.keys(saved.result.diagnostics).length,
      skipped: 0,
      error: null,
      result: saved.result,
      phase: 'completed',
      metadata: { saved: true },
    }
  } catch {
    replayResult.value = null
    replayJob.value = null
  }
}

function applyReplayCandidate(candidate: StrategyReplayCandidate): void {
  sessionStorage.setItem(APPLY_STORAGE_KEY, JSON.stringify(candidate.config))
  sessionStorage.setItem(APPLY_METADATA_KEY, JSON.stringify({
    candidate_hash: candidate.candidate_hash,
    data_fingerprint: candidate.data_fingerprint,
    validation_metrics: candidate.overall_metrics,
  }))
  if (import.meta.env.MODE !== 'test') window.location.assign('/portfolio-backtest')
}

function selectReplayCandidate(candidateHash: string): void {
  selectedReplayCandidateHash.value = candidateHash || null
}

function metric(candidate: StrategyDiscoveryCandidate, split: string, key: string): number | null {
  const value = candidate.metrics[split]?.[key]
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function formatPercent(value: number | null): string {
  return value === null ? '—' : `${(value * 100).toFixed(2)}%`
}

function formatNumber(value: number | null): string {
  return value === null ? '—' : value.toFixed(2)
}

function metadataNumber(state: StrategyDiscoveryJobState | null, key: string): number | null {
  const value = state?.metadata?.[key]
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function progressValue(state: StrategyDiscoveryJobState | null): number {
  const overall = metadataNumber(state, 'overall_progress')
  return Math.min(1, Math.max(0, overall ?? state?.progress ?? 0))
}

function progressPercent(state: StrategyDiscoveryJobState | null): string {
  return `${Math.round(progressValue(state) * 100)}%`
}

function progressPhase(state: StrategyDiscoveryJobState | null): string {
  if (!state) return '准备中'
  if (state.status === 'completed') return '已完成'
  if (state.status === 'failed') return '实验失败'
  if (state.status === 'cancelled') return '已取消'
  const label = state.metadata?.phase_label
  if (typeof label === 'string' && label) return label
  return state.phase ?? '准备中'
}

function progressCount(state: StrategyDiscoveryJobState | null, key: string, fallback: number): number {
  return metadataNumber(state, key) ?? fallback
}

function formatDuration(state: StrategyDiscoveryJobState | null): string {
  if (state?.status === 'completed') return '已完成'
  const seconds = metadataNumber(state, 'estimated_seconds_remaining')
  if (seconds === null) return '计算中'
  if (seconds < 60) return `${Math.max(1, Math.ceil(seconds))} 秒`
  const minutes = Math.ceil(seconds / 60)
  if (minutes < 60) return `${minutes} 分钟`
  return `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分钟`
}

function formatHistoryDuration(value: unknown): string {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return '—'
  if (value < 60) return `${value.toFixed(1)} 秒`
  const totalSeconds = Math.round(value)
  const totalMinutes = Math.floor(totalSeconds / 60)
  if (totalMinutes < 60) {
    const seconds = totalSeconds % 60
    return seconds ? `${totalMinutes} 分 ${seconds} 秒` : `${totalMinutes} 分钟`
  }
  const hours = Math.floor(totalMinutes / 60)
  const minutes = totalMinutes % 60
  return minutes ? `${hours} 小时 ${minutes} 分钟` : `${hours} 小时`
}

function stageDuration(state: StrategyDiscoveryJobState | null, key: string): string {
  const raw = state?.metadata?.stage_durations_seconds
  if (!raw || typeof raw !== 'object') return '—'
  return formatHistoryDuration((raw as Record<string, unknown>)[key])
}

function formatHistoryTimestamp(value: unknown): string {
  return formatChinaTimestamp(value)
}

function historyTrialSummary(item: Record<string, unknown>): string {
  const requested = typeof item.requested_trials === 'number'
    ? item.requested_trials
    : typeof item.total_trials === 'number' ? item.total_trials : 0
  const unique = typeof item.unique_evaluated_trials === 'number'
    ? item.unique_evaluated_trials
    : typeof item.total_trials === 'number' ? item.total_trials : 0
  const duplicate = typeof item.duplicate_trials === 'number' ? item.duplicate_trials : 0
  return `${requested} / ${unique} / ${duplicate}`
}

function historyMode(item: Record<string, unknown>): string {
  if (item.mode === 'refinement') {
    const hash = typeof item.parent_candidate_hash === 'string'
      ? item.parent_candidate_hash.slice(0, 10)
      : '—'
    return `二次优化 · ${hash}`
  }
  return '全局搜索'
}

function holdoutStatus(candidate: StrategyDiscoveryCandidate): string {
  const value = candidate.metrics.holdout?.constraints_passed
  return value === true ? '通过' : value === false ? '未通过' : '—'
}

function selectCandidate(candidate: StrategyDiscoveryCandidate): void {
  selectedCandidate.value = candidate
}

function applyCandidate(candidate: StrategyDiscoveryCandidate): void {
  sessionStorage.setItem(APPLY_STORAGE_KEY, JSON.stringify(candidate.config))
  sessionStorage.setItem(APPLY_METADATA_KEY, JSON.stringify({
    candidate_hash: candidate.candidate_hash,
    data_fingerprint: result.value?.data_fingerprint ?? null,
    validation_metrics: candidate.metrics.validation ?? null,
  }))
  if (import.meta.env.MODE !== 'test') window.location.assign('/portfolio-backtest')
}

async function loadHistory(): Promise<void> {
  try {
    history.value = await fetchStrategyDiscoveryHistory()
  } catch (error) {
    message.value = apiMessage(error)
  }
}

async function loadHistoryDetail(historyId: string): Promise<void> {
  try {
    const loaded = await getStrategyDiscoveryHistory(historyId)
    result.value = loaded
    restoreHistoryRequest(loaded.request)
    activeBoard.value = 'sharpe'
    replayPeriod.value = 'overall'
    selectedReplayCandidateHash.value = null
    await loadSavedReplay(historyId, activeBoard.value)
    message.value = `已加载历史实验 ${historyId}。`
  } catch (error) {
    message.value = apiMessage(error)
  }
}

watch(activeBoard, (leaderboard) => {
  selectedReplayCandidateHash.value = null
  replayPeriod.value = 'overall'
  if (result.value?.history_id) {
    void loadSavedReplay(result.value.history_id, leaderboard)
  } else {
    replayResult.value = null
    replayJob.value = null
  }
})

function restoreHistoryRequest(raw: unknown): void {
  if (!raw || typeof raw !== 'object') return
  const request = raw as Record<string, unknown>
  const base = request.base_config && typeof request.base_config === 'object'
    ? request.base_config as Record<string, unknown>
    : {}
  if (typeof base.universe === 'string' && ['all', 'sh', 'sz', 'custom'].includes(base.universe)) form.universe = base.universe as Universe
  if (typeof base.universe_file === 'string') form.universeFile = base.universe_file
  if (base.date_mode === 'recent5' || base.date_mode === 'full') {
    form.dateMode = base.date_mode
    form.startDate = ''
    form.endDate = ''
  } else if (typeof base.start_date === 'string' && typeof base.end_date === 'string') {
    form.dateMode = 'custom'
    form.startDate = base.start_date.slice(0, 10)
    form.endDate = base.end_date.slice(0, 10)
  } else {
    form.dateMode = 'recent5'
    form.startDate = ''
    form.endDate = ''
  }
  if (Array.isArray(base.instrument_types)) {
    form.instrumentTypes = base.instrument_types.filter((item): item is InstrumentType => INSTRUMENT_TYPE_OPTIONS.some((option) => option.key === item))
  }
  if (Array.isArray(base.boards)) {
    form.boards = base.boards.filter((item): item is InstrumentBoard => BOARD_OPTIONS.some((option) => option.key === item))
  }
  form.requiredBuyRule = requiredRuleFromHistory(request.required_buy_conditions)
  form.requiredSellRule = requiredRuleFromHistory(request.required_sell_conditions)
  if (typeof base.max_positions === 'number' && Number.isInteger(base.max_positions)) form.maxPositions = base.max_positions
}

function requiredRuleFromHistory(raw: unknown): RuleGroupPayload {
  if (!Array.isArray(raw)) return emptyRule()
  const operators = new Set<ConditionOperator>([
    'gt', 'gte', 'lt', 'lte', 'eq', 'neq', 'cross_up', 'cross_down',
  ])
  const conditions = raw.flatMap((item): ScreenCondition[] => {
    if (!item || typeof item !== 'object') return []
    const value = item as Record<string, unknown>
    const left = value.left_value
    const operator = value.operator
    const rightValue = value.right_value
    const rightConstant = value.right_constant
    if (
      typeof left !== 'string'
      || !operators.has(operator as ConditionOperator)
      || (rightValue !== null && typeof rightValue !== 'string')
      || (rightConstant !== null && typeof rightConstant !== 'number')
      || (typeof rightValue === 'string') === (typeof rightConstant === 'number')
    ) return []
    return [{
      left_value: left,
      operator: operator as ConditionOperator,
      right_value: typeof rightValue === 'string' ? rightValue : null,
      right_constant: typeof rightConstant === 'number' ? rightConstant : null,
    }]
  })
  return { signals: [], conditions: conditions.slice(0, 3), combine_mode: 'all', minimum_matches: null }
}

async function removeHistory(historyId: string): Promise<void> {
  try {
    await deleteStrategyDiscoveryHistory(historyId)
    if (result.value?.history_id === historyId) {
      result.value = null
      job.value = null
      replayJob.value = null
      replayResult.value = null
      selectedCandidate.value = null
      selectedReplayCandidateHash.value = null
    }
    await loadHistory()
    message.value = '历史实验已删除。'
  } catch (error) {
    message.value = apiMessage(error)
  }
}

onMounted(async () => {
  try {
    capabilities.value = await fetchStrategyDiscoveryCapabilities()
  } catch (error) {
    message.value = apiMessage(error)
  }
  await loadHistory()
  await resumeActiveDiscoveryJob()
})
</script>

<template>
  <div class="screen-page strategy-discovery-page" data-testid="strategy-discovery-page">
    <header class="topbar">
      <a class="brand" href="/indicator-lab" aria-label="指标实验室首页"><img class="brand-mark" src="/indicator-lab-mark.png" alt="" aria-hidden="true" width="34" height="34"><span><strong>Indicator Lab</strong><small>指标实验室</small></span></a>
      <nav aria-label="主导航">
        <a class="nav-link" href="/indicator-lab">公式选股</a>
                <a class="nav-link" href="/portfolio-backtest">组合回测</a>
        <a class="nav-link active" href="/strategy-lab" aria-current="page">策略实验室</a>
        <a class="nav-link" href="/condition-research">买卖条件研究</a>
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
        <div><p class="eyebrow">STRATEGY DISCOVERY / DAILY</p><h1>策略实验室</h1><p class="intro-copy">只设置品种范围和持仓数量，系统默认使用最近 5 年已完成日线寻找排序、买卖和风险控制配置。</p></div>
        <div class="intro-note"><span class="note-label">自动流程</span><strong>寻优 · 验证 · 最终留出</strong><small>真实组合回测裁定候选结果</small></div>
      </section>

      <PageGuide page-key="strategy-lab" />

      <div v-if="message" class="notice" :class="{ error: !message.includes('完成') && !message.includes('加载') && !message.includes('删除') }" role="alert" data-testid="strategy-discovery-message">{{ message }}</div>

      <div class="workspace-grid strategy-discovery-grid">
        <form class="config-panel panel" data-testid="strategy-discovery-config" @submit.prevent="submit">
          <div class="panel-heading"><div><span class="section-kicker">01 / CONFIGURE</span><h2>实验设置</h2></div><span class="required-hint">* 必填</span></div>
          <fieldset class="form-section">
            <legend>数据范围</legend>
            <div class="compact-fields">
              <div><label for="strategy-discovery-universe">品种范围</label><select id="strategy-discovery-universe" v-model="form.universe" data-testid="strategy-discovery-universe"><option value="all">沪深全部品种</option><option value="sh">仅上海</option><option value="sz">仅深圳</option><option value="custom">自定义列表</option></select></div>
              <div><label for="strategy-discovery-positions">持仓数量 N</label><input id="strategy-discovery-positions" v-model.number="form.maxPositions" data-testid="strategy-discovery-positions" type="number" min="1" max="100" step="1"></div>
            </div>
            <DateRangeSelector v-model="dateRange" prefix="strategy-discovery" field-suffix="-date" testid="strategy-discovery-date-range" helper="默认使用本地最新已完成日线向前 5 年；你也可以选择全历史或自定义回测区间。" />
            <p v-if="errors.startDate" class="field-error">{{ errors.startDate }}</p>
            <p v-if="errors.endDate" class="field-error">{{ errors.endDate }}</p>
            <p v-if="errors.dateRange" class="field-error">{{ errors.dateRange }}</p>
            <div class="scope-config inline-scope-config" data-testid="strategy-discovery-scope"><div class="scope-options"><span>证券类型</span><label v-for="option in INSTRUMENT_TYPE_OPTIONS" :key="option.key"><input type="checkbox" :data-testid="`strategy-discovery-scope-type-${option.key}`" :checked="form.instrumentTypes.includes(option.key)" @change="toggleInstrumentType(option.key)">{{ option.label }}</label></div><div class="scope-options"><span>板块</span><label v-for="option in BOARD_OPTIONS" :key="option.key"><input type="checkbox" :data-testid="`strategy-discovery-scope-board-${option.key}`" :checked="form.boards.includes(option.key)" @change="toggleBoard(option.key)">{{ option.label }}</label></div><small class="scope-helper">未勾选表示全部；类型和板块同时选择时取交集。</small></div>
            <div v-if="form.universe === 'custom'" class="portfolio-inline-field"><label for="strategy-discovery-universe-file">品种列表文件</label><input id="strategy-discovery-universe-file" v-model="form.universeFile" data-testid="strategy-discovery-universe-file" type="text"><p v-if="errors.universeFile" class="field-error">{{ errors.universeFile }}</p></div>
            <p v-if="errors.maxPositions" class="field-error">{{ errors.maxPositions }}</p>
          </fieldset>
          <fieldset class="form-section required-conditions-section">
            <legend>用户必选条件（可选）</legend>
            <p class="helper">这里设置的指标条件会强制加入每一个候选；所有必选条件均按 AND 执行。固定条件可以减少无效搜索。</p>
            <RuleGroupEditor
              :model-value="form.requiredBuyRule"
              :values="availableValues"
              title="必选买入条件"
              testid="strategy-required-buy-rule"
              fixed
              :max-conditions="3"
              @update:model-value="setRequiredRule('buy', $event)"
            />
            <RuleGroupEditor
              :model-value="form.requiredSellRule"
              :values="availableValues"
              title="必选卖出条件"
              testid="strategy-required-sell-rule"
              fixed
              :max-conditions="3"
              @update:model-value="setRequiredRule('sell', $event)"
            />
            <p class="scope-helper">自动补充规则名额：买入 {{ requiredBuySlots }} 项 · 卖出 {{ requiredSellSlots }} 项；排序、调仓和风控仍会继续寻优。</p>
          </fieldset>
          <fieldset class="form-section">
            <legend>搜索强度</legend>
            <select v-model="form.searchLevel" data-testid="strategy-discovery-search-level"><option value="low">低 · 1000 组</option><option value="medium">中 · 2000 组</option><option value="high">高 · 3000 组</option></select>
            <p class="helper">固定搜索范围：3 组公式指标的用户可选输出 + MA5/10/20/60、RSI14、MACD、SIGNAL、Histogram；Z 等恒定输出不参与寻优。</p>
            <p class="helper">排序指标、买卖条件、止盈止损、移动止盈和冷却天数均自动搜索。佣金默认万分之一，最低 0.1 元。</p>
            <p class="helper">夏普目标和收益目标分别保留各自的验证候选，验证后独立排序；两个榜单可以包含不同策略。</p>
          </fieldset>
          <div v-if="capabilities" class="discovery-capability-note" data-testid="strategy-discovery-capabilities">指标 {{ capabilities.indicator_count }} 个 · 信号 {{ capabilities.signal_count }} 个 · Optuna {{ capabilities.optuna_available ? '可用' : '不可用' }} · LightGBM {{ capabilities.lightgbm_available ? '可用' : '不可用' }} · 回测 {{ capabilities.evaluation_workers ?? 2 }} 进程<span v-if="capabilities.enabled === false" class="negative"> · 请重新构建 Docker 镜像安装策略寻优依赖</span></div>
          <div class="action-stack"><button class="primary-button" data-testid="strategy-discovery-submit" type="submit" :disabled="!canSubmit"><span v-if="loading" class="spinner" aria-hidden="true"></span>{{ loading ? '实验进行中…' : '开始策略实验' }}</button><button v-if="loading" class="secondary-button" data-testid="strategy-discovery-cancel" type="button" @click="cancel">取消实验</button></div>
          <div v-if="job" class="discovery-progress" data-testid="strategy-discovery-progress"><div class="discovery-progress-heading"><strong>{{ progressPhase(job) }}</strong><span data-testid="strategy-discovery-overall-progress">总体 {{ progressPercent(job) }}</span></div><div class="progress-track"><span :style="{ width: progressPercent(job) }"></span></div><div class="discovery-progress-stats"><span>当前阶段 {{ progressCount(job, 'phase_current', job.total_scanned) }} / {{ progressCount(job, 'phase_total', job.total_candidates) }}</span><span>预计总评估 {{ progressCount(job, 'estimated_total_evaluations', job.total_candidates) }} 次</span><span data-testid="strategy-discovery-remaining">预计剩余 {{ formatDuration(job) }}</span></div><small v-if="job.metadata?.best_sharpe !== undefined">吞吐 {{ formatNumber(metadataNumber(job, 'throughput_per_second')) }} 候选/秒 · 当前最佳夏普 {{ formatNumber(typeof job.metadata.best_sharpe === 'number' ? job.metadata.best_sharpe : null) }} · 收益 {{ formatPercent(typeof job.metadata.best_return === 'number' ? job.metadata.best_return : null) }}</small><small v-if="job.metadata?.requested_trials !== undefined" data-testid="strategy-discovery-budget-stats">请求候选 {{ progressCount(job, 'requested_trials', 0) }} · 已评估 {{ progressCount(job, 'evaluated_trials', job.total_scanned) }} · 唯一 {{ progressCount(job, 'unique_evaluated_trials', job.total_scanned) }} · 重复 {{ progressCount(job, 'duplicate_trials', 0) }}</small><small v-if="job.metadata?.stage_durations_seconds" data-testid="strategy-discovery-stage-durations">快照 {{ stageDuration(job, 'snapshot') }} · 粗筛 {{ stageDuration(job, 'coarse') }} · 中筛 {{ stageDuration(job, 'medium') }} · 完整 {{ stageDuration(job, 'full') }} · 验证 {{ stageDuration(job, 'validation') }} · 留出 {{ stageDuration(job, 'holdout') }}</small></div>
          <div v-if="refinementJob" class="discovery-progress refinement-progress" data-testid="strategy-refinement-progress"><div class="discovery-progress-heading"><strong>{{ progressPhase(refinementJob) }}</strong><span data-testid="strategy-refinement-overall-progress">总体 {{ progressPercent(refinementJob) }}</span></div><div class="progress-track"><span :style="{ width: progressPercent(refinementJob) }"></span></div><div class="discovery-progress-stats"><span>当前阶段 {{ progressCount(refinementJob, 'phase_current', refinementJob.total_scanned) }} / {{ progressCount(refinementJob, 'phase_total', refinementJob.total_candidates) }}</span><span>预计总评估 {{ progressCount(refinementJob, 'estimated_total_evaluations', refinementJob.total_candidates) }} 次</span><span data-testid="strategy-refinement-remaining">预计剩余 {{ formatDuration(refinementJob) }}</span></div><small v-if="refinementJob.metadata?.best_sharpe !== undefined">吞吐 {{ formatNumber(metadataNumber(refinementJob, 'throughput_per_second')) }} 候选/秒 · 当前最佳夏普 {{ formatNumber(typeof refinementJob.metadata.best_sharpe === 'number' ? refinementJob.metadata.best_sharpe : null) }} · 收益 {{ formatPercent(typeof refinementJob.metadata.best_return === 'number' ? refinementJob.metadata.best_return : null) }}</small><small v-if="refinementJob.metadata?.requested_trials !== undefined" data-testid="strategy-refinement-budget-stats">请求候选 {{ progressCount(refinementJob, 'requested_trials', 0) }} · 已评估 {{ progressCount(refinementJob, 'evaluated_trials', refinementJob.total_scanned) }} · 唯一 {{ progressCount(refinementJob, 'unique_evaluated_trials', refinementJob.total_scanned) }} · 重复 {{ progressCount(refinementJob, 'duplicate_trials', 0) }}</small></div>
        </form>

        <section class="results-panel panel" data-testid="strategy-discovery-results" aria-live="polite">
          <div class="panel-heading"><div><span class="section-kicker">02 / LEADERBOARD</span><h2>候选排行榜</h2></div><span v-if="result" class="required-hint">验证期排序</span></div>
          <div v-if="!result" class="empty-state"><div class="empty-icon" aria-hidden="true">/</div><h3>{{ loading ? '正在搜索策略候选…' : '还没有实验结果' }}</h3><p>实验完成后会分别展示夏普榜和收益榜。</p></div>
          <template v-else>
            <div class="formula-mode-tabs" role="tablist" aria-label="策略排行榜"><button type="button" data-testid="strategy-discovery-sharpe-board" :class="{ active: activeBoard === 'sharpe' }" role="tab" :aria-selected="activeBoard === 'sharpe'" @click="activeBoard = 'sharpe'">夏普榜前 10</button><button type="button" data-testid="strategy-discovery-return-board" :class="{ active: activeBoard === 'return' }" role="tab" :aria-selected="activeBoard === 'return'" @click="activeBoard = 'return'">收益榜前 10</button></div>
            <div v-if="boards.length" class="discovery-replay-actions">
              <button class="secondary-button replay-start-button" data-testid="strategy-discovery-replay" type="button" :disabled="replayLoading" @click="startReplay"><span v-if="replayLoading" class="spinner" aria-hidden="true"></span>{{ replayLoading ? '全量复核进行中…' : '一键回测当前榜单前十' }}</button>
              <button v-if="replayLoading" class="secondary-button" data-testid="strategy-discovery-replay-cancel" type="button" @click="cancelReplay">取消全量复核</button>
            </div>
            <div v-if="replayJob" class="discovery-replay-progress" data-testid="strategy-discovery-replay-progress"><div class="discovery-progress-heading"><strong>{{ replayPhase(replayJob) }}</strong><span data-testid="strategy-discovery-replay-percent">{{ replayProgressPercent(replayJob) }}</span></div><div class="progress-track"><span :style="{ width: replayProgressPercent(replayJob) }"></span></div><div class="discovery-progress-stats"><span>已完成 {{ replayCount(replayJob) }} / 总数 {{ replayTotal(replayJob) }}</span><span v-if="replayResult?.finished_at">完成于 {{ formatHistoryTimestamp(replayResult.finished_at) }}</span><span v-if="replayResult?.duration_seconds !== undefined">耗时 {{ formatHistoryDuration(replayResult.duration_seconds) }}</span></div><small v-if="replayJob.errors">有 {{ replayJob.errors }} 个候选未能完成复核，请展开诊断。</small></div>
            <div v-if="!boards.length" class="inline-empty">没有满足当前约束的候选策略。</div>
            <div v-else class="table-wrap"><table><caption class="sr-only">策略候选排行榜</caption><thead><tr><th>#</th><th>策略配置</th><th>验证收益</th><th>验证夏普</th><th>验证回撤</th><th>留出收益</th><th>留出约束</th><th>操作</th></tr></thead><tbody><tr v-for="(candidate, index) in boards" :key="candidate.candidate_hash" :data-testid="`strategy-discovery-row-${candidate.candidate_hash}`"><td>{{ index + 1 }}</td><td><button class="table-link" type="button" @click="selectCandidate(candidate)">{{ candidate.human_readable_rules }}</button></td><td>{{ formatPercent(metric(candidate, 'validation', 'annual_return')) }}</td><td>{{ formatNumber(metric(candidate, 'validation', 'sharpe')) }}</td><td>{{ formatPercent(metric(candidate, 'validation', 'max_drawdown')) }}</td><td>{{ formatPercent(metric(candidate, 'holdout', 'annual_return')) }}</td><td :class="{ positive: holdoutStatus(candidate) === '通过', negative: holdoutStatus(candidate) === '未通过' }">{{ holdoutStatus(candidate) }}</td><td><button class="secondary-button" :data-testid="`strategy-discovery-apply-${candidate.candidate_hash}`" type="button" @click="applyCandidate(candidate)">应用</button><button class="secondary-button" :data-testid="`strategy-discovery-refine-${candidate.candidate_hash}`" type="button" :disabled="refinementLoading" @click="startRefinement(candidate)">{{ refinementLoading ? '优化中…' : '二次优化' }}</button></td></tr></tbody></table></div>
            <div v-if="selectedCandidate" class="discovery-detail" data-testid="strategy-discovery-detail"><div class="section-heading"><div><span class="section-kicker">CANDIDATE DETAIL</span><h3>策略配置</h3></div><div><button class="secondary-button" type="button" :disabled="refinementLoading" @click="startRefinement(selectedCandidate)">{{ refinementLoading ? '优化中…' : '二次优化' }}</button><button class="secondary-button" type="button" @click="selectedCandidate = null">关闭</button></div></div><p>{{ selectedCandidate.human_readable_rules }}</p><div class="metrics-strip"><div><span>验证收益</span><strong>{{ formatPercent(metric(selectedCandidate, 'validation', 'annual_return')) }}</strong></div><div><span>验证夏普</span><strong>{{ formatNumber(metric(selectedCandidate, 'validation', 'sharpe')) }}</strong></div><div><span>最终留出收益</span><strong>{{ formatPercent(metric(selectedCandidate, 'holdout', 'annual_return')) }}</strong></div><div><span>复杂度</span><strong>{{ selectedCandidate.complexity }}</strong></div></div><pre>{{ JSON.stringify(selectedCandidate.config, null, 2) }}</pre></div>
            <StrategyReplayPanel v-if="replayResult" :result="replayResult" :period="replayPeriod" :selected-candidate-hash="selectedReplayCandidateHash" @update:period="replayPeriod = $event" @select="selectReplayCandidate" @apply="applyReplayCandidate" />
          </template>
        </section>
      </div>

      <section class="panel discovery-history-panel" data-testid="strategy-discovery-history">
        <div class="panel-heading"><div><span class="section-kicker">03 / HISTORY</span><h2>历史实验</h2></div><button class="secondary-button" type="button" @click="loadHistory">刷新</button></div>
        <div v-if="!history.length" class="inline-empty">暂无历史实验。</div>
        <div v-else class="table-wrap"><table><thead><tr><th>实验 ID</th><th>类型</th><th>开始时间</th><th>结束时间</th><th>请求/唯一/重复</th><th>耗时</th><th>最佳夏普</th><th>最佳收益</th><th>操作</th></tr></thead><tbody><tr v-for="item in history" :key="String(item.history_id)"><td>{{ item.history_id }}</td><td>{{ historyMode(item) }}</td><td class="muted-code">{{ formatHistoryTimestamp(item.started_at) }}</td><td class="muted-code">{{ formatHistoryTimestamp(item.finished_at) }}</td><td>{{ historyTrialSummary(item) }}</td><td>{{ formatHistoryDuration(item.duration_seconds) }}</td><td>{{ item.best_sharpe ? '可查看' : '—' }}</td><td>{{ item.best_return ? '可查看' : '—' }}</td><td><button class="table-link" type="button" @click="loadHistoryDetail(String(item.history_id))">加载</button><button class="table-link danger-link" type="button" @click="removeHistory(String(item.history_id))">删除</button></td></tr></tbody></table></div>
      </section>
    </main>
  </div>
</template>
