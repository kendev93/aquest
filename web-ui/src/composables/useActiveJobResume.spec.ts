import { beforeEach, describe, expect, it, vi } from 'vitest'

import { fetchActiveJobs } from '../api/jobs'
import { adoptActiveJob, useActiveJobResume } from './useActiveJobResume'
import type { ActiveJobCandidate, ActiveJobResumeOptions } from './useActiveJobResume'
import type { ActiveJobState } from '../types/market'

vi.mock('../api/jobs', () => ({ fetchActiveJobs: vi.fn() }))

interface PageJobState extends ActiveJobState {
  result: string | null
}

function activeJob(
  jobId: string,
  description: string,
  extra: Record<string, unknown> = {},
): ActiveJobState {
  return {
    job_id: jobId,
    description,
    status: 'running',
    progress: 0.4,
    total_candidates: 3,
    total_scanned: 2,
    errors: 0,
    error: null,
    ...extra,
  } as ActiveJobState
}

function harness(
  candidateOverrides: Partial<ActiveJobCandidate> = {},
  options: Partial<ActiveJobResumeOptions> = {},
) {
  const adopted: ActiveJobState[] = []
  const polled: string[] = []
  const loading: boolean[] = []
  const errors: unknown[] = []
  const candidate: ActiveJobCandidate = {
    descriptions: ['组合回测'],
    adopt: (job) => { adopted.push(job) },
    poll: async (job) => { polled.push(job.job_id) },
    setLoading: (value) => { loading.push(value) },
    ...candidateOverrides,
  }
  const resumed = useActiveJobResume({
    candidates: [candidate],
    reportError: (error) => { errors.push(error) },
    ...options,
  })
  return { resumed, adopted, polled, loading, errors }
}

describe('useActiveJobResume', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('adopts the matching active job, polls it, and always clears the loading flag', async () => {
    vi.mocked(fetchActiveJobs).mockResolvedValue([
      activeJob('job-1', '公式选股'),
      activeJob('job-2', '组合回测'),
    ])
    const { resumed, adopted, polled, loading, errors } = harness()

    await expect(resumed.resumeActiveJob()).resolves.toBe(true)

    expect(adopted.map((job) => job.job_id)).toEqual(['job-2'])
    expect(polled).toEqual(['job-2'])
    expect(loading).toEqual([true, false])
    expect(errors).toEqual([])
  })

  it('does nothing when no active job belongs to this page', async () => {
    vi.mocked(fetchActiveJobs).mockResolvedValue([activeJob('job-1', '公式选股')])
    const { resumed, adopted, polled, loading, errors } = harness()

    await expect(resumed.resumeActiveJob()).resolves.toBe(false)

    expect(adopted).toEqual([])
    expect(polled).toEqual([])
    expect(errors).toEqual([])
    // 与收口前一致：没有可接管的任务时也会把加载态归位一次。
    expect(loading).toEqual([false])
  })

  it('takes over only the first matching candidate and still fetches the job list once', async () => {
    vi.mocked(fetchActiveJobs).mockResolvedValue([activeJob('job-9', '策略前十全量复核')])
    const discovery = harness()
    const refined = harness()
    const replay = harness({ descriptions: ['策略前十全量复核'] })
    const resumed = useActiveJobResume({
      candidates: [
        { descriptions: ['策略实验室'], adopt: () => {}, poll: async () => {} },
        { descriptions: ['策略二次优化'], adopt: () => {}, poll: async () => {} },
        {
          descriptions: ['策略前十全量复核'],
          adopt: (job) => { discovery.adopted.push(job) },
          poll: async (job) => { refined.polled.push(job.job_id) },
          setLoading: (value) => { replay.loading.push(value) },
        },
      ],
      reportError: (error) => { replay.errors.push(error) },
    })

    await expect(resumed.resumeActiveJob()).resolves.toBe(true)

    expect(fetchActiveJobs).toHaveBeenCalledTimes(1)
    expect(discovery.adopted.map((job) => job.job_id)).toEqual(['job-9'])
    expect(refined.polled).toEqual(['job-9'])
    expect(replay.loading).toEqual([true, false])
  })

  it('refuses to adopt a payload missing a field the page reads right away', async () => {
    const partial = activeJob('job-3', '组合回测') as unknown as Record<string, unknown>
    delete partial.progress
    vi.mocked(fetchActiveJobs).mockResolvedValue([partial as unknown as ActiveJobState])
    const { resumed, adopted, polled, errors } = harness({ requiredFields: ['progress'] })

    await resumed.resumeActiveJob()

    expect(adopted).toEqual([])
    expect(polled).toEqual([])
    expect((errors[0] as Error).message).toContain('progress')
  })

  it('reports resume failures once instead of leaving the page half attached', async () => {
    vi.mocked(fetchActiveJobs).mockRejectedValue(new Error('后端未就绪'))
    const { resumed, loading, errors } = harness()

    await expect(resumed.resumeActiveJob()).resolves.toBe(false)

    expect(errors).toHaveLength(1)
    expect(loading).toEqual([false])
  })

  it('keeps the single sanctioned widening of the generic job shape in one place', () => {
    const job = activeJob('job-4', '组合回测')

    expect(adoptActiveJob<PageJobState>(job)).toBe(job)
  })
})
