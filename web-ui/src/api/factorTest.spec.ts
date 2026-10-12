import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  createFactorTest,
  deleteFactorTestHistory,
  fetchFactorTestHistory,
  getFactorTest,
  getFactorTestHistory,
  getFactorTestResults,
} from './factorTest'

describe('factor test API', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('posts the horizon together with the selected values', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ data: { job_id: 'job-1', status: 'queued' } }), { status: 200 }),
    )

    await expect(
      createFactorTest({ horizon: 10, value_ids: ['technical.rsi14'], permutations: 0 }),
    ).resolves.toEqual({ job_id: 'job-1', status: 'queued' })

    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/v1/factor-test')
    expect(JSON.parse(String(init?.body))).toMatchObject({
      horizon: 10,
      value_ids: ['technical.rsi14'],
      permutations: 0,
    })
  })

  it('reads the job state and the finished report from their own paths', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ data: { job_id: 'job-1', status: 'running' } }), {
          status: 200,
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ data: { factors: [] } }), { status: 200 }),
      )

    await expect(getFactorTest('job-1')).resolves.toMatchObject({ status: 'running' })
    await expect(getFactorTestResults('job-1')).resolves.toEqual({ factors: [] })

    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/factor-test/job-1')
    expect(fetchMock.mock.calls[1][0]).toBe('/api/v1/factor-test/job-1/results')
  })

  it('surfaces the server message when the run cannot fit in memory', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({ error: { code: 'validation_error', message: '因子面板需要 9.0 GB 内存' } }),
        { status: 422 },
      ),
    )

    await expect(createFactorTest({ horizon: 5 })).rejects.toThrow('因子面板需要 9.0 GB 内存')
  })

  it('lists, reads and deletes history', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: [] }), { status: 200 }))
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ data: { request: { horizon: 5 } } }), { status: 200 }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ data: { deleted: true } }), { status: 200 }),
      )

    await expect(fetchFactorTestHistory()).resolves.toEqual([])
    await expect(getFactorTestHistory('h1')).resolves.toMatchObject({ request: { horizon: 5 } })
    await expect(deleteFactorTestHistory('h1')).resolves.toEqual({ deleted: true })

    expect(fetchMock.mock.calls[1][0]).toBe('/api/v1/factor-test/history/h1')
    expect(String(fetchMock.mock.calls[2][1]?.method)).toBe('DELETE')
  })
})
