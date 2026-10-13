
import { ApiError, createRequestClient } from './client'
import type {
  ScreenPreset,
  ScreenPresetPayload,
  ScreenPresetSummary,
} from '../types'

const API_BASE = '/api/v1/screen-presets'

const request = createRequestClient({
  errorClass: ApiError,
  messages: { fallback: '筛选方案请求失败，请稍后重试。' },
})

export async function fetchScreenPresets(): Promise<ScreenPresetSummary[]> {
  const response = await request<{ data: ScreenPresetSummary[] }>(API_BASE)
  return response.data
}

export async function getScreenPreset(presetId: string): Promise<ScreenPreset> {
  const response = await request<{ data: ScreenPreset }>(
    `${API_BASE}/${encodeURIComponent(presetId)}`,
  )
  return response.data
}

export async function createScreenPreset(payload: {
  name: string
  note?: string
  payload: ScreenPresetPayload
}): Promise<ScreenPreset> {
  const response = await request<{ data: ScreenPreset }>(API_BASE, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  return response.data
}

export async function updateScreenPreset(
  presetId: string,
  payload: { name?: string; note?: string; payload?: ScreenPresetPayload },
): Promise<ScreenPreset> {
  const response = await request<{ data: ScreenPreset }>(
    `${API_BASE}/${encodeURIComponent(presetId)}`,
    {
      method: 'PATCH',
      body: JSON.stringify(payload),
    },
  )
  return response.data
}

export async function deleteScreenPreset(presetId: string): Promise<{ deleted: boolean }> {
  const response = await request<{ data: { deleted: boolean } }>(
    `${API_BASE}/${encodeURIComponent(presetId)}`,
    { method: 'DELETE' },
  )
  return response.data
}
