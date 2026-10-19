import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  cancelStrategyPoolJob,
  clearStrategyPoolOverride,
  createStrategyPoolJob,
  fetchStrategyPoolEntries,
  fetchStrategyPoolJobStats,
  fetchStrategyPoolJobs,
  fetchStrategyPoolResearchEstimate,
  getStrategyPoolJob,
  saveStrategyPoolOverride,
} from './strategyPool'
import type { StrategyPoolCandidate } from '../types'

const candidate: StrategyPoolCandidate = {
  candidate_id: 'c1',
  buy_rule: { signals: ['technical.macd'] },
  sell_rule: { signals: ['technical.rsi14'] },
  config: { stop_loss_pct: 0.08 },
}

afterEach(() => vi.unstubAllGlobals())

describe('strategy pool API', () => {
  it('requests entries with the selected optimization objective', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ data: { items: [{ market: 'SH', code: '600000' }] } }), { status: 200 }))
    vi.stubGlobal('fetch', fetch)

    const entries = await fetchStrategyPoolEntries('sharpe')

    expect(fetch.mock.calls[0]?.[0]).toBe('/api/v1/strategy-pool/entries?objective=sharpe')
    expect(entries[0]?.code).toBe('600000')
  })

  it('submits asynchronous signal scans and returns their durable task id', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ data: { job_id: 'pool-job-1', status: 'queued' } }), { status: 200 }))
    vi.stubGlobal('fetch', fetch)

    const job = await createStrategyPoolJob({ mode: 'signals', objective: 'annual_return' })

    expect(JSON.parse(String(fetch.mock.calls[0]?.[1]?.body))).toEqual({
      mode: 'signals',
      objective: 'annual_return',
    })
    expect(job.job_id).toBe('pool-job-1')
  })

  it('loads completed job results and applies a mode-specific manual candidate', async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { job_id: 'j1', status: 'completed', result: { mode: 'single' } } }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { code: '600000' } }), { status: 200 }))
    vi.stubGlobal('fetch', fetch)

    const job = await getStrategyPoolJob('j1')
    const benchmark = { full_period: { total_return: 0.2 } }
    await saveStrategyPoolOverride('SH', '600000', 'annual_return', candidate, benchmark)

    expect(job.result?.mode).toBe('single')
    expect(fetch.mock.calls[1]?.[0]).toBe('/api/v1/strategy-pool/entries/SH/600000/overrides/annual_return')
    expect(JSON.parse(String(fetch.mock.calls[1]?.[1]?.body))).toEqual({ candidate, benchmark })
  })

  it('lists durable pool job summaries', async () => {
    const items = [{ job_id: 'job-1', mode: 'single', status: 'completed' }]
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ data: { items } }), { status: 200 }))
    vi.stubGlobal('fetch', fetch)

    const jobs = await fetchStrategyPoolJobs()

    expect(fetch.mock.calls[0]?.[0]).toBe('/api/v1/strategy-pool/jobs')
    expect(jobs).toEqual(items)
  })

  it('reads task duration stats from the separate stats route', async () => {
    // /jobs/stats 必须原样请求：后端把它注册在 /jobs/{job_id} 之前，写成别的路径会 404。
    const stats = { sample_limit: 60, modes: { full: { samples: 3, median_duration_seconds: 2400 } } }
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ data: stats }), { status: 200 }))
    vi.stubGlobal('fetch', fetch)

    await expect(fetchStrategyPoolJobStats()).resolves.toEqual(stats)

    expect(fetch.mock.calls[0]?.[0]).toBe('/api/v1/strategy-pool/jobs/stats')
  })

  it('cancels a pool job by its encoded task id', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ data: { cancelled: true } }), { status: 200 }))
    vi.stubGlobal('fetch', fetch)

    const result = await cancelStrategyPoolJob('job/1')

    expect(fetch.mock.calls[0]?.[0]).toBe('/api/v1/strategy-pool/jobs/job%2F1/cancel')
    expect(fetch.mock.calls[0]?.[1]?.method).toBe('POST')
    expect(result.cancelled).toBe(true)
  })

  it('clears an override with DELETE', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ data: {} }), { status: 200 }))
    vi.stubGlobal('fetch', fetch)

    await clearStrategyPoolOverride('SH', '600000', 'sharpe')

    expect(fetch.mock.calls[0]?.[0]).toBe(
      '/api/v1/strategy-pool/entries/SH/600000/overrides/sharpe',
    )
    expect(fetch.mock.calls[0]?.[1]?.method).toBe('DELETE')
  })
})

  it('posts the incremental estimate with the same payload the run button sends', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ data: { pending_symbols: 112, reusable_symbols: 4305 } }), { status: 200 }))
    vi.stubGlobal('fetch', fetch)

    const estimate = await fetchStrategyPoolResearchEstimate({ mode: 'full', rebuild: false, risk_trials: 3_000 })

    expect(estimate.pending_symbols).toBe(112)
    expect(fetch.mock.calls[0]?.[0]).toBe('/api/v1/strategy-pool/research-estimate')
    expect(JSON.parse(String(fetch.mock.calls[0]?.[1]?.body))).toEqual({
      mode: 'full',
      rebuild: false,
      risk_trials: 3000,
    })
  })
