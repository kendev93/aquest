import { FormulaScreenApiError } from './formulaScreen'
import type {
  PortfolioBacktestPayload,
  SavedStrategyDetail,
  SavedStrategySummary,
  StrategyTrackingJobState,
  StrategyTrackingSnapshot,
  TrackingTradePage,
} from '../types'

const API_BASE = '/api/v1/strategies'

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
      payload.detail ?? payload.error?.message ?? '策略库请求失败，请稍后重试。',
      response.status,
    )
  }
  return payload as T
}

export async function fetchStrategies(): Promise<SavedStrategySummary[]> {
  const response = await request<{ data: SavedStrategySummary[] }>(API_BASE)
  return response.data
}

export async function getStrategy(strategyId: string): Promise<SavedStrategyDetail> {
  const response = await request<{ data: SavedStrategyDetail }>(
    `${API_BASE}/${encodeURIComponent(strategyId)}`,
  )
  return response.data
}

export async function createStrategy(payload: {
  name: string
  note?: string
  request: PortfolioBacktestPayload
  summary: Record<string, unknown>
  source_history_id?: string | null
}): Promise<SavedStrategyDetail> {
  const response = await request<{ data: SavedStrategyDetail }>(API_BASE, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  return response.data
}

export async function updateStrategy(
  strategyId: string,
  payload: { name?: string; note?: string },
): Promise<SavedStrategyDetail> {
  const response = await request<{ data: SavedStrategyDetail }>(
    `${API_BASE}/${encodeURIComponent(strategyId)}`,
    {
      method: 'PATCH',
      body: JSON.stringify(payload),
    },
  )
  return response.data
}

export async function deleteStrategy(strategyId: string): Promise<{ deleted: boolean }> {
  const response = await request<{ data: { deleted: boolean } }>(
    `${API_BASE}/${encodeURIComponent(strategyId)}`,
    { method: 'DELETE' },
  )
  return response.data
}

// ---------------------------------------------------------------------------
// 策略追踪：把保存下来的策略沿着交易日历往前推，让它回答"上线之后还灵不灵"。
// ---------------------------------------------------------------------------

export async function fetchStrategyTracking(): Promise<StrategyTrackingSnapshot[]> {
  const response = await request<{ data: StrategyTrackingSnapshot[] }>(`${API_BASE}/tracking`)
  return response.data
}

export async function getStrategyTracking(
  strategyId: string,
): Promise<StrategyTrackingSnapshot> {
  const response = await request<{ data: StrategyTrackingSnapshot }>(
    `${API_BASE}/tracking/${encodeURIComponent(strategyId)}`,
  )
  return response.data
}

export async function getStrategyTrackingTrades(
  strategyId: string,
  filters: {
    phase?: 'pre' | 'post' | ''
    date_from?: string
    date_to?: string
    limit?: number
    offset?: number
  } = {},
): Promise<TrackingTradePage> {
  const query = new URLSearchParams({
    limit: String(filters.limit ?? 50),
    offset: String(filters.offset ?? 0),
  })
  if (filters.phase) query.set('phase', filters.phase)
  if (filters.date_from) query.set('date_from', filters.date_from)
  if (filters.date_to) query.set('date_to', filters.date_to)
  const response = await request<{ data: TrackingTradePage }>(
    `${API_BASE}/tracking/${encodeURIComponent(strategyId)}/trades?${query.toString()}`,
  )
  return response.data
}

interface TrackingJobAccepted {
  job_id: string
  status: string
}

/** 上线。会把当时那段回测固化成"研究段"，之后不再改动。 */
export async function launchStrategyTracking(
  strategyId: string,
): Promise<TrackingJobAccepted> {
  const response = await request<{ data: TrackingJobAccepted }>(
    `${API_BASE}/tracking/${encodeURIComponent(strategyId)}/launch`,
    { method: 'POST' },
  )
  return response.data
}

/** 用最新行情重跑。同一天跑几次都一样；漏了几天也能一次补齐。 */
export async function rerunStrategyTracking(
  strategyId: string,
): Promise<TrackingJobAccepted> {
  const response = await request<{ data: TrackingJobAccepted }>(
    `${API_BASE}/tracking/${encodeURIComponent(strategyId)}/rerun`,
    { method: 'POST' },
  )
  return response.data
}

export async function stopStrategyTracking(
  strategyId: string,
): Promise<StrategyTrackingSnapshot> {
  const response = await request<{ data: StrategyTrackingSnapshot }>(
    `${API_BASE}/tracking/${encodeURIComponent(strategyId)}/stop`,
    { method: 'POST' },
  )
  return response.data
}

export async function getTrackingJob(jobId: string): Promise<StrategyTrackingJobState> {
  const response = await request<{ data: StrategyTrackingJobState }>(
    `${API_BASE}/tracking/jobs/${encodeURIComponent(jobId)}`,
  )
  return response.data
}
