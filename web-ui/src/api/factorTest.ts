
import { ApiError, createRequestClient } from './client'
import type {
  BacktestHistoryRecord,
  BacktestHistorySummary,
  FactorTestJobState,
  FactorTestPayload,
  FactorTestReport,
} from '../types'

const API_BASE = '/api/v1/factor-test'

const request = createRequestClient({
  errorClass: ApiError,
  messages: { fallback: '请求失败，请检查因子检验配置后重试。' },
})

export async function createFactorTest(
  payload: FactorTestPayload,
): Promise<{ job_id: string; status: string }> {
  const response = await request<{ data: { job_id: string; status: string } }>(API_BASE, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  return response.data
}

export async function getFactorTest(jobId: string): Promise<FactorTestJobState> {
  const response = await request<{ data: FactorTestJobState }>(
    `${API_BASE}/${encodeURIComponent(jobId)}`,
  )
  return response.data
}

export async function getFactorTestResults(jobId: string): Promise<FactorTestReport> {
  const response = await request<{ data: FactorTestReport }>(
    `${API_BASE}/${encodeURIComponent(jobId)}/results`,
  )
  return response.data
}

export async function fetchFactorTestHistory(): Promise<BacktestHistorySummary[]> {
  const response = await request<{ data: BacktestHistorySummary[] }>(`${API_BASE}/history`)
  return response.data
}

export async function getFactorTestHistory(
  historyId: string,
): Promise<BacktestHistoryRecord<FactorTestReport>> {
  const response = await request<{ data: BacktestHistoryRecord<FactorTestReport> }>(
    `${API_BASE}/history/${encodeURIComponent(historyId)}`,
  )
  return response.data
}

export async function deleteFactorTestHistory(
  historyId: string,
): Promise<{ deleted: boolean }> {
  const response = await request<{ data: { deleted: boolean } }>(
    `${API_BASE}/history/${encodeURIComponent(historyId)}`,
    { method: 'DELETE' },
  )
  return response.data
}
