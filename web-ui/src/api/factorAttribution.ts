
import { ApiError, createRequestClient } from './client'
import type {
  AttributionFactor,
  BacktestHistoryRecord,
  BacktestHistorySummary,
  FactorAttributionJobState,
  FactorAttributionPayload,
  FactorAttributionReport,
} from '../types'

const API_BASE = '/api/v1/factor-attribution'

const request = createRequestClient({
  errorClass: ApiError,
  messages: { fallback: '请求失败，请检查因子归因配置后重试。' },
})

export async function createFactorAttribution(
  payload: FactorAttributionPayload,
): Promise<{ job_id: string; status: string }> {
  const response = await request<{ data: { job_id: string; status: string } }>(API_BASE, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  return response.data
}

export async function getFactorAttribution(jobId: string): Promise<FactorAttributionJobState> {
  const response = await request<{ data: FactorAttributionJobState }>(
    `${API_BASE}/${encodeURIComponent(jobId)}`,
  )
  return response.data
}

export async function getFactorAttributionResults(
  jobId: string,
): Promise<FactorAttributionReport> {
  const response = await request<{ data: FactorAttributionReport }>(
    `${API_BASE}/${encodeURIComponent(jobId)}/results`,
  )
  return response.data
}

/** 可归因的回测记录：结果里带成交明细的那些。 */
export async function fetchAttributionSources(): Promise<BacktestHistorySummary[]> {
  const response = await request<{ data: BacktestHistorySummary[] }>(`${API_BASE}/sources`)
  return response.data
}

export async function fetchFactorAttributionHistory(): Promise<BacktestHistorySummary[]> {
  const response = await request<{ data: BacktestHistorySummary[] }>(`${API_BASE}/history`)
  return response.data
}

export async function getFactorAttributionHistory(
  historyId: string,
): Promise<BacktestHistoryRecord<FactorAttributionReport>> {
  const response = await request<{ data: BacktestHistoryRecord<FactorAttributionReport> }>(
    `${API_BASE}/history/${encodeURIComponent(historyId)}`,
  )
  return response.data
}

export async function deleteFactorAttributionHistory(
  historyId: string,
): Promise<{ deleted: boolean }> {
  const response = await request<{ data: { deleted: boolean } }>(
    `${API_BASE}/history/${encodeURIComponent(historyId)}`,
    { method: 'DELETE' },
  )
  return response.data
}

export type { AttributionFactor }
