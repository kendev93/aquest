import { beforeEach, describe, expect, it, vi } from 'vitest'

import { fetchActiveJobs } from './jobs'
import { ApiError } from './client'

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

  it('surfaces a structured error from the shared endpoint', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      new Response(JSON.stringify({ error: { message: '任务服务暂不可用' } }), { status: 503 }),
    )

    await expect(fetchActiveJobs()).rejects.toMatchObject({
      name: 'ApiError',
      message: '任务服务暂不可用',
      status: 503,
    })
  })

  it('falls back to the detail field when the shared endpoint omits an error object', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: '任务服务正在重启' }), { status: 503 }),
    )

    await expect(fetchActiveJobs()).rejects.toMatchObject({
      message: '任务服务正在重启',
      status: 503,
    })
  })

  it('uses the default message when the shared endpoint has no error details', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      new Response(JSON.stringify({}), { status: 503 }),
    )

    await expect(fetchActiveJobs()).rejects.toMatchObject({
      message: '任务状态读取失败，请稍后重试。',
      status: 503,
    })
  })
})
