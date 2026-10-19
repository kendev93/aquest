
import { ApiError, createRequestClient } from './client'
import type {
  StrategyPoolEntry,
  StrategyPoolResearchEstimate,
  StrategyPoolFocusItem,
  StrategyPoolJobState,
  StrategyPoolJobStats,
  StrategyPoolJobSummary,
  StrategyPoolSignalDay,
  StrategyPoolObjective,
  StrategyPoolCandidate,
  StrategyPoolBenchmark,
  StrategyPoolMode,
} from '../types'

const API_BASE = '/api/v1/strategy-pool'

const request = createRequestClient({
  errorClass: ApiError,
  messages: { fallback: '全市场策略池请求失败，请检查配置和本地行情。' },
})

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

export async function fetchStrategyPoolEntries(
  objective: StrategyPoolObjective,
): Promise<StrategyPoolEntry[]> {
  const response = await request<{ data: { items: StrategyPoolEntry[] } }>(
    `${API_BASE}/entries?objective=${objective}`,
  )
  return response.data.items
}

export async function fetchStrategyPoolResearchEstimate(
  payload: StrategyPoolJobRequest,
): Promise<StrategyPoolResearchEstimate> {
  const response = await request<{ data: StrategyPoolResearchEstimate }>(`${API_BASE}/research-estimate`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  return response.data
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

// 可回看的信号交易日：每个交易日一份（当天最后一次全市场扫描），后端只留最近 10 个交易日。
// 选中某天就用它的 job_id 走 /jobs/{job_id} 取结果，形状和当天那份完全一样。
export async function fetchPoolSignalDays(): Promise<StrategyPoolSignalDay[]> {
  const response = await request<{ data: { items?: StrategyPoolSignalDay[] } }>(
    `${API_BASE}/signal-days`,
  )
  return response.data.items ?? []
}

// 平均耗时按模式各取最近若干条终态任务，跟列表分页无关，所以单独一个接口：
// 列表每页 20 条里几乎全是十分钟一次的信号扫描，就地聚合算不出全市场那一轮多久。
export async function fetchStrategyPoolJobStats(): Promise<StrategyPoolJobStats> {
  const response = await request<{ data: StrategyPoolJobStats }>(`${API_BASE}/jobs/stats`)
  return response.data
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

// 特别关注走后端而不是 localStorage：清空重建全市场会删空标的条目，关注是用户的选择，
// 不能跟着一起丢。三个函数都返回服务端权威名单，所以点一次就同步一次，不用二次 GET。
// e2e 的接口兜底 mock 会返回空对象，这里一律 `?? []`。
function readFocusItems(data: { items?: StrategyPoolFocusItem[] }): StrategyPoolFocusItem[] {
  return data.items ?? []
}

export async function fetchStrategyPoolFocus(): Promise<StrategyPoolFocusItem[]> {
  const response = await request<{ data: { items?: StrategyPoolFocusItem[] } }>(`${API_BASE}/focus`)
  return readFocusItems(response.data)
}

export async function addStrategyPoolFocus(
  market: string,
  code: string,
  name: string,
): Promise<StrategyPoolFocusItem[]> {
  const response = await request<{ data: { items?: StrategyPoolFocusItem[] } }>(`${API_BASE}/focus`, {
    method: 'POST',
    body: JSON.stringify({ market, code, name }),
  })
  return readFocusItems(response.data)
}

export async function removeStrategyPoolFocus(
  market: string,
  code: string,
): Promise<StrategyPoolFocusItem[]> {
  const response = await request<{ data: { items?: StrategyPoolFocusItem[] } }>(
    `${API_BASE}/focus/${market}/${code}`,
    { method: 'DELETE' },
  )
  return readFocusItems(response.data)
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
