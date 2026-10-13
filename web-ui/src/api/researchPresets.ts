
import { ApiError, createRequestClient } from './client'
import type {
  ConditionResearchPayload,
  ResearchPreset,
  ResearchPresetSummary,
} from '../types'

const API_BASE = '/api/v1/research-presets'

const request = createRequestClient({
  errorClass: ApiError,
  messages: { fallback: '研究方案请求失败，请稍后重试。', offline: '无法连接研究方案服务，请确认后端已启动。', invalidBody: (status) => `研究方案服务返回无效响应（HTTP ${status}）。` },
})

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
