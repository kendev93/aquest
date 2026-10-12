import { FormulaScreenApiError } from './formulaScreen'
import type {
  IndividualResearchHistoryRecord,
  IndividualResearchHistorySummary,
  IndividualResearchJobState,
  IndividualResearchPayload,
  IndividualResearchResult,
} from '../types'

const API_BASE = '/api/v1/individual-research'

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
      payload.error?.message ?? payload.detail ?? '个股研究请求失败，请检查配置和本地行情。',
      response.status,
    )
  }
  return payload as T
}

export async function createIndividualResearch(
  payload: IndividualResearchPayload,
): Promise<{ job_id: string; status: string }> {
  const response = await request<{ data: { job_id: string; status: string } }>(API_BASE, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  return response.data
}

export async function getIndividualResearchJob(jobId: string): Promise<IndividualResearchJobState> {
  const response = await request<{ data: IndividualResearchJobState }>(
    `${API_BASE}/jobs/${encodeURIComponent(jobId)}`,
  )
  return response.data
}

export async function cancelIndividualResearch(jobId: string): Promise<{ cancelled: boolean }> {
  const response = await request<{ data: { cancelled: boolean } }>(
    `${API_BASE}/jobs/${encodeURIComponent(jobId)}/cancel`,
    { method: 'POST' },
  )
  return response.data
}

export async function fetchIndividualResearchHistory(): Promise<IndividualResearchHistorySummary[]> {
  const response = await request<{ data: IndividualResearchHistorySummary[] }>(`${API_BASE}/history`)
  return response.data
}

export async function getIndividualResearchHistory(
  historyId: string,
): Promise<IndividualResearchHistoryRecord> {
  const response = await request<{ data: IndividualResearchHistoryRecord }>(
    `${API_BASE}/history/${encodeURIComponent(historyId)}`,
  )
  return response.data
}

export async function deleteIndividualResearchHistory(
  historyId: string,
): Promise<{ deleted: boolean }> {
  const response = await request<{ data: { deleted: boolean } }>(
    `${API_BASE}/history/${encodeURIComponent(historyId)}`,
    { method: 'DELETE' },
  )
  return response.data
}

export type { IndividualResearchResult }
