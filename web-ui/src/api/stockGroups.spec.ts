import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  createStockGroup,
  deleteStockGroup,
  fetchStockGroupPerformance,
  fetchStockGroups,
  fetchStockGroupsSummary,
  getStockGroup,
  removeStockGroupMembers,
  renameStockGroup,
} from './stockGroups'
import type { StockGroupMember } from '../types'

function ok(data: unknown): Response {
  return new Response(JSON.stringify({ data }), { status: 200 })
}

afterEach(() => vi.unstubAllGlobals())

describe('stock groups API', () => {
  it('covers group CRUD, summary, membership and performance endpoints', async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(ok([]))
      .mockResolvedValueOnce(ok({ group_id: 'g1' }))
      .mockResolvedValueOnce(ok({ group_id: 'g/1' }))
      .mockResolvedValueOnce(ok({ group_id: 'g/1', name: '新名称' }))
      .mockResolvedValueOnce(ok({ as_of: '2026-10-01', groups: [] }))
      .mockResolvedValueOnce(ok({ group_id: 'g/1', items: [] }))
      .mockResolvedValueOnce(ok({ deleted: true }))
      .mockResolvedValueOnce(ok({ group_id: 'g/1', excess_return: 0.1 }))
    vi.stubGlobal('fetch', fetch)
    const members: StockGroupMember[] = [{ market: 'SH', code: '600000', name: '浦发银行' }]

    await expect(fetchStockGroups()).resolves.toEqual([])
    await createStockGroup({ name: '观察组', items: members })
    await expect(getStockGroup('g/1')).resolves.toMatchObject({ group_id: 'g/1' })
    await renameStockGroup('g/1', '新名称')
    await expect(fetchStockGroupsSummary([20, 60])).resolves.toMatchObject({ as_of: '2026-10-01' })
    await removeStockGroupMembers('g/1', members)
    await expect(deleteStockGroup('g/1')).resolves.toBe(true)
    await expect(fetchStockGroupPerformance('g/1')).resolves.toMatchObject({ excess_return: 0.1 })

    expect(fetch.mock.calls.map(([url, init]) => [url, init?.method ?? 'GET'])).toEqual([
      ['/api/v1/stock-groups', 'GET'],
      ['/api/v1/stock-groups', 'POST'],
      ['/api/v1/stock-groups/g%2F1', 'GET'],
      ['/api/v1/stock-groups/g%2F1', 'PATCH'],
      ['/api/v1/stock-groups/summary?windows=20,60', 'GET'],
      ['/api/v1/stock-groups/g%2F1/members/remove', 'POST'],
      ['/api/v1/stock-groups/g%2F1', 'DELETE'],
      ['/api/v1/stock-groups/g%2F1/performance', 'GET'],
    ])
  })
})
