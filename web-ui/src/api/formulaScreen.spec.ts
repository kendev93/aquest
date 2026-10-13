import { afterEach, describe, expect, it, vi } from 'vitest'

import { createJob, createSyncJob, fetchMetadata, fetchScreenHistory, getJob, getResults, getScreenHistory, getSyncJob, parseFormula } from './formulaScreen'
import { ApiError } from './client'

describe('formula screen API client', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('fetches metadata and unwraps the data envelope', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ data: { indicators: [] } }), { status: 200 })))

    await expect(fetchMetadata()).resolves.toEqual({ indicators: [] })
    expect(fetch).toHaveBeenCalledWith('/api/v1/formula-screen/metadata', expect.objectContaining({ headers: { 'Content-Type': 'application/json' } }))
  })

  it('parses a custom formula through the dedicated endpoint', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ data: { parameters: [{ name: 'N', default: 5 }], signals: [] } }), { status: 200 }),
    ))

    await expect(parseFormula('N:=5; SIGNAL:CROSS(C,REF(C,N));')).resolves.toMatchObject({
      parameters: [{ name: 'N', default: 5 }],
    })
    expect(fetch).toHaveBeenCalledWith('/api/v1/formula-screen/parse', expect.objectContaining({ method: 'POST' }))
  })

  it('posts a scan job and URL-encodes job ids for reads', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { job_id: 'job/1', status: 'queued' } }), { status: 202 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { job_id: 'job/1', status: 'completed' } }), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)

    const payload = { selected_signals: ['indicator_three.begin_zone'], combine_mode: 'any' as const, minimum_matches: null, universe: 'all' as const, universe_file: null, workers: 1, period: 'daily' as const, formula_text: null, formula_parameters: {} }
    await expect(createJob(payload)).resolves.toEqual({ job_id: 'job/1', status: 'queued' })
    await expect(getJob('job/1')).resolves.toMatchObject({ status: 'completed' })
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual(payload)
    expect(fetchMock.mock.calls[1][0]).toBe('/api/v1/formula-screen/jobs/job%2F1')
  })

  it('reads results and exposes structured API errors', async () => {
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: [], meta: { total_signals: 0 } }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ error: { message: '路径无效' } }), { status: 422 })))

    await expect(getResults('job-1')).resolves.toEqual({ results: [], meta: { total_signals: 0 } })
    await expect(fetchMetadata()).rejects.toEqual(expect.objectContaining({
      name: 'ApiError', message: '路径无效', status: 422,
    } satisfies Partial<ApiError>))
  })

  it('loads screen history summaries and a selected history record', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: [{ history_id: 'job-1', created_at: '2026-09-09T10:00:00', result_count: 1 }] }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { history_id: 'job-1', created_at: '2026-09-09T10:00:00', meta: { total_candidates: 1, total_scanned: 1, total_signals: 1, errors: 0, skipped: 0, failure_reasons: {}, skip_reasons: {} }, results: [] } }), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(fetchScreenHistory()).resolves.toHaveLength(1)
    await expect(getScreenHistory('job/1')).resolves.toMatchObject({ history_id: 'job-1' })
    expect(fetchMock.mock.calls[1][0]).toBe('/api/v1/formula-screen/history/job%2F1')
  })

  it('uses the fallback message when the server omits error details', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ error: {} }), { status: 503 }),
    ))

    await expect(fetchMetadata()).rejects.toMatchObject({
      name: 'ApiError', message: '请求失败，请检查配置后重试。', status: 503,
    })
  })

  it('surfaces FastAPI detail errors when the error envelope is absent', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: '任务执行器暂不可用' }), { status: 503 }),
    ))

    await expect(fetchMetadata()).rejects.toMatchObject({
      name: 'ApiError', message: '任务执行器暂不可用', status: 503,
    })
  })

  it('creates and polls a market sync job', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { job_id: 'sync-1', status: 'queued' } }), { status: 202 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { job_id: 'sync-1', status: 'completed', progress: 1, total_candidates: 1, total_scanned: 1, errors: 0, error: null, result: { written_bars: 3 } } }), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(createSyncJob()).resolves.toEqual({ job_id: 'sync-1', status: 'queued' })
    await expect(getSyncJob('sync-1')).resolves.toMatchObject({ status: 'completed', result: { written_bars: 3 } })
    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/market-data/sync')
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({})
  })
})
