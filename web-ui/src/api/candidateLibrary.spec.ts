import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  deleteCandidate,
  fetchCandidates,
  fetchPublishPreview,
  importCandidates,
  publishCandidate,
  updateCandidateStatus,
} from './candidateLibrary'

function ok(payload: unknown): Response {
  return new Response(JSON.stringify(payload), { status: 200 })
}

describe('candidateLibrary API', () => {
  beforeEach(() => vi.restoreAllMocks())

  it('lists candidates without a status filter', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(ok({ data: { items: [{ candidate_id: 'c1' }] } }))

    await expect(fetchCandidates()).resolves.toMatchObject([{ candidate_id: 'c1' }])
    expect(fetchMock).toHaveBeenCalledWith('/api/v1/candidate-library', expect.anything())
  })

  it('appends the status filter as a query', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(ok({ data: { items: [] } }))

    await fetchCandidates('adopted')

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/candidate-library?status=adopted',
      expect.anything(),
    )
  })

  it('imports candidates from one study', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(ok({ data: { added: [{ candidate_id: 'c1' }], skipped: [] } }))

    await expect(
      importCandidates({ history_id: 'h1', candidate_ids: ['buy-1'] }),
    ).resolves.toMatchObject({ added: [{ candidate_id: 'c1' }] })
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/candidate-library/import',
      expect.objectContaining({ method: 'POST' }),
    )
  })

  it('patches the adoption status', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(ok({ data: { candidate_id: 'c1', status: 'adopted' } }))

    await expect(updateCandidateStatus('c1', { status: 'adopted' })).resolves.toMatchObject({
      status: 'adopted',
    })
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/candidate-library/c1',
      expect.objectContaining({ method: 'PATCH' }),
    )
  })

  it('deletes a candidate', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(ok({ data: { deleted: true } }))

    await expect(deleteCandidate('c1')).resolves.toEqual({ deleted: true })
  })

  it('reads the publish preview before writing anything', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(ok({ data: { candidate_id: 'c1', ready: true } }))

    await expect(fetchPublishPreview('c1')).resolves.toMatchObject({ ready: true })
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/candidate-library/c1/publish-preview',
      expect.anything(),
    )
  })

  it('publishes with the overrides the user gave', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(ok({ data: { online: false, message: '已发布' } }))

    const outcome = await publishCandidate('c1', { name: '甲策略', stop_loss_pct: 0.1 })

    expect(outcome.online).toBe(false)
    const init = fetchMock.mock.calls[0][1] as RequestInit
    expect(JSON.parse(String(init.body))).toMatchObject({ name: '甲策略', stop_loss_pct: 0.1 })
  })

  it('surfaces the server refusal message', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: '发布前必须先采纳' }), { status: 409 }),
    )

    await expect(publishCandidate('c1', { name: '甲策略' })).rejects.toThrow('发布前必须先采纳')
  })
})
