import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  cancelConditionResearch,
  createConditionResearch,
  fetchConditionResearchHistory,
  getConditionResearch,
  getConditionResearchHistory,
  getConditionResearchResults,
} from './conditionResearch'

describe('condition research API', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('creates, polls, cancels and retrieves a condition research job', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { job_id: 'research-1', status: 'queued' } }), { status: 202 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { job_id: 'research-1', status: 'running' } }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { cancelled: true } }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { candidates: [] } }), { status: 200 }))
    const payload = {
      universe: 'all' as const,
      buy_rule: { signals: ['indicator_three.begin_zone'], conditions: [], combine_mode: 'all' as const, minimum_matches: null },
      sell_rule: { signals: ['indicator_three.end_zone'], conditions: [], combine_mode: 'all' as const, minimum_matches: null },
      date_mode: 'recent5' as const,
    }

    await expect(createConditionResearch(payload)).resolves.toEqual({ job_id: 'research-1', status: 'queued' })
    await expect(getConditionResearch('research-1')).resolves.toMatchObject({ status: 'running' })
    await expect(cancelConditionResearch('research-1')).resolves.toEqual({ cancelled: true })
    await expect(getConditionResearchResults('research-1')).resolves.toEqual({ candidates: [] })
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      '/api/v1/condition-research',
      '/api/v1/condition-research/research-1',
      '/api/v1/condition-research/research-1/cancel',
      '/api/v1/condition-research/research-1/results',
    ])
  })

  it('loads saved condition research history', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: [{ history_id: 'history-1' }] }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { history_id: 'history-1', result: { candidates: [] } } }), { status: 200 }))

    await expect(fetchConditionResearchHistory()).resolves.toEqual([{ history_id: 'history-1' }])
    await expect(getConditionResearchHistory('history-1')).resolves.toMatchObject({ history_id: 'history-1' })
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      '/api/v1/condition-research/history',
      '/api/v1/condition-research/history/history-1',
    ])
  })
})
