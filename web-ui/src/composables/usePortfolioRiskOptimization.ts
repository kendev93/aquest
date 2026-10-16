/**
 * 退出风控寻优：提交、轮询（瞬时错误递增重试）、取消、接回后台任务、
 * 候选榜展示与应用、寻优历史。
 *
 * 从 PortfolioBacktestPage 拆出来。所有寻优状态由本模块持有；表单的校验、
 * 组装、通用回填留在页面，以依赖注入；「应用候选」要保留当前日期区间并
 * 清掉旧的发现标注，通过 form 只读访问 / clearDiscoveryState 完成。
 */

import { computed, nextTick, ref } from 'vue'
import type { Ref } from 'vue'

import {
  cancelStrategyDiscovery,
  createPortfolioRiskOptimization,
  fetchRiskOptimizationHistory,
  getRiskOptimizationHistory,
  getStrategyDiscovery,
  getStrategyDiscoveryResults,
} from '../api/strategyDiscovery'
import { ApiError } from '../api/client'
import { pollUntil } from './usePolling'
import { adoptActiveJob, useActiveJobResume } from './useActiveJobResume'
import { formatTradeRatio } from '../utils/format'
import type { TradeRatioKey } from '../utils/format'
import { portfolioBaseFingerprint } from '../utils/portfolioFingerprint'
import type {
  PortfolioBacktestPayload,
  PortfolioRiskOptimizationPayload,
  RiskOptimizationHistorySummary,
  StrategyDiscoveryCandidate,
  StrategyDiscoveryJobState,
  StrategyDiscoveryResult,
} from '../types'

/** 应用候选时要保留的表单现场（页面 reactive form 是它的超集）。 */
interface CurrentFormSnapshot {
  dateMode: 'recent5' | 'full' | 'custom'
  startDate: string
  endDate: string
  excludeSt: boolean
  atrPeriod: number
}

export interface PortfolioRiskOptimizationDeps {
  /** 寻优跑的就是当前基础回测配置，提交前先过表单校验。 */
  validate: () => Record<string, string>
  errors: Ref<Record<string, string>>
  buildPayload: () => PortfolioBacktestPayload
  /** 页面顶部的消息条。 */
  message: Ref<string>
  /** 后端错误 → 用户可读文案。 */
  describeError: (error: unknown) => string
  /** 应用候选时读取并保留的表单现场。 */
  form: CurrentFormSnapshot
  /** 候选落地的通用回填。 */
  applyStoredCandidate: (raw: unknown) => void
  /**
   * 应用后写回页面的复现标注（candidate_hash + 指纹 + 验证段指标），
   * 并清掉上一次的比对结果；只有与验证段区间一致时才带标注。
   */
  onCandidateApplied: (
    applied: {
      candidate_hash: string
      data_fingerprint: string | null
      validation_metrics: Record<string, number | string | boolean | null> | null
    } | null,
  ) => void
  /** 「从历史载入」要先还原寻优时的基础配置，走回测历史的还原链路。 */
  applyHistoryRequest: (raw: unknown, notice: string) => Promise<void>
  /** 结果区锚点：「载入候选」成功后滚动过去。 */
  resultsSection: Ref<HTMLElement | null>
}

