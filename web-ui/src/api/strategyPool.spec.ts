import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  createStrategyPoolJob,
  fetchStrategyPoolEntries,
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
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ data: { items: [{ market: 'SH', code: '600000' }] } }),
    })
    vi.stubGlobal('fetch', fetch)

    const entries = await fetchStrategyPoolEntries('sharpe')

    expect(fetch.mock.calls[0]?.[0]).toBe('/api/v1/strategy-pool/entries?objective=sharpe')
    expect(entries[0]?.code).toBe('600000')
  })

  it('submits asynchronous signal scans and returns their durable task id', async () => {
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ data: { job_id: 'pool-job-1', status: 'queued' } }),
    })
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
      .mockResolvedValueOnce({ ok: true, json: async () => ({ data: { job_id: 'j1', status: 'completed', result: { mode: 'single' } } }) })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ data: { code: '600000' } }) })
    vi.stubGlobal('fetch', fetch)

    const job = await getStrategyPoolJob('j1')
    await saveStrategyPoolOverride('SH', '600000', 'annual_return', candidate)

    expect(job.result?.mode).toBe('single')
    expect(fetch.mock.calls[1]?.[0]).toBe('/api/v1/strategy-pool/entries/SH/600000/overrides/annual_return')
    expect(JSON.parse(String(fetch.mock.calls[1]?.[1]?.body))).toEqual({ candidate })
  })
})
