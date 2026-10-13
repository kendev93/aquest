
import { ApiError, createRequestClient } from './client'
import type {
  BacktestHistoryRecord,
  BacktestHistorySummary,
  PortfolioBacktestJobState,
  PortfolioBacktestPayload,
  PortfolioBacktestResult,
} from '../types'

const API_BASE = '/api/v1/portfolio-backtests'

const request = createRequestClient({
  errorClass: ApiError,
  messages: { fallback: '请求失败，请检查组合回测配置后重试。' },
})

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

export async function cancelPortfolioBacktest(
  jobId: string,
): Promise<{ job_id: string; status: string; cancel_requested: boolean }> {
  const response = await request<{
    data: { job_id: string; status: string; cancel_requested: boolean }
  }>(`${API_BASE}/${encodeURIComponent(jobId)}/cancel`, { method: 'POST' })
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
