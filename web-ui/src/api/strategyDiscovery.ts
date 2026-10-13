
import { ApiError, createRequestClient } from './client'
import type {
  StrategyDiscoveryLeaderboard,
  StrategyDiscoveryJobState,
  StrategyDiscoveryPayload,
  StrategyDiscoveryRefinementPayload,
  StrategyDiscoveryResult,
  PortfolioRiskOptimizationPayload,
  RiskOptimizationHistoryPage,
  StrategyReplayHistoryRecord,
  StrategyReplayJobState,
  StrategyReplayResult,
} from '../types'

const API_BASE = '/api/v1/strategy-discovery'

const MAX_GET_ATTEMPTS = 5
const RETRYABLE_GATEWAY_STATUSES = new Set([500, 502, 503, 504])
const GET_RETRY_DELAYS_MS = [100, 250, 500, 1000]

const request = createRequestClient({
  errorClass: ApiError,
  messages: { fallback: '策略实验请求失败，请稍后重试。', offline: '无法连接策略实验服务，请确认后端已启动或 Docker 容器健康。', invalidBody: (status) => (RETRYABLE_GATEWAY_STATUSES.has(status) ? `策略实验服务暂时不可用（HTTP ${status}），请稍后重试。` : `策略实验服务返回了无效响应（HTTP ${status}）。`) },
  retryGet: { attempts: MAX_GET_ATTEMPTS, retryableStatuses: RETRYABLE_GATEWAY_STATUSES, delaysMs: GET_RETRY_DELAYS_MS },
})

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

export async function createPortfolioRiskOptimization(
  payload: PortfolioRiskOptimizationPayload,
): Promise<{ job_id: string; status: string }> {
  const response = await request<{ data: { job_id: string; status: string } }>(
    `${API_BASE}/risk-optimizations`,
    {
      method: 'POST',
      body: JSON.stringify(payload),
    },
  )
  return response.data
}

export async function fetchRiskOptimizationHistory(
  limit = 20,
  offset = 0,
): Promise<RiskOptimizationHistoryPage> {
  const query = new URLSearchParams({ limit: String(limit), offset: String(offset) })
  const response = await request<{ data: RiskOptimizationHistoryPage }>(
    `${API_BASE}/risk-optimizations/history?${query.toString()}`,
  )
  return response.data
}

export async function getRiskOptimizationHistory(jobId: string): Promise<StrategyDiscoveryResult> {
  const response = await request<{ data: StrategyDiscoveryResult }>(
    `${API_BASE}/risk-optimizations/history/${encodeURIComponent(jobId)}`,
  )
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
