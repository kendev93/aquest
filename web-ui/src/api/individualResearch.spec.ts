import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  cancelIndividualResearch,
  createIndividualResearch,
  deleteIndividualResearchHistory,
  fetchIndividualResearchHistory,
  getIndividualResearchHistory,
  getIndividualResearchJob,
} from './individualResearch'

describe('individual research API', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('creates, reads, cancels, and loads persisted single-stock research', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { job_id: 'job-1', status: 'queued' } })))
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { job_id: 'job-1', status: 'running', progress: 0.5 } })))
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { cancelled: true } })))
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: [{ history_id: 'job-1' }] })))
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { history_id: 'job-1', result: { code: '600000' } } })))
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { deleted: true } })))

    await expect(createIndividualResearch({
      market: 'SH', code: '600000', date_mode: 'recent5', start_date: null, end_date: null,
      reversal_multiple: 3, atr_window: 14, min_bars: 5, horizon: 10,
      value_ids: ['technical.rsi14'], signal_ids: [], candidate_limit: 5, risk_trials: 1000,
      initial_cash: 1_000_000, commission: 0.0001, min_commission: 0.1, stamp_tax: 0.001,
      slippage: 0, execution: 'next_open', position_mode: 'full', fixed_size: null,
    })).resolves.toEqual({ job_id: 'job-1', status: 'queued' })
    await expect(getIndividualResearchJob('job-1')).resolves.toMatchObject({ status: 'running' })
    await expect(cancelIndividualResearch('job-1')).resolves.toEqual({ cancelled: true })
    await expect(fetchIndividualResearchHistory()).resolves.toEqual([{ history_id: 'job-1' }])
    await expect(getIndividualResearchHistory('job-1')).resolves.toMatchObject({ result: { code: '600000' } })
    await expect(deleteIndividualResearchHistory('job-1')).resolves.toEqual({ deleted: true })
    expect(fetchMock).toHaveBeenNthCalledWith(1, '/api/v1/individual-research', expect.objectContaining({ method: 'POST' }))
  })
})
