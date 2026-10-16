/**
 * 买卖条件研究的任务执行：提交、轮询、取消、恢复进行中的任务。
 *
 * 从 ConditionResearchPage 拆出来。job / report / loading 由本模块持有，
 * 页面解构后名字不变。表单校验与组装留在页面（validate / buildPayload），
 * 任务完成后的页面收尾（重置结果视图、清掉旧勾选、刷新历史并定位新记录）
 * 通过 onReportReady 注入——这些动作写的都是页面自己的状态。
 */

import { ref } from 'vue'
import type { Ref } from 'vue'

import { createConditionResearch, cancelConditionResearch, getConditionResearch, getConditionResearchResults } from '../api/conditionResearch'
import { adoptActiveJob, useActiveJobResume } from '../composables/useActiveJobResume'
import { pollUntil } from './usePolling'
import type { ConditionResearchJobState, ConditionResearchReport, ConditionResearchPayload } from '../types'

export interface ConditionResearchJobDeps {
  /** 本地校验；返回的对象里有错误信息时 submit 不发请求。 */
  validate: () => Record<string, string>
  /** 校验结果写回这里，表单在各字段下显示内联错误。 */
  errors: Ref<Record<string, string>>
  /** 组装请求 payload。 */
  buildPayload: () => ConditionResearchPayload
  /** 页面顶部的消息条。 */
  message: Ref<string>
  /** 后端错误 → 用户可读文案。 */
  describeError: (error: unknown) => string
  /** 报告就绪后的页面收尾（写页面自己的状态）。 */
  onReportReady: (jobId: string) => Promise<void>
}

export function useConditionResearchJob(deps: ConditionResearchJobDeps) {
  const job = ref<ConditionResearchJobState | null>(null)
  const report = ref<ConditionResearchReport | null>(null)
  const loading = ref(false)

  async function pollJob(jobId: string): Promise<void> {
    const state = await pollUntil(
      async () => {
        const current = await getConditionResearch(jobId)
        job.value = current
        return current
      },
      {
        intervalMs: 220,
        until: (current) =>
          current.status === 'completed' || current.status === 'failed' || current.status === 'cancelled',
      },
    )
    if (state.status === 'completed') {
      report.value = await getConditionResearchResults(jobId)
      deps.message.value = '研究完成：处理 ' + report.value.processed + ' 只股票，发现 '
        + report.value.candidates.length + ' 条候选规则。'
      await deps.onReportReady(jobId)
      return
    }
    deps.message.value = state.error
      ?? (state.status === 'cancelled' ? '研究任务已取消。' : '买卖条件研究失败。')
  }

  async function submit(): Promise<boolean> {
    deps.errors.value = deps.validate()
    deps.message.value = ''
    if (Object.values(deps.errors.value).some(Boolean)) return false
    loading.value = true
    report.value = null
    try {
      const created = await createConditionResearch(deps.buildPayload())
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
      return true
    } catch (error) {
      deps.message.value = deps.describeError(error)
      return false
    } finally {
      loading.value = false
    }
  }

  async function cancel(): Promise<void> {
    if (!job.value || !loading.value) return
    try {
      await cancelConditionResearch(job.value.job_id)
      deps.message.value = '已请求取消，等待当前股票回测结束。'
    } catch (error) {
      deps.message.value = deps.describeError(error)
    }
  }

  /** 刷新页面时后端可能还在跑上一轮：找到它并接上轮询，而不是让用户重新提交。 */
  const { resumeActiveJob } = useActiveJobResume({
    descriptions: ['买卖条件研究'],
    // `/jobs/active` 只给通用任务形状，`result` 要等轮询拉到详情才有；这里只校验
    // 页面在接管瞬间就会读的字段，其余由 poll 的第一次拉取补齐。
    requiredFields: ['job_id', 'status', 'progress'],
    adopt: (active) => { job.value = adoptActiveJob<ConditionResearchJobState>(active) },
    poll: (active) => pollJob(active.job_id),
    setLoading: (value) => { loading.value = value },
    reportError: (error) => { deps.message.value = deps.describeError(error) },
  })

  return { job, report, loading, submit, cancel, resumeActiveJob }
}
