import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'

import {
  cancelConditionResearch,
  createConditionResearch,
  getConditionResearch,
  getConditionResearchResults,
} from '../api/conditionResearch'
import { fetchActiveJobs } from '../api/jobs'
import { useConditionResearchJob } from './useConditionResearchJob'
import type { ConditionResearchJobDeps } from './useConditionResearchJob'
import type { ConditionResearchJobState, ConditionResearchReport } from '../types'

vi.mock('../api/conditionResearch', () => ({
  cancelConditionResearch: vi.fn(),
  createConditionResearch: vi.fn(),
  getConditionResearch: vi.fn(),
  getConditionResearchResults: vi.fn(),
}))

vi.mock('../api/jobs', () => ({ fetchActiveJobs: vi.fn() }))

function jobState(over: Partial<ConditionResearchJobState> = {}): ConditionResearchJobState {
  return {
    job_id: 'job-1',
    status: 'completed',
    progress: 1,
    total_candidates: 3,
    total_scanned: 120,
    errors: 0,
    skipped: 0,
    error: null,
    result: null,
    ...over,
  }
}

const report = { processed: 120, candidates: [{ id: 'a' }, { id: 'b' }, { id: 'c' }] } as unknown as ConditionResearchReport

/** 活动任务列表里的条目：轮询恢复只按 description 找，其余字段照后端形状填齐。 */
function activeJob(jobId: string, description: string) {
  return {
    job_id: jobId,
    description,
    status: 'running' as const,
    progress: 0.5,
    total_candidates: 10,
    total_scanned: 5,
    errors: 0,
    skipped: 0,
    error: null,
  }
}

function makeDeps(over: Partial<ConditionResearchJobDeps> = {}): ConditionResearchJobDeps {
  return {
    validate: vi.fn().mockReturnValue({}),
    errors: ref({}),
    buildPayload: vi.fn().mockReturnValue({ universe: 'all' }),
    message: ref(''),
    describeError: (error: unknown) => (error instanceof Error ? error.message : '研究失败'),
    onReportReady: vi.fn().mockResolvedValue(undefined),
    ...over,
  }
}

describe('useConditionResearchJob', () => {
  let deps: ConditionResearchJobDeps

  beforeEach(() => {
    vi.clearAllMocks()
    deps = makeDeps()
    vi.mocked(createConditionResearch).mockResolvedValue({ job_id: 'job-1', status: 'queued' })
    vi.mocked(getConditionResearch).mockResolvedValue(jobState())
    vi.mocked(getConditionResearchResults).mockResolvedValue(report)
    vi.mocked(fetchActiveJobs).mockResolvedValue([])
  })

  it('keeps the request offline when the form has errors', async () => {
    deps = makeDeps({ validate: vi.fn().mockReturnValue({ codesText: '请填写代码' }) })
    const research = useConditionResearchJob(deps)
    await expect(research.submit()).resolves.toBe(false)
    expect(createConditionResearch).not.toHaveBeenCalled()
    expect(research.loading.value).toBe(false)
  })

  it('polls to completion, shows the result and runs the page cleanup', async () => {
    const research = useConditionResearchJob(deps)
    await expect(research.submit()).resolves.toBe(true)

    expect(getConditionResearch).toHaveBeenCalledWith('job-1')
    expect(research.report.value).toEqual(report)
    expect(deps.message.value).toBe('研究完成：处理 120 只股票，发现 3 条候选规则。')
    expect(deps.onReportReady).toHaveBeenCalledWith('job-1')
    expect(research.loading.value).toBe(false)
  })

  it('prefers the backend error and distinguishes cancellation from failure', async () => {
    vi.mocked(getConditionResearch).mockResolvedValue(jobState({ status: 'failed', error: '行情库为空' }))
    const failed = useConditionResearchJob(deps)
    await failed.submit()
    expect(deps.message.value).toBe('行情库为空')
    expect(getConditionResearchResults).not.toHaveBeenCalled()

    vi.mocked(getConditionResearch).mockResolvedValue(jobState({ status: 'failed', error: null }))
    deps.message.value = ''
    await failed.submit()
    expect(deps.message.value).toBe('买卖条件研究失败。')

    vi.mocked(getConditionResearch).mockResolvedValue(jobState({ status: 'cancelled', error: null }))
    deps.message.value = ''
    await failed.submit()
    expect(deps.message.value).toBe('研究任务已取消。')
  })

  it('only asks to cancel a job it is still waiting on', async () => {
    const research = useConditionResearchJob(deps)
    await research.cancel()
    expect(cancelConditionResearch).not.toHaveBeenCalled()

    vi.mocked(getConditionResearch).mockResolvedValue(jobState({ status: 'running' }))
    research.job.value = jobState({ status: 'running' })
    research.loading.value = true
    vi.mocked(cancelConditionResearch).mockResolvedValue({ cancelled: true })
    await research.cancel()
    expect(cancelConditionResearch).toHaveBeenCalledWith('job-1')
    expect(deps.message.value).toBe('已请求取消，等待当前股票回测结束。')

    vi.mocked(cancelConditionResearch).mockRejectedValueOnce(new Error('任务已结束'))
    await research.cancel()
    expect(deps.message.value).toBe('任务已结束')
  })

  it('re-attaches a research job the backend is still running', async () => {
    vi.mocked(fetchActiveJobs).mockResolvedValue([
      activeJob('other', '公式选股'),
      activeJob('job-9', '买卖条件研究'),
    ])
    vi.mocked(getConditionResearch).mockResolvedValue(jobState({ job_id: 'job-9' }))
    const research = useConditionResearchJob(deps)
    await research.resumeActiveJob()

    expect(getConditionResearch).toHaveBeenCalledWith('job-9')
    expect(deps.onReportReady).toHaveBeenCalledWith('job-9')
    expect(research.loading.value).toBe(false)
  })

  it('ignores unrelated active jobs and reports resume failures', async () => {
    const research = useConditionResearchJob(deps)
    await research.resumeActiveJob()
    expect(getConditionResearch).not.toHaveBeenCalled()

    vi.mocked(fetchActiveJobs).mockRejectedValue(new Error('服务未就绪'))
    await research.resumeActiveJob()
    expect(deps.message.value).toBe('服务未就绪')
  })
})
