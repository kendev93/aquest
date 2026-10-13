import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  createStrategyFitness,
  deleteStrategyFitnessHistory,
  fetchStrategyFitnessHistory,
  getStrategyFitness,
  getStrategyFitnessHistory,
  getStrategyFitnessResults,
} from './strategyFitness'

describe('strategy fitness API', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('creates a job and retrieves its status and report', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { job_id: 'fitness-1', status: 'queued' } }), { status: 202 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { job_id: 'fitness-1', status: 'completed' } }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { results: [] }, meta: {} }), { status: 200 }))

    await expect(createStrategyFitness({
      universe: 'all', selected_signals: ['custom.buy'],
      combine_mode: 'any', minimum_matches: null, ranking_value: 'custom.rank',
      sell_signal: 'custom.sell', stop_loss_pct: 0.08,
    })).resolves.toEqual({ job_id: 'fitness-1', status: 'queued' })
    await expect(getStrategyFitness('fitness-1')).resolves.toEqual({ job_id: 'fitness-1', status: 'completed' })
    await expect(getStrategyFitnessResults('fitness-1')).resolves.toEqual({ results: [] })
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      '/api/v1/strategy-fitness',
      '/api/v1/strategy-fitness/fitness-1',
      '/api/v1/strategy-fitness/fitness-1/results',
    ])
  })

  it('uses the fallback message when the server omits error details', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      new Response(JSON.stringify({ error: {} }), { status: 500 }),
    )

    await expect(getStrategyFitness('missing/job')).rejects.toMatchObject({
      name: 'ApiError',
      message: '请求失败，请检查策略适配性配置后重试。',
      status: 500,
    })
  })

  it('reads and deletes persisted fitness history', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: [{ history_id: 'f-1' }] }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { history_id: 'f-1', result: { processed: 2 } } }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { deleted: true } }), { status: 200 }))

    await expect(fetchStrategyFitnessHistory()).resolves.toEqual([{ history_id: 'f-1' }])
    await expect(getStrategyFitnessHistory('f-1')).resolves.toEqual({ history_id: 'f-1', result: { processed: 2 } })
    await expect(deleteStrategyFitnessHistory('f-1')).resolves.toEqual({ deleted: true })
    expect(fetchMock.mock.calls.map(([url, init]) => [url, init?.method ?? 'GET'])).toEqual([
      ['/api/v1/strategy-fitness/history', 'GET'],
      ['/api/v1/strategy-fitness/history/f-1', 'GET'],
      ['/api/v1/strategy-fitness/history/f-1', 'DELETE'],
    ])
  })
})
