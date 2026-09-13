import { beforeEach, describe, expect, it, vi } from 'vitest'

import { fetchActiveJobs } from './jobs'

describe('jobs API', () => {
  beforeEach(() => vi.restoreAllMocks())

  it('returns active jobs from the shared endpoint for page recovery', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      new Response(JSON.stringify({
        data: [{
          job_id: 'backtest-1',
          description: '历史回测',
          status: 'running',
          progress: 0.42,
          total_candidates: 1,
          total_scanned: 0,
          errors: 0,
          error: null,
          result: null,
        }],
      }), { status: 200 }),
    )

    await expect(fetchActiveJobs()).resolves.toMatchObject([{
      job_id: 'backtest-1',
      description: '历史回测',
      progress: 0.42,
    }])
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/jobs/active',
      expect.objectContaining({ headers: { 'Content-Type': 'application/json' } }),
    )
  })
})
