import { beforeEach, describe, expect, it, vi } from 'vitest'

import { compareHistory, fetchHistoryCompareCandidates } from './historyCompare'

describe('history compare API', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('reads the candidate list from its own path', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ data: { items: [], types: [], max_items: 5 } }), { status: 200 }),
    )

    await expect(fetchHistoryCompareCandidates()).resolves.toMatchObject({ max_items: 5 })

    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/history-compare/candidates')
  })

  it('posts the selection with the normalization and axis mode', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ data: { items: [], warnings: [] } }), { status: 200 }),
    )

    await compareHistory({ history_ids: ['a', 'b'], normalize: 'first', x_axis: 'index' })

    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/v1/history-compare')
    expect(String(init?.method)).toBe('POST')
    expect(JSON.parse(String(init?.body))).toEqual({
      history_ids: ['a', 'b'],
      normalize: 'first',
      x_axis: 'index',
    })
  })

  it('surfaces the server message when a record is gone', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ error: { message: '以下历史记录不存在: nope' } }), { status: 422 }),
    )

    await expect(compareHistory({ history_ids: ['a', 'nope'] })).rejects.toThrow('nope')
  })
})
