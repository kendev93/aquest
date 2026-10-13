
import { ApiError, createRequestClient } from './client'
import type {
  BacktestHistoryRecord,
  BacktestHistorySummary,
  StrategyFitnessJobState,
  StrategyFitnessPayload,
  StrategyFitnessReport,
} from '../types'

const API_BASE = '/api/v1/strategy-fitness'

const request = createRequestClient({
  errorClass: ApiError,
  messages: { fallback: '请求失败，请检查策略适配性配置后重试。' },
})

export async function createStrategyFitness(payload: StrategyFitnessPayload): Promise<{ job_id: string; status: string }> {
  const response = await request<{ data: { job_id: string; status: string } }>(API_BASE, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  return response.data
}

export async function getStrategyFitness(jobId: string): Promise<StrategyFitnessJobState> {
  const response = await request<{ data: StrategyFitnessJobState }>(`${API_BASE}/${encodeURIComponent(jobId)}`)
  return response.data
}

export async function getStrategyFitnessResults(jobId: string): Promise<StrategyFitnessReport> {
  const response = await request<{ data: StrategyFitnessReport }>(`${API_BASE}/${encodeURIComponent(jobId)}/results`)
  return response.data
}

export async function fetchStrategyFitnessHistory(): Promise<BacktestHistorySummary[]> {
  const response = await request<{ data: BacktestHistorySummary[] }>(`${API_BASE}/history`)
  return response.data
}

export async function getStrategyFitnessHistory(historyId: string): Promise<BacktestHistoryRecord<StrategyFitnessReport>> {
  const response = await request<{ data: BacktestHistoryRecord<StrategyFitnessReport> }>(`${API_BASE}/history/${encodeURIComponent(historyId)}`)
  return response.data
}

export async function deleteStrategyFitnessHistory(historyId: string): Promise<{ deleted: boolean }> {
  const response = await request<{ data: { deleted: boolean } }>(`${API_BASE}/history/${encodeURIComponent(historyId)}`, {
    method: 'DELETE',
  })
  return response.data
}
