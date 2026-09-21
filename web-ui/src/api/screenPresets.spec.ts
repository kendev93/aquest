import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  createScreenPreset,
  deleteScreenPreset,
  fetchScreenPresets,
  getScreenPreset,
  updateScreenPreset,
} from './screenPresets'
import type { ScreenPresetPayload } from '../types'

function ok(payload: unknown): Response {
  return new Response(JSON.stringify(payload), { status: 200 })
}

const payload: ScreenPresetPayload = {
  mode: 'preset',
  rule_mode: 'conditions',
  selected_signals: ['indicator_three.prepare_rally'],
  conditions: [
    { left_value: 'indicator_three.var1', operator: 'lt', right_value: null, right_constant: 5 },
  ],
  combine_mode: 'all',
  minimum_matches: null,
  universe: 'all',
  universe_file: '',
  instrument_types: [],
  boards: [],
  workers: 8,
  period: 'daily',
  formula_text: '',
  formula_parameters: {},
}

describe('screenPresets API', () => {
  beforeEach(() => vi.restoreAllMocks())

  it('lists presets from the preset endpoint', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(ok({ data: [{ preset_id: 'p1', name: '低位' }] }))

    await expect(fetchScreenPresets()).resolves.toMatchObject([{ preset_id: 'p1' }])
    expect(fetchMock).toHaveBeenCalledWith('/api/v1/screen-presets', expect.anything())
  })

  it('reads one preset with its payload', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      ok({ data: { preset_id: 'p1', payload } }),
    )

    await expect(getScreenPreset('p1')).resolves.toMatchObject({ preset_id: 'p1' })
  })

  it('posts a new preset', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(ok({ data: { preset_id: 'p1', name: '低位' } }))

    await createScreenPreset({ name: '低位', payload })

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/screen-presets',
      expect.objectContaining({ method: 'POST' }),
    )
  })

  it('patches a preset with a new name', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(ok({ data: { preset_id: 'p1', name: '乙' } }))

    await updateScreenPreset('p1', { name: '乙' })

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/screen-presets/p1',
      expect.objectContaining({ method: 'PATCH' }),
    )
  })

  it('deletes a preset', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(ok({ data: { deleted: true } }))

    await expect(deleteScreenPreset('p1')).resolves.toMatchObject({ deleted: true })
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/screen-presets/p1',
      expect.objectContaining({ method: 'DELETE' }),
    )
  })

  it('surfaces the server message when saving fails', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      new Response(JSON.stringify({ error: { message: '已存在同名方案: 低位' } }), {
        status: 422,
      }),
    )

    await expect(createScreenPreset({ name: '低位', payload })).rejects.toThrow('已存在同名方案')
  })
})
