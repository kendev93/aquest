import { FormulaScreenApiError } from './formulaScreen'
import type {
  ScreenPreset,
  ScreenPresetPayload,
  ScreenPresetSummary,
} from '../types'

const API_BASE = '/api/v1/screen-presets'

interface ApiErrorPayload {
  error?: { message?: string }
  detail?: string
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  })
  const payload = (await response.json()) as T & ApiErrorPayload
  if (!response.ok) {
    throw new FormulaScreenApiError(
      payload.detail ?? payload.error?.message ?? '筛选方案请求失败，请稍后重试。',
      response.status,
    )
  }
  return payload as T
}

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
