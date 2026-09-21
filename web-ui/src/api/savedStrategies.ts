import { FormulaScreenApiError } from './formulaScreen'
import type {
  PortfolioBacktestPayload,
  SavedStrategyDetail,
  SavedStrategySummary,
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
