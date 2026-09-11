import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  createBacktest,
  deleteBacktestHistory,
  fetchBacktestHistory,
  getBacktest,
  getBacktestHistory,
  getBacktestResults,
} from './backtest'

describe('backtest API', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('creates a job and retrieves its status and results', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { job_id: 'bt-1', status: 'queued' } }), { status: 202 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { job_id: 'bt-1', status: 'completed' } }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { code: '600000' }, meta: {} }), { status: 200 }))
    const payload = { market: 'SH' as const, code: '600000', buy_signal: 'custom.buy', sell_signal: 'custom.sell' }

    await expect(createBacktest(payload)).resolves.toEqual({ job_id: 'bt-1', status: 'queued' })
    await expect(getBacktest('bt-1')).resolves.toEqual({ job_id: 'bt-1', status: 'completed' })
    await expect(getBacktestResults('bt-1')).resolves.toEqual({ code: '600000' })
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      '/api/v1/backtests',
      '/api/v1/backtests/bt-1',
      '/api/v1/backtests/bt-1/results',
    ])
  })

  it('uses the fallback message when the server omits error details', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      new Response(JSON.stringify({ error: {} }), { status: 500 }),
    )

    await expect(getBacktest('missing/job')).rejects.toMatchObject({
      name: 'FormulaScreenApiError',
      message: '请求失败，请检查回测配置后重试。',
      status: 500,
    })
  })

  it('reads and deletes persisted backtest history', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: [{ history_id: 'h-1' }] }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { history_id: 'h-1', result: { code: '600000' } } }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { deleted: true } }), { status: 200 }))

    await expect(fetchBacktestHistory()).resolves.toEqual([{ history_id: 'h-1' }])
    await expect(getBacktestHistory('h-1')).resolves.toEqual({ history_id: 'h-1', result: { code: '600000' } })
    await expect(deleteBacktestHistory('h-1')).resolves.toEqual({ deleted: true })
    expect(fetchMock.mock.calls.map(([url, init]) => [url, init?.method ?? 'GET'])).toEqual([
      ['/api/v1/backtests/history', 'GET'],
      ['/api/v1/backtests/history/h-1', 'GET'],
      ['/api/v1/backtests/history/h-1', 'DELETE'],
    ])
  })
})
