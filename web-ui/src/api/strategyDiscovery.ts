import { FormulaScreenApiError } from './formulaScreen'
import type {
  StrategyDiscoveryLeaderboard,
  StrategyDiscoveryJobState,
  StrategyDiscoveryPayload,
  StrategyDiscoveryRefinementPayload,
  StrategyDiscoveryResult,
  StrategyReplayHistoryRecord,
  StrategyReplayJobState,
  StrategyReplayResult,
} from '../types'

const API_BASE = '/api/v1/strategy-discovery'

interface ErrorPayload {
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
    throw new FormulaScreenApiError('无法连接策略实验服务，请确认后端已启动或 Docker 容器健康。', 0)
  }
  let payload: T & ErrorPayload
  try {
    payload = await response.json() as T & ErrorPayload
  } catch {
    throw new FormulaScreenApiError(`策略实验服务返回了无效响应（HTTP ${response.status}）。`, response.status)
  }
  if (!response.ok) {
    throw new FormulaScreenApiError(
      payload.error?.message ?? payload.detail ?? '策略实验请求失败，请稍后重试。',
      response.status,
    )
  }
  return payload as T
}

export async function fetchStrategyDiscoveryCapabilities(): Promise<Record<string, unknown>> {
  const response = await request<{ data: Record<string, unknown> }>(`${API_BASE}/capabilities`)
  return response.data
}

export async function createStrategyDiscovery(payload: StrategyDiscoveryPayload): Promise<{ job_id: string; status: string }> {
  const response = await request<{ data: { job_id: string; status: string } }>(API_BASE, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  return response.data
}

export async function createStrategyDiscoveryRefinement(payload: StrategyDiscoveryRefinementPayload): Promise<{ job_id: string; status: string }> {
  const response = await request<{ data: { job_id: string; status: string } }>(`${API_BASE}/refinements`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  return response.data
}

export async function getStrategyDiscovery(jobId: string): Promise<StrategyDiscoveryJobState> {
  const response = await request<{ data: StrategyDiscoveryJobState }>(`${API_BASE}/${encodeURIComponent(jobId)}`)
  return response.data
}

export async function cancelStrategyDiscovery(jobId: string): Promise<{ job_id: string; status: string; cancel_requested: boolean }> {
  const response = await request<{ data: { job_id: string; status: string; cancel_requested: boolean } }>(`${API_BASE}/${encodeURIComponent(jobId)}/cancel`, {
    method: 'POST',
  })
  return response.data
}

export async function getStrategyDiscoveryResults(jobId: string): Promise<StrategyDiscoveryResult> {
  const response = await request<{ data: StrategyDiscoveryResult }>(`${API_BASE}/${encodeURIComponent(jobId)}/results`)
  return response.data
}

export async function fetchStrategyDiscoveryHistory(): Promise<Array<Record<string, unknown>>> {
  const response = await request<{ data: Array<Record<string, unknown>> }>(`${API_BASE}/history`)
  return response.data
}

export async function getStrategyDiscoveryHistory(historyId: string): Promise<StrategyDiscoveryResult & { history_id: string }> {
  const response = await request<{ data: StrategyDiscoveryResult & { history_id: string } }>(`${API_BASE}/history/${encodeURIComponent(historyId)}`)
  return response.data
}

export async function deleteStrategyDiscoveryHistory(historyId: string): Promise<{ deleted: boolean }> {
  const response = await request<{ data: { deleted: boolean } }>(`${API_BASE}/history/${encodeURIComponent(historyId)}`, {
    method: 'DELETE',
  })
  return response.data
}

export async function createStrategyReplay(
  experimentId: string,
  leaderboard: StrategyDiscoveryLeaderboard,
): Promise<{ job_id: string; status: string }> {
  const response = await request<{ data: { job_id: string; status: string } }>(
    `${API_BASE}/${encodeURIComponent(experimentId)}/replays`,
    {
      method: 'POST',
      body: JSON.stringify({ leaderboard }),
    },
  )
  return response.data
}

export async function getStrategyReplay(jobId: string): Promise<StrategyReplayJobState> {
  const response = await request<{ data: StrategyReplayJobState }>(
    `${API_BASE}/replay-jobs/${encodeURIComponent(jobId)}`,
  )
  return response.data
}

export async function getStrategyReplayResults(jobId: string): Promise<StrategyReplayResult> {
  const response = await request<{ data: StrategyReplayResult }>(
    `${API_BASE}/replay-jobs/${encodeURIComponent(jobId)}/results`,
  )
  return response.data
}

export async function cancelStrategyReplay(
  jobId: string,
): Promise<{ job_id: string; status: string; cancel_requested: boolean }> {
  const response = await request<{
    data: { job_id: string; status: string; cancel_requested: boolean }
  }>(`${API_BASE}/replay-jobs/${encodeURIComponent(jobId)}/cancel`, {
    method: 'POST',
  })
  return response.data
}

export async function getStrategyReplayHistory(
  experimentId: string,
  leaderboard: StrategyDiscoveryLeaderboard,
): Promise<StrategyReplayHistoryRecord> {
  const response = await request<{ data: StrategyReplayHistoryRecord }>(
    `${API_BASE}/history/${encodeURIComponent(experimentId)}/replays?leaderboard=${leaderboard}`,
  )
  return response.data
}
