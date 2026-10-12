import { FormulaScreenApiError } from './formulaScreen'
import type {
  StrategyPoolEntry,
  StrategyPoolJobState,
  StrategyPoolJobSummary,
  StrategyPoolObjective,
  StrategyPoolCandidate,
  StrategyPoolBenchmark,
  StrategyPoolMode,
} from '../types'

const API_BASE = '/api/v1/strategy-pool'

interface ApiErrorPayload {
  error?: { message?: string }
  detail?: string
}

interface StrategyPoolJobRequest {
  mode: StrategyPoolMode
  automatic?: boolean
  rebuild?: boolean
  risk_trials?: 1_000 | 2_000 | 3_000
  max_hold_days?: number | null
  market?: 'SH' | 'SZ'
  code?: string
  objective?: StrategyPoolObjective
  boards?: StrategyPoolBoard[]
  exclude_st?: boolean
}

export type StrategyPoolBoard = 'main' | 'chinext' | 'star'

export interface StrategyPoolResearchSettings {
  boards: StrategyPoolBoard[]
  exclude_st: boolean
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  })
  const payload = (await response.json()) as T & ApiErrorPayload
  if (!response.ok) {
    throw new FormulaScreenApiError(
      payload.error?.message ?? payload.detail ?? '全市场策略池请求失败，请检查配置和本地行情。',
      response.status,
    )
  }
  return payload as T
}

export async function fetchStrategyPoolEntries(
  objective: StrategyPoolObjective,
): Promise<StrategyPoolEntry[]> {
  const response = await request<{ data: { items: StrategyPoolEntry[] } }>(
    `${API_BASE}/entries?objective=${objective}`,
  )
  return response.data.items
}

export async function createStrategyPoolJob(
  payload: StrategyPoolJobRequest,
): Promise<{ job_id: string; status: string; skipped?: boolean }> {
  const response = await request<{ data: { job_id: string; status: string; skipped?: boolean } }>(`${API_BASE}/jobs`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  return response.data
}

export async function getStrategyPoolJob(jobId: string): Promise<StrategyPoolJobState> {
  const response = await request<{ data: StrategyPoolJobState }>(
    `${API_BASE}/jobs/${encodeURIComponent(jobId)}`,
  )
  return response.data
}

export async function cancelStrategyPoolJob(jobId: string): Promise<{ cancelled: boolean }> {
  const response = await request<{ data: { cancelled: boolean } }>(
    `${API_BASE}/jobs/${encodeURIComponent(jobId)}/cancel`,
    { method: 'POST' },
  )
  return response.data
}

export async function fetchStrategyPoolJobs(): Promise<StrategyPoolJobSummary[]> {
  const response = await request<{ data: { items: StrategyPoolJobSummary[] } }>(`${API_BASE}/jobs`)
  return response.data.items
}

export async function fetchResearchSettings(): Promise<StrategyPoolResearchSettings> {
  const response = await request<{ data: StrategyPoolResearchSettings }>(
    `${API_BASE}/research-settings`,
  )
  return response.data
}

export async function saveResearchSettings(
  payload: StrategyPoolResearchSettings,
): Promise<StrategyPoolResearchSettings> {
  const response = await request<{ data: StrategyPoolResearchSettings }>(
    `${API_BASE}/research-settings`,
    { method: 'PUT', body: JSON.stringify(payload) },
  )
  return response.data
}

export async function saveStrategyPoolOverride(
  market: string,
  code: string,
  objective: StrategyPoolObjective,
  candidate: StrategyPoolCandidate,
  benchmark?: StrategyPoolBenchmark,
): Promise<StrategyPoolEntry> {
  const response = await request<{ data: StrategyPoolEntry }>(
    `${API_BASE}/entries/${market}/${code}/overrides/${objective}`,
    {
      method: 'POST',
      body: JSON.stringify({ candidate, ...(benchmark ? { benchmark } : {}) }),
    },
  )
  return response.data
}

export async function clearStrategyPoolOverride(
  market: string,
  code: string,
  objective: StrategyPoolObjective,
): Promise<StrategyPoolEntry> {
  const response = await request<{ data: StrategyPoolEntry }>(
    `${API_BASE}/entries/${market}/${code}/overrides/${objective}`,
    { method: 'DELETE' },
  )
  return response.data
}

export type { StrategyPoolJobRequest }
