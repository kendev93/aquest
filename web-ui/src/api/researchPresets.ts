import { FormulaScreenApiError } from './formulaScreen'
import type {
  ConditionResearchPayload,
  ResearchPreset,
  ResearchPresetSummary,
} from '../types'

const API_BASE = '/api/v1/research-presets'

interface ApiErrorPayload {
  error?: { message?: string }
  detail?: string
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(url, {
      ...init,
      headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
    })
  } catch {
    throw new FormulaScreenApiError('无法连接研究方案服务，请确认后端已启动。', 0)
  }
  let payload: T & ApiErrorPayload
  try {
    payload = await response.json() as T & ApiErrorPayload
  } catch {
    throw new FormulaScreenApiError(
      '研究方案服务返回无效响应（HTTP ' + response.status + '）。',
      response.status,
    )
  }
  if (!response.ok) {
    throw new FormulaScreenApiError(
      payload.error?.message ?? payload.detail ?? '研究方案请求失败，请稍后重试。',
      response.status,
    )
  }
  return payload as T
}

export async function fetchResearchPresets(): Promise<ResearchPresetSummary[]> {
  const response = await request<{ data: ResearchPresetSummary[] }>(API_BASE)
  return response.data
}

export async function getResearchPreset(presetId: string): Promise<ResearchPreset> {
  const response = await request<{ data: ResearchPreset }>(
    API_BASE + '/' + encodeURIComponent(presetId),
  )
  return response.data
}

export async function createResearchPreset(payload: {
  name: string
  note?: string
  payload: ConditionResearchPayload
}): Promise<ResearchPreset> {
  const response = await request<{ data: ResearchPreset }>(API_BASE, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  return response.data
}

export async function updateResearchPreset(
  presetId: string,
  payload: { name?: string; note?: string; payload?: ConditionResearchPayload },
): Promise<ResearchPreset> {
  const response = await request<{ data: ResearchPreset }>(
    API_BASE + '/' + encodeURIComponent(presetId),
    { method: 'PATCH', body: JSON.stringify(payload) },
  )
  return response.data
}

export async function deleteResearchPreset(presetId: string): Promise<{ deleted: boolean }> {
  const response = await request<{ data: { deleted: boolean } }>(
    API_BASE + '/' + encodeURIComponent(presetId),
    { method: 'DELETE' },
  )
  return response.data
}
