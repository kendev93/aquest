import { describe, expect, it, vi } from 'vitest'

import { createStrategyDiscoveryRefinement } from './strategyDiscovery'

describe('strategy discovery refinement API', () => {
  it('posts the parent experiment and candidate identity', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      new Response(JSON.stringify({ data: { job_id: 'refine-1', status: 'queued' } }), { status: 202 }),
    )

    await expect(createStrategyDiscoveryRefinement({
      parent_history_id: 'history-1',
      parent_candidate_hash: 'candidate-1',
      refinement_radius: 'medium',
      max_trials: 2000,
    })).resolves.toEqual({ job_id: 'refine-1', status: 'queued' })

    expect(fetchMock).toHaveBeenCalledWith('/api/v1/strategy-discovery/refinements', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({
        parent_history_id: 'history-1',
        parent_candidate_hash: 'candidate-1',
        refinement_radius: 'medium',
        max_trials: 2000,
      }),
    }))
    fetchMock.mockRestore()
  })
})
