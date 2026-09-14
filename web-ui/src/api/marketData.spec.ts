import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  createOnlineBackfill,
  createMarketDataSync,
  createOnlineSync,
  cancelMarketDataJob,
  fetchLocalChart,
  fetchLocalInstruments,
  fetchMarketDataCapabilities,
  fetchActiveMarketDataJobs,
  fetchStoreStatus,
  getMarketDataJob,
} from './marketData'

describe('market data API', () => {
  beforeEach(() => vi.restoreAllMocks())

  it('fetches paginated local instruments and chart data', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(new Response(JSON.stringify({
        data: [{ market: 'SH', code: '600000', instrument_type: 'stock', bars: 300, data_start: '2020-01-01', data_end: '2024-12-31', last_close: 12.5, error: null }],
        meta: { total: 1, page: 1, page_size: 20, pages: 1 },
      }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        data: { market: 'SH', code: '600000', period: 'monthly', total_daily_bars: 300, bars: 12, available_data_start: '2020-01-01', available_data_end: '2024-12-31', data_start: '2024-01-31', data_end: '2024-12-31', candles: [] },
      }), { status: 200 }))

    await expect(fetchLocalInstruments({ market: 'SH', keyword: '600', page: 1, page_size: 20 })).resolves.toMatchObject({ meta: { total: 1 } })
    await expect(fetchLocalChart({ market: 'SH', code: '600000', period: 'monthly', start_date: '2024-01-01', end_date: '2024-12-31' })).resolves.toMatchObject({ period: 'monthly', code: '600000' })

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      '/api/v1/market-data/local/instruments?market=SH&keyword=600&page=1&page_size=20',
      '/api/v1/market-data/local/SH/600000/bars?period=monthly&start_date=2024-01-01&end_date=2024-12-31',
    ])
  })

  it('surfaces local-data API errors', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(new Response(JSON.stringify({ error: {} }), { status: 422 }))

    await expect(fetchLocalInstruments()).rejects.toMatchObject({
      name: 'FormulaScreenApiError',
      message: '请求失败，请检查共享行情数据库后重试。',
      status: 422,
    })
  })

  it('posts a one-click online-only synchronization request', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      new Response(JSON.stringify({ data: { job_id: 'sync-1', status: 'queued' } }), { status: 202 }),
    )

    await expect(createMarketDataSync({
      universe: 'sh',
      instrument_types: ['stock'],
      boards: ['main'],
    })).resolves.toMatchObject({ job_id: 'sync-1' })

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/market-data/sync',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          universe: 'sh',
          instrument_types: ['stock'],
          boards: ['main'],
        }),
      }),
    )
  })

  it('starts online sync jobs and reads store status', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { job_id: 'online-1', status: 'queued' } }), { status: 202 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { database_path: '/data/market/market.duckdb', schema_version: 1, instrument_count: 2, bar_count: 10, data_start: '2024-01-01', data_end: '2024-01-10', last_online_sync_at: null } }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { job_id: 'online-1', status: 'completed', progress: 1, total_candidates: 1, total_scanned: 1, errors: 0, error: null, result: null } }), { status: 200 }))

    await expect(createOnlineSync({ universe: 'all', bars: 800 })).resolves.toMatchObject({ job_id: 'online-1' })
    await expect(fetchStoreStatus()).resolves.toMatchObject({ instrument_count: 2 })
    await expect(getMarketDataJob('online-1')).resolves.toMatchObject({ status: 'completed' })
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      '/api/v1/market-data/sync-online',
      '/api/v1/market-data/store',
      '/api/v1/market-data/jobs/online-1',
    ])
    expect(fetchMock.mock.calls[0][1]).toMatchObject({
      method: 'POST',
      body: JSON.stringify({ universe: 'all', bars: 800 }),
    })
  })

  it('reads online market-data provider capabilities', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      new Response(JSON.stringify({
        data: {
          default_provider: 'eastmoney',
          instrument_count: 5219,
          providers: {
            eastmoney: { provider: 'eastmoney', available: true, version: 'builtin-http-1' },
            tdx: { provider: 'tdx', available: true },
          },
        },
      }), { status: 200 }),
    )

    await expect(fetchMarketDataCapabilities()).resolves.toMatchObject({
      default_provider: 'eastmoney',
      instrument_count: 5219,
    })
  })

  it('starts an East Money full-history backfill', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      new Response(JSON.stringify({ data: { job_id: 'backfill-1', status: 'queued' } }), { status: 202 }),
    )

    await expect(createOnlineBackfill({ universe: 'sh', boards: ['main'] })).resolves.toMatchObject({
      job_id: 'backfill-1',
    })
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/market-data/backfill',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ provider: 'eastmoney', universe: 'sh', boards: ['main'] }),
      }),
    )
  })

  it('lists active market-data jobs and cancels one job', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(new Response(JSON.stringify({
        data: [{
          job_id: 'sync-1',
          description: '行情同步',
          status: 'running',
          progress: 0.42,
          total_candidates: 100,
          total_scanned: 42,
          errors: 0,
          error: null,
          result: null,
        }],
      }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        data: { job_id: 'sync-1', status: 'cancelling', cancel_requested: true },
      }), { status: 200 }))

    await expect(fetchActiveMarketDataJobs()).resolves.toHaveLength(1)
    await expect(cancelMarketDataJob('sync-1')).resolves.toMatchObject({ status: 'cancelling' })

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      '/api/v1/market-data/jobs/active',
      '/api/v1/market-data/jobs/sync-1/cancel',
    ])
  })
})
