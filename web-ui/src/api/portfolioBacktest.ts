import { FormulaScreenApiError } from './formulaScreen'
import type {
  BacktestHistoryRecord,
  BacktestHistorySummary,
  PortfolioBacktestJobState,
  PortfolioBacktestPayload,
  PortfolioBacktestResult,
} from '../types'

const API_BASE = '/api/v1/portfolio-backtests'

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
      payload.error?.message ?? payload.detail ?? '请求失败，请检查组合回测配置后重试。',
      response.status,
    )
  }
  return payload as T
}

export async function createPortfolioBacktest(payload: PortfolioBacktestPayload): Promise<{ job_id: string; status: string }> {
  const response = await request<{ data: { job_id: string; status: string } }>(API_BASE, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  return response.data
}

export async function getPortfolioBacktest(jobId: string): Promise<PortfolioBacktestJobState> {
  const response = await request<{ data: PortfolioBacktestJobState }>(`${API_BASE}/${encodeURIComponent(jobId)}`)
  return response.data
}

export async function getPortfolioBacktestResults(jobId: string): Promise<PortfolioBacktestResult> {
  const response = await request<{ data: PortfolioBacktestResult }>(`${API_BASE}/${encodeURIComponent(jobId)}/results`)
  return response.data
}

export async function fetchPortfolioBacktestHistory(): Promise<BacktestHistorySummary[]> {
  const response = await request<{ data: BacktestHistorySummary[] }>(`${API_BASE}/history`)
  return response.data
}

export async function getPortfolioBacktestHistory(historyId: string): Promise<BacktestHistoryRecord<PortfolioBacktestResult>> {
  const response = await request<{ data: BacktestHistoryRecord<PortfolioBacktestResult> }>(`${API_BASE}/history/${encodeURIComponent(historyId)}`)
  return response.data
}

export async function deletePortfolioBacktestHistory(historyId: string): Promise<{ deleted: boolean }> {
  const response = await request<{ data: { deleted: boolean } }>(`${API_BASE}/history/${encodeURIComponent(historyId)}`, {
    method: 'DELETE',
  })
  return response.data
}
