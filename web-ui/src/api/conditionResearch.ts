import { FormulaScreenApiError } from './formulaScreen'
import type {
  BacktestHistoryRecord,
  BacktestHistorySummary,
  ConditionResearchJobState,
  ConditionResearchPayload,
  ConditionResearchReport,
} from '../types'

const API_BASE = '/api/v1/condition-research'

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
    throw new FormulaScreenApiError('无法连接买卖条件研究服务，请确认后端已启动。', 0)
  }
  let payload: T & ApiErrorPayload
  try {
    payload = await response.json() as T & ApiErrorPayload
  } catch {
    throw new FormulaScreenApiError(
      '买卖条件研究服务返回无效响应（HTTP ' + response.status + '）。',
      response.status,
    )
  }
  if (!response.ok) {
    throw new FormulaScreenApiError(
      payload.error?.message ?? payload.detail ?? '买卖条件研究请求失败，请检查配置后重试。',
      response.status,
    )
  }
  return payload as T
}

export async function createConditionResearch(
  payload: ConditionResearchPayload,
): Promise<{ job_id: string; status: string }> {
  const response = await request<{ data: { job_id: string; status: string } }>(API_BASE, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  return response.data
}

export async function getConditionResearch(jobId: string): Promise<ConditionResearchJobState> {
  const response = await request<{ data: ConditionResearchJobState }>(
    API_BASE + '/' + encodeURIComponent(jobId),
  )
  return response.data
}

export async function cancelConditionResearch(jobId: string): Promise<{ cancelled: boolean }> {
  const response = await request<{ data: { cancelled: boolean } }>(
    API_BASE + '/' + encodeURIComponent(jobId) + '/cancel',
    { method: 'POST' },
  )
  return response.data
}

export async function getConditionResearchResults(
  jobId: string,
): Promise<ConditionResearchReport> {
  const response = await request<{ data: ConditionResearchReport }>(
    API_BASE + '/' + encodeURIComponent(jobId) + '/results',
  )
  return response.data
}

export async function fetchConditionResearchHistory(): Promise<BacktestHistorySummary[]> {
  const response = await request<{ data: BacktestHistorySummary[] }>(API_BASE + '/history')
  return response.data
}

export async function getConditionResearchHistory(
  historyId: string,
): Promise<BacktestHistoryRecord<ConditionResearchReport>> {
  const response = await request<{
    data: BacktestHistoryRecord<ConditionResearchReport>
  }>(API_BASE + '/history/' + encodeURIComponent(historyId))
  return response.data
}

export async function deleteConditionResearchHistory(historyId: string): Promise<boolean> {
  const response = await request<{ data: { deleted: boolean } }>(
    API_BASE + '/history/' + encodeURIComponent(historyId),
    { method: 'DELETE' },
  )
  return response.data.deleted
}
