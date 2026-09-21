import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  createStrategy,
  deleteStrategy,
  fetchStrategies,
  fetchStrategyTracking,
  getStrategy,
  getStrategyTracking,
  getTrackingJob,
  launchStrategyTracking,
  rerunStrategyTracking,
  stopStrategyTracking,
  updateStrategy,
} from './savedStrategies'
import type { PortfolioBacktestPayload } from '../types'

function ok(payload: unknown): Response {
  return new Response(JSON.stringify(payload), { status: 200 })
}

describe('savedStrategies API', () => {
  beforeEach(() => vi.restoreAllMocks())

  it('lists saved strategies from the library endpoint', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(ok({ data: [{ strategy_id: 's1', name: '轮动' }] }))

    await expect(fetchStrategies()).resolves.toMatchObject([{ strategy_id: 's1' }])
    expect(fetchMock).toHaveBeenCalledWith('/api/v1/strategies', expect.anything())
  })

  it('reads one strategy with its replayable request', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      ok({ data: { strategy_id: 's1', request: { universe: 'all' } } }),
    )

    await expect(getStrategy('s1')).resolves.toMatchObject({ strategy_id: 's1' })
  })

  it('posts a new strategy with a frozen request', async () => {
    const request = { universe: 'all' } as PortfolioBacktestPayload
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      ok({ data: { strategy_id: 's1', name: '轮动' } }),
    )

    await createStrategy({ name: '轮动', request, summary: { annual_return: 0.2 } })

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/strategies',
      expect.objectContaining({ method: 'POST' }),
    )
  })

  it('renames a strategy without touching its configuration', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      ok({ data: { strategy_id: 's1', name: '新名字' } }),
    )

    await expect(updateStrategy('s1', { name: '新名字' })).resolves.toMatchObject({
      name: '新名字',
    })
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/strategies/s1',
      expect.objectContaining({ method: 'PATCH' }),
    )
  })

  it('deletes a strategy and reports whether a row went away', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(ok({ data: { deleted: true } }))

    await expect(deleteStrategy('s1')).resolves.toEqual({ deleted: true })
  })

  it('surfaces duplicate-name rejections from the server', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: '已存在同名策略: 轮动' }), { status: 422 }),
    )

    await expect(
      createStrategy({ name: '轮动', request: {} as PortfolioBacktestPayload, summary: {} }),
    ).rejects.toMatchObject({ status: 422, message: '已存在同名策略: 轮动' })
  })

  it('turns an unreadable error body into the default message', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(new Response('{}', { status: 503 }))

    await expect(fetchStrategies()).rejects.toMatchObject({
      message: '策略库请求失败，请稍后重试。',
    })
  })
})

describe('strategy tracking API', () => {
  beforeEach(() => vi.restoreAllMocks())

  it('loads every strategy together with its tracking state', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      ok({ data: [{ strategy_id: 's1', tracking_status: 'active' }] }),
    )

    await expect(fetchStrategyTracking()).resolves.toMatchObject([{ tracking_status: 'active' }])
    expect(fetchMock).toHaveBeenCalledWith('/api/v1/strategies/tracking', expect.anything())
  })

  it('loads a single tracking snapshot including its curve', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      ok({ data: { strategy_id: 's1', observations: [{ trade_date: 20260901, equity: 1 }] } }),
    )

    await expect(getStrategyTracking('s1')).resolves.toMatchObject({
      observations: [{ trade_date: 20260901 }],
    })
  })

  it('launches by submitting a background job rather than waiting inline', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(ok({ data: { job_id: 'job-1', status: 'queued' } }))

    await expect(launchStrategyTracking('s1')).resolves.toEqual({
      job_id: 'job-1',
      status: 'queued',
    })
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/strategies/tracking/s1/launch',
      expect.objectContaining({ method: 'POST' }),
    )
  })

  it('reruns against the newest market day', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(ok({ data: { job_id: 'job-2', status: 'queued' } }))

    await expect(rerunStrategyTracking('s1')).resolves.toMatchObject({ job_id: 'job-2' })
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/strategies/tracking/s1/rerun',
      expect.objectContaining({ method: 'POST' }),
    )
  })

  it('stops tracking and returns the retained snapshot', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      ok({ data: { strategy_id: 's1', tracking_status: 'off' } }),
    )

    await expect(stopStrategyTracking('s1')).resolves.toMatchObject({ tracking_status: 'off' })
  })

  it('polls a tracking job and surfaces its failure message', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      ok({ data: { job_id: 'job-1', status: 'failed', error: '行情缺失', result: null } }),
    )

    await expect(getTrackingJob('job-1')).resolves.toMatchObject({
      status: 'failed',
      error: '行情缺失',
    })
  })

  it('reports a missing strategy as a 404 instead of a generic failure', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: '策略不存在' }), { status: 404 }),
    )

    await expect(launchStrategyTracking('ghost')).rejects.toMatchObject({
      status: 404,
      message: '策略不存在',
    })
  })
})
