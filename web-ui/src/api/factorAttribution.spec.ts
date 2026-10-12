import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  createFactorAttribution,
  deleteFactorAttributionHistory,
  fetchAttributionSources,
  fetchFactorAttributionHistory,
  getFactorAttribution,
  getFactorAttributionResults,
} from './factorAttribution'

describe('factor attribution API', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('posts the source history together with the statistical knobs', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ data: { job_id: 'job-1', status: 'queued' } }), { status: 200 }),
    )

    await expect(
      createFactorAttribution({
        source_history_id: 'hist-1',
        value_ids: ['technical.rsi14'],
        permutations: 0,
      }),
    ).resolves.toEqual({ job_id: 'job-1', status: 'queued' })

    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/v1/factor-attribution')
    expect(JSON.parse(String(init?.body))).toMatchObject({
      source_history_id: 'hist-1',
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

    await expect(getFactorAttribution('job-1')).resolves.toMatchObject({ status: 'running' })
    await expect(getFactorAttributionResults('job-1')).resolves.toEqual({ factors: [] })

    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/factor-attribution/job-1')
    expect(fetchMock.mock.calls[1][0]).toBe('/api/v1/factor-attribution/job-1/results')
  })

  it('surfaces the server message when the history has no trades', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({ error: { code: 'validation_error', message: '这条回测记录没有成交明细，无法归因' } }),
        { status: 422 },
      ),
    )

    await expect(createFactorAttribution({ source_history_id: 'hist-1' })).rejects.toThrow(
      '这条回测记录没有成交明细，无法归因',
    )
  })

  it('asks for attributable sources and its own history separately', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ data: [{ history_id: 'a', backtest_type: 'portfolio' }] }), {
          status: 200,
        }),
      )
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: [] }), { status: 200 }))

    await expect(fetchAttributionSources()).resolves.toHaveLength(1)
    await expect(fetchFactorAttributionHistory()).resolves.toEqual([])

    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/factor-attribution/sources')
    expect(fetchMock.mock.calls[1][0]).toBe('/api/v1/factor-attribution/history')
  })

  it('deletes one archived attribution', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ data: { deleted: true } }), { status: 200 }),
    )

    await expect(deleteFactorAttributionHistory('hist-1')).resolves.toEqual({ deleted: true })

    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/v1/factor-attribution/history/hist-1')
    expect(init?.method).toBe('DELETE')
  })

  it('falls back to a readable message when the server sends none', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({}), { status: 500 }),
    )

    await expect(getFactorAttribution('job-1')).rejects.toThrow('请求失败，请检查因子归因配置后重试。')
  })
})
