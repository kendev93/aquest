import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  createPortfolioBacktest,
  deletePortfolioBacktestHistory,
  fetchPortfolioBacktestHistory,
  getPortfolioBacktest,
  getPortfolioBacktestHistory,
  getPortfolioBacktestResults,
} from './portfolioBacktest'
import { ApiError } from './client'

describe('portfolio backtest API', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('creates a job and retrieves its status and results', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { job_id: 'portfolio-1', status: 'queued' } }), { status: 202 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { job_id: 'portfolio-1', status: 'completed' } }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { max_positions: 3 }, meta: {} }), { status: 200 }))

    await expect(createPortfolioBacktest({
      universe: 'all',
      selected_signals: ['custom.buy'],
      combine_mode: 'any',
      minimum_matches: null,
      ranking_value: 'custom.rank',
      max_positions: 3,
      sell_signal: 'custom.sell',
    })).resolves.toEqual({ job_id: 'portfolio-1', status: 'queued' })
    await expect(getPortfolioBacktest('portfolio-1')).resolves.toEqual({ job_id: 'portfolio-1', status: 'completed' })
    await expect(getPortfolioBacktestResults('portfolio-1')).resolves.toEqual({ max_positions: 3 })
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      '/api/v1/portfolio-backtests',
      '/api/v1/portfolio-backtests/portfolio-1',
      '/api/v1/portfolio-backtests/portfolio-1/results',
    ])
  })

  it('uses the fallback message when the server omits error details', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      new Response(JSON.stringify({ error: {} }), { status: 500 }),
    )

    await expect(getPortfolioBacktest('missing/job')).rejects.toMatchObject({
      name: 'ApiError',
      message: '请求失败，请检查组合回测配置后重试。',
      status: 500,
    })
  })

  it('reads and deletes persisted portfolio history', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: [{ history_id: 'p-1' }] }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { history_id: 'p-1', result: { max_positions: 3 } } }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { deleted: true } }), { status: 200 }))

    await expect(fetchPortfolioBacktestHistory()).resolves.toEqual([{ history_id: 'p-1' }])
    await expect(getPortfolioBacktestHistory('p-1')).resolves.toEqual({ history_id: 'p-1', result: { max_positions: 3 } })
    await expect(deletePortfolioBacktestHistory('p-1')).resolves.toEqual({ deleted: true })
    expect(fetchMock.mock.calls.map(([url, init]) => [url, init?.method ?? 'GET'])).toEqual([
      ['/api/v1/portfolio-backtests/history', 'GET'],
      ['/api/v1/portfolio-backtests/history/p-1', 'GET'],
      ['/api/v1/portfolio-backtests/history/p-1', 'DELETE'],
    ])
  })
})