export function usePortfolioRiskOptimization(deps: PortfolioRiskOptimizationDeps) {
  const riskOptimizationLoading = ref(false)
  const riskOptimizationJob = ref<StrategyDiscoveryJobState | null>(null)
  const riskOptimizationResult = ref<StrategyDiscoveryResult | null>(null)
  const riskOptimizationResultsVisible = ref(true)
  const riskOptimizationBoard = ref<'sharpe' | 'return'>('sharpe')
  const riskOptimizationMaxTrials = ref<1000 | 2000 | 3000>(1000)
  const riskOptimizationBaseFingerprint = ref<string | null>(null)
  const riskOptimizationBaseFingerprintWithoutSt = ref<string | null>(null)
  const riskOptimizationRestoreRequired = ref(false)
  const riskOptimizationHistory = ref<RiskOptimizationHistorySummary[]>([])
  const riskOptimizationHistoryTotal = ref(0)
  const riskOptimizationHistoryLoading = ref(false)
  const riskOptimizationHistoryLoadingId = ref<string | null>(null)
  const riskOptimizationHistoryError = ref('')
  const riskOptimizationHistoryDetail = ref<StrategyDiscoveryResult | null>(null)
  const riskOptimizationHistoryDetailId = ref<string | null>(null)
  const riskOptimizationHistoryDetailLoadingId = ref<string | null>(null)
  const riskOptimizationHistoryDetailError = ref('')

  // 指纹不一致说明提交寻优后表单又被改过：候选不再对应眼前的配置。
  const riskOptimizationStale = computed(() => (
    riskOptimizationRestoreRequired.value
    || (riskOptimizationBaseFingerprint.value !== null
      && riskOptimizationBaseFingerprint.value !== portfolioBaseFingerprint(deps.buildPayload()))
  ))
  const riskOptimizationApplicationBlocked = computed(() => (
    riskOptimizationRestoreRequired.value
    || (riskOptimizationBaseFingerprintWithoutSt.value !== null
      && riskOptimizationBaseFingerprintWithoutSt.value !== portfolioBaseFingerprint(deps.buildPayload(), true))
  ))
  const riskOptimizationCandidates = computed(() => (
    riskOptimizationBoard.value === 'sharpe'
      ? riskOptimizationResult.value?.results_by_sharpe ?? []
      : riskOptimizationResult.value?.results_by_return ?? []
  ))

  function riskMetric(
    candidate: StrategyDiscoveryCandidate,
    split: string,
    key: string,
  ): number | null {
    const value = candidate.metrics[split]?.[key]
    return typeof value === 'number' && Number.isFinite(value) ? value : null
  }

  function riskMetricRatio(
    candidate: StrategyDiscoveryCandidate,
    split: string,
    key: TradeRatioKey,
  ): string {
    return formatTradeRatio(key, {
      value: riskMetric(candidate, split, key),
      totalTrades: riskMetric(candidate, split, 'total_trades'),
      loseTrades: riskMetric(candidate, split, 'lose_trades'),
    })
  }

  function riskOptimizationPhaseLabel(state: StrategyDiscoveryJobState | null): string {
    const phase = state?.phase ?? ''
    if (state?.status === 'cancelled' || phase === 'cancelled') return '风险参数寻优已取消'
    if (state?.cancel_requested || phase === 'cancelling') return '正在取消，等待当前计算批次结束'
    const labels: Record<string, string> = {
      preparing_snapshot: '准备行情快照',
      snapshot_ready: '开始训练段寻优',
      searching: '训练段寻优',
      refining_medium: '训练段复核',
      refining_full: '训练段精筛',
      validation: '验证段筛选与排序',
      holdout: '留出段复核',
      completed: '风险参数寻优完成',
      failed: '风险参数寻优失败',
      cancelled: '风险参数寻优已取消',
    }
    return labels[phase] ?? '风险参数寻优进行中'
  }

  async function startRiskOptimization(): Promise<void> {
    deps.errors.value = deps.validate()
    deps.message.value = ''
    if (Object.values(deps.errors.value).some(Boolean)) return
    const basePayload = deps.buildPayload()
    riskOptimizationBaseFingerprint.value = portfolioBaseFingerprint(basePayload)
    riskOptimizationBaseFingerprintWithoutSt.value = portfolioBaseFingerprint(basePayload, true)
    riskOptimizationRestoreRequired.value = false
    riskOptimizationResult.value = null
    riskOptimizationJob.value = null
    riskOptimizationLoading.value = true
    try {
      const payload: PortfolioRiskOptimizationPayload = {
        ...basePayload,
        max_trials: riskOptimizationMaxTrials.value,
      }
      const created = await createPortfolioRiskOptimization(payload)
      riskOptimizationJob.value = {
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
      await pollRiskOptimization(created.job_id)
    } catch (error) {
      deps.message.value = deps.describeError(error)
    } finally {
      riskOptimizationLoading.value = false
    }
  }

  async function pollRiskOptimization(jobId: string): Promise<void> {
    // 任务是分钟级的，进度读取偶尔抖一下不该把整个等待作废：
    // 瞬时错误按递增间隔重试 5 次，仍失败才向上抛。
    const retryDelays = [250, 500, 1_000, 2_000, 3_000]
    let consecutiveStatusFailures = 0
    const state = await pollUntil(
      async () => {
        const current = await getStrategyDiscovery(jobId)
        riskOptimizationJob.value = current
        if (consecutiveStatusFailures > 0) {
          consecutiveStatusFailures = 0
          deps.message.value = ''
        }
        return current
      },
      {
        intervalMs: 250,
        until: (current) => {
          if (current.status === 'failed' || current.status === 'cancelled') {
            deps.message.value = current.error
              ?? (current.status === 'cancelled' ? '风险参数寻优已取消。' : '风险参数寻优失败。')
            return true
          }
          return current.status === 'completed'
        },
        retryableError: (error, failures) => {
          const transientStatus = error instanceof ApiError
            && [0, 500, 502, 503, 504].includes(error.status)
          if (!transientStatus || failures > retryDelays.length) return null
          const delay = retryDelays[failures - 1] ?? 3_000
          consecutiveStatusFailures = failures
          deps.message.value = `寻优仍在后台运行，进度读取暂时失败，${Math.ceil(delay / 1000)} 秒后重试（${failures}/${retryDelays.length}）。`
          return delay
        },
      },
    )
    if (state.status === 'completed') {
      riskOptimizationResult.value = await getStrategyDiscoveryResults(jobId)
      riskOptimizationResultsVisible.value = true
      riskOptimizationBoard.value = 'sharpe'
      deps.message.value = '风险参数寻优完成：请按验证榜查看候选，并用留出段结果做独立复核。'
      await loadRiskOptimizationHistory()
    }
  }

  // 加载态由 pollRiskOptimization 自己的 catch/finally 收尾，因此不把 setLoading
  // 交给接管流程：接管函数在任务真正跑完前就返回，提前归零会让转圈消失而任务还在跑。
  const { resumeActiveJob: resumeActiveRiskOptimization } = useActiveJobResume({
    candidates: [{
      descriptions: ['组合风险寻优'],
      adopt: (active) => {
        riskOptimizationJob.value = adoptActiveJob<StrategyDiscoveryJobState>(active)
        // 接回后台任务时无从得知提交时的指纹，只能先标记「需从历史还原」，
        // 等用户从历史列表载入后再解锁候选应用。
        riskOptimizationBaseFingerprint.value = null
        riskOptimizationBaseFingerprintWithoutSt.value = null
        riskOptimizationRestoreRequired.value = true
        riskOptimizationLoading.value = true
      },
      poll: async (active) => {
        void pollRiskOptimization(active.job_id).catch((error: unknown) => {
          deps.message.value = deps.describeError(error)
        })
      },
    }],
    reportError: (error) => { deps.message.value = deps.describeError(error) },
  })

  async function loadRiskOptimizationHistory(append = false): Promise<void> {
    if (riskOptimizationHistoryLoading.value) return
    riskOptimizationHistoryLoading.value = true
    riskOptimizationHistoryError.value = ''
    try {
      const offset = append ? riskOptimizationHistory.value.length : 0
      const page = await fetchRiskOptimizationHistory(20, offset)
      riskOptimizationHistory.value = append
        ? [...riskOptimizationHistory.value, ...page.items]
        : page.items
      riskOptimizationHistoryTotal.value = page.total
    } catch (error) {
      riskOptimizationHistoryError.value = deps.describeError(error)
    } finally {
      riskOptimizationHistoryLoading.value = false
    }
  }

  async function toggleRiskOptimizationHistoryDetail(jobId: string): Promise<void> {
    if (riskOptimizationHistoryDetailId.value === jobId) {
      riskOptimizationHistoryDetailId.value = null
      riskOptimizationHistoryDetail.value = null
      riskOptimizationHistoryDetailError.value = ''
      return
    }
    riskOptimizationHistoryDetailLoadingId.value = jobId
    riskOptimizationHistoryDetailError.value = ''
    riskOptimizationHistoryDetailId.value = null
    riskOptimizationHistoryDetail.value = null
    try {
      riskOptimizationHistoryDetail.value = await getRiskOptimizationHistory(jobId)
      riskOptimizationHistoryDetailId.value = jobId
    } catch (error) {
      riskOptimizationHistoryDetailError.value = deps.describeError(error)
    } finally {
      riskOptimizationHistoryDetailLoadingId.value = null
    }
  }

  function closeRiskOptimizationHistoryDetail(): void {
    riskOptimizationHistoryDetailId.value = null
    riskOptimizationHistoryDetail.value = null
    riskOptimizationHistoryDetailError.value = ''
  }

  async function loadRiskOptimizationHistoryDetail(jobId: string): Promise<void> {
    riskOptimizationHistoryLoadingId.value = jobId
    try {
      const savedResult = await getRiskOptimizationHistory(jobId)
      const savedRequest = savedResult.request
      const baseConfig = savedRequest && typeof savedRequest === 'object'
        ? (savedRequest as Record<string, unknown>).base_config
        : null
      if (!baseConfig || typeof baseConfig !== 'object') {
        throw new ApiError('这条历史记录缺少寻优时的基础配置，无法恢复候选。', 422)
      }
      await deps.applyHistoryRequest(
        baseConfig,
        `已恢复风险寻优 ${jobId} 的配置和候选，可继续查看或应用。`,
      )
      riskOptimizationResult.value = savedResult
      riskOptimizationResultsVisible.value = true
      riskOptimizationBoard.value = 'sharpe'
      riskOptimizationBaseFingerprint.value = portfolioBaseFingerprint(deps.buildPayload())
      riskOptimizationBaseFingerprintWithoutSt.value = portfolioBaseFingerprint(deps.buildPayload(), true)
      riskOptimizationRestoreRequired.value = false
      riskOptimizationJob.value = null
      deps.onCandidateApplied(null)
      await nextTick()
      deps.resultsSection.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    } catch (error) {
      deps.message.value = deps.describeError(error)
    } finally {
      riskOptimizationHistoryLoadingId.value = null
    }
  }

  async function cancelRiskOptimization(): Promise<void> {
    const active = riskOptimizationJob.value
    if (!active || active.cancel_requested) return
    try {
      const response = await cancelStrategyDiscovery(active.job_id)
      if (response.cancel_requested) {
        riskOptimizationJob.value = {
          ...active,
          cancel_requested: true,
          phase: 'cancelling',
        }
      }
      deps.message.value = '已请求取消风险参数寻优，正在等待任务停止。'
    } catch (error) {
      deps.message.value = deps.describeError(error)
    }
  }

  function applyRiskOptimizationCandidate(candidate: StrategyDiscoveryCandidate): void {
    if (!riskOptimizationResult.value) return
    if (riskOptimizationApplicationBlocked.value) {
      deps.message.value = riskOptimizationRestoreRequired.value
        ? '请先从历史列表载入寻优时的基础配置，再应用候选。'
        : '买卖规则、排序或其他基础设置已变化，当前不能应用该候选。'
      return
    }
    const currentDateMode = deps.form.dateMode
    const currentStartDate = deps.form.startDate
    const currentEndDate = deps.form.endDate
    const currentExcludeSt = deps.form.excludeSt
    const currentAtrPeriod = deps.form.atrPeriod
    const request = riskOptimizationResult.value.request
    const splits = request.splits
    const validation = splits && typeof splits === 'object'
      ? (splits as Record<string, unknown>).validation
      : null
    const validationDates = validation && typeof validation === 'object'
      ? validation as Record<string, unknown>
      : null
    const matchesValidationRange = currentStartDate === validationDates?.start_date
      && currentEndDate === validationDates?.end_date

    deps.applyStoredCandidate(candidate.config)
    deps.form.dateMode = currentDateMode
    deps.form.startDate = currentStartDate
    deps.form.endDate = currentEndDate
    deps.form.excludeSt = currentExcludeSt
    deps.form.atrPeriod = currentAtrPeriod
    deps.onCandidateApplied(matchesValidationRange
      ? {
          candidate_hash: candidate.candidate_hash,
          data_fingerprint: typeof riskOptimizationResult.value.data_fingerprint === 'string'
            ? riskOptimizationResult.value.data_fingerprint
            : null,
          validation_metrics: candidate.metrics.validation ?? null,
        }
      : null)
    deps.message.value = matchesValidationRange
      ? '已应用候选风险参数并保留当前验证区间；运行普通组合回测以核对榜单复现。'
      : `已应用候选风险参数并保留当前区间 ${currentStartDate || '默认起始日'} 至 ${currentEndDate || '默认结束日'}；运行普通组合回测查看该区间表现。`
  }

  return {
    riskOptimizationJob,
    riskOptimizationLoading,
    riskOptimizationResult,
    riskOptimizationResultsVisible,
    riskOptimizationBoard,
    riskOptimizationMaxTrials,
    riskOptimizationRestoreRequired,
    riskOptimizationStale,
    riskOptimizationApplicationBlocked,
    riskOptimizationCandidates,
    riskOptimizationHistory,
    riskOptimizationHistoryTotal,
    riskOptimizationHistoryLoading,
    riskOptimizationHistoryLoadingId,
    riskOptimizationHistoryError,
    riskOptimizationHistoryDetail,
    riskOptimizationHistoryDetailId,
    riskOptimizationHistoryDetailLoadingId,
    riskOptimizationHistoryDetailError,
    riskMetric,
    riskMetricRatio,
    riskOptimizationPhaseLabel,
    startRiskOptimization,
    pollRiskOptimization,
    resumeActiveRiskOptimization,
    loadRiskOptimizationHistory,
    toggleRiskOptimizationHistoryDetail,
    closeRiskOptimizationHistoryDetail,
    loadRiskOptimizationHistoryDetail,
    cancelRiskOptimization,
    applyRiskOptimizationCandidate,
  }
}
