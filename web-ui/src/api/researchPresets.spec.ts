import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  createResearchPreset,
  deleteResearchPreset,
  fetchResearchPresets,
  getResearchPreset,
  updateResearchPreset,
} from './researchPresets'
import type { ConditionResearchPayload } from '../types'

const payload = {
  universe: 'all',
  buy_rule: null,
  sell_rule: null,
} as ConditionResearchPayload

function ok(data: unknown): Response {
  return new Response(JSON.stringify({ data }), { status: 200 })
}

afterEach(() => vi.unstubAllGlobals())

describe('research presets API', () => {
  it('covers list, detail, create, update and delete endpoints', async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(ok([{ preset_id: 'p1', name: '研究方案' }]))
      .mockResolvedValueOnce(ok({ preset_id: 'p/1', payload }))
      .mockResolvedValueOnce(ok({ preset_id: 'p2', name: '新方案', payload }))
      .mockResolvedValueOnce(ok({ preset_id: 'p/1', name: '改名', payload }))
      .mockResolvedValueOnce(ok({ deleted: true }))
    vi.stubGlobal('fetch', fetch)

    await expect(fetchResearchPresets()).resolves.toHaveLength(1)
    await expect(getResearchPreset('p/1')).resolves.toMatchObject({ preset_id: 'p/1' })
    await createResearchPreset({ name: '新方案', payload })
    await updateResearchPreset('p/1', { name: '改名' })
    await expect(deleteResearchPreset('p/1')).resolves.toEqual({ deleted: true })

    expect(fetch.mock.calls.map(([url, init]) => [url, init?.method ?? 'GET'])).toEqual([
      ['/api/v1/research-presets', 'GET'],
      ['/api/v1/research-presets/p%2F1', 'GET'],
      ['/api/v1/research-presets', 'POST'],
      ['/api/v1/research-presets/p%2F1', 'PATCH'],
      ['/api/v1/research-presets/p%2F1', 'DELETE'],
    ])
  })

  it('preserves network and server errors', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValueOnce(new Error('offline')))
    await expect(fetchResearchPresets()).rejects.toThrow('无法连接研究方案服务')

    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: '方案名称重复' }), { status: 422 }),
    ))
    await expect(fetchResearchPresets()).rejects.toThrow('方案名称重复')
  })
})
