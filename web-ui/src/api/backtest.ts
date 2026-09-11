import { FormulaScreenApiError } from './formulaScreen'
import type {
  BacktestHistoryRecord,
  BacktestHistorySummary,
  BacktestJobState,
  BacktestPayload,
  BacktestResult,
} from '../types'

const API_BASE = '/api/v1/backtests'

interface ApiErrorPayload {
  error?: { message?: string }
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  })
  const payload = (await response.json()) as T & ApiErrorPayload
  if (!response.ok) {
    throw new FormulaScreenApiError(
      payload.error?.message ?? '请求失败，请检查回测配置后重试。',
      response.status,
    )
  }
  return payload as T
}

export async function createBacktest(payload: BacktestPayload): Promise<{ job_id: string; status: string }> {
  const response = await request<{ data: { job_id: string; status: string } }>(API_BASE, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  return response.data
}

export async function getBacktest(jobId: string): Promise<BacktestJobState> {
  const response = await request<{ data: BacktestJobState }>(`${API_BASE}/${encodeURIComponent(jobId)}`)
  return response.data
}

export async function getBacktestResults(jobId: string): Promise<BacktestResult> {
  const response = await request<{ data: BacktestResult }>(`${API_BASE}/${encodeURIComponent(jobId)}/results`)
  return response.data
}

export async function fetchBacktestHistory(): Promise<BacktestHistorySummary[]> {
  const response = await request<{ data: BacktestHistorySummary[] }>(`${API_BASE}/history`)
  return response.data
}

export async function getBacktestHistory(historyId: string): Promise<BacktestHistoryRecord<BacktestResult>> {
  const response = await request<{ data: BacktestHistoryRecord<BacktestResult> }>(`${API_BASE}/history/${encodeURIComponent(historyId)}`)
  return response.data
}

export async function deleteBacktestHistory(historyId: string): Promise<{ deleted: boolean }> {
  const response = await request<{ data: { deleted: boolean } }>(`${API_BASE}/history/${encodeURIComponent(historyId)}`, {
    method: 'DELETE',
  })
  return response.data
}
