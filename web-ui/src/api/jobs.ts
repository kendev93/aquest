import { FormulaScreenApiError } from './formulaScreen'
import type { ActiveJobState } from '../types'

const API_BASE = '/api/v1/jobs'

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
      payload.error?.message ?? payload.detail ?? '任务状态读取失败，请稍后重试。',
      response.status,
    )
  }
  return payload as T
}

export async function fetchActiveJobs(): Promise<ActiveJobState[]> {
  const response = await request<{ data: ActiveJobState[] }>(`${API_BASE}/active`)
  return response.data
}
