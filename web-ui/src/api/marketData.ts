import { FormulaScreenApiError } from './formulaScreen'
import type {
  DataStoreStatus,
  InstrumentBoard,
  InstrumentType,
  LocalInstrument,
  LocalInstrumentMeta,
  LocalMarketChart,
  LocalMarketScope,
  LocalVipdocCandidate,
  MarketDataCapabilities,
  MarketChartPeriod,
} from '../types'
import type { MarketSyncJobState, MarketSyncResult } from '../types'

const API_BASE = '/api/v1/market-data'

interface ApiErrorPayload {
  error?: { code?: string; message?: string }
  detail?: string
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  })
  const raw = await response.text()
  let payload: (T & ApiErrorPayload) | null = null
  try {
    payload = raw ? (JSON.parse(raw) as T & ApiErrorPayload) : null
  } catch {
    throw new FormulaScreenApiError(
      raw.trim().slice(0, 240) || '服务器返回了无效响应，请稍后重试。',
      response.status,
    )
  }
  if (!response.ok) {
    throw new FormulaScreenApiError(
      payload?.error?.message ?? payload?.detail ?? '请求失败，请检查共享行情数据库后重试。',
      response.status,
    )
  }
  if (payload === null) {
    throw new FormulaScreenApiError('服务器返回了空响应，请稍后重试。', response.status)
  }
  return payload as T
}

export interface LocalInstrumentQuery {
  market?: LocalMarketScope
  keyword?: string
  page?: number
  page_size?: number
}

export async function fetchLocalInstruments(
  query: LocalInstrumentQuery = {},
): Promise<{ items: LocalInstrument[]; meta: LocalInstrumentMeta }> {
  const params = new URLSearchParams()
  if (query.market) params.set('market', query.market)
  if (query.keyword) params.set('keyword', query.keyword)
  if (query.page) params.set('page', String(query.page))
  if (query.page_size) params.set('page_size', String(query.page_size))
  const suffix = params.toString()
  const response = await request<{ data: LocalInstrument[]; meta: LocalInstrumentMeta }>(
    `${API_BASE}/local/instruments${suffix ? `?${suffix}` : ''}`,
  )
  return { items: response.data, meta: response.meta }
}

export interface LocalChartQuery {
  market: 'SH' | 'SZ'
  code: string
  period?: MarketChartPeriod
  start_date?: string
  end_date?: string
  formula_ids?: string[]
}

export async function fetchLocalChart(query: LocalChartQuery): Promise<LocalMarketChart> {
  const params = new URLSearchParams()
  if (query.period) params.set('period', query.period)
  if (query.start_date) params.set('start_date', query.start_date)
  if (query.end_date) params.set('end_date', query.end_date)
  if (query.formula_ids?.length) params.set('formula_ids', query.formula_ids.join(','))
  const suffix = params.toString()
  const response = await request<{ data: LocalMarketChart }>(
    `${API_BASE}/local/${query.market}/${encodeURIComponent(query.code)}/bars${suffix ? `?${suffix}` : ''}`,
  )
  return response.data
}

export async function fetchStoreStatus(): Promise<DataStoreStatus> {
  const response = await request<{ data: DataStoreStatus }>(`${API_BASE}/store`)
  return response.data
}

export async function fetchMarketDataCapabilities(): Promise<MarketDataCapabilities> {
  const response = await request<{ data: MarketDataCapabilities }>(`${API_BASE}/capabilities`)
  return response.data
}

export async function createMarketDataSync(payload: {
  universe?: 'all' | 'sh' | 'sz'
  bars?: number
  instrument_types?: InstrumentType[]
  boards?: InstrumentBoard[]
} = {}): Promise<{ job_id: string; status: string }> {
  const response = await request<{ data: { job_id: string; status: string } }>(
    `${API_BASE}/sync`,
    { method: 'POST', body: JSON.stringify(payload) },
  )
  return response.data
}

export async function createOnlineBackfill(payload: {
  universe?: 'all' | 'sh' | 'sz'
  boards?: InstrumentBoard[]
} = {}): Promise<{ job_id: string; status: string }> {
  const response = await request<{ data: { job_id: string; status: string } }>(
    `${API_BASE}/backfill`,
    {
      method: 'POST',
      body: JSON.stringify(payload),
    },
  )
  return response.data
}

export interface LocalVipdocDetection {
  candidates: LocalVipdocCandidate[]
  default_path: string
  mount_hint: string
  host_mount_prefix?: string | null
}

export async function detectLocalVipdoc(): Promise<LocalVipdocDetection> {
  const response = await request<{ data: LocalVipdocDetection }>(
    `${API_BASE}/local-import/detect`,
  )
  return response.data
}

export async function createLocalImport(payload: {
  vipdoc_path?: string
  universe?: 'all' | 'sh' | 'sz'
  instrument_types?: InstrumentType[]
  boards?: InstrumentBoard[]
} = {}): Promise<{ job_id: string; status: string }> {
  const response = await request<{ data: { job_id: string; status: string } }>(
    `${API_BASE}/local-import`,
    { method: 'POST', body: JSON.stringify(payload) },
  )
  return response.data
}

export async function createOnlineSync(payload: {
  universe?: 'all' | 'sh' | 'sz'
  bars?: number
  instrument_types?: InstrumentType[]
  boards?: InstrumentBoard[]
} = {}): Promise<{ job_id: string; status: string }> {
  const response = await request<{ data: { job_id: string; status: string } }>(
    `${API_BASE}/sync-online`,
    { method: 'POST', body: JSON.stringify(payload) },
  )
  return response.data
}

export async function getMarketDataJob(jobId: string): Promise<MarketSyncJobState> {
  const response = await request<{ data: MarketSyncJobState }>(
    `${API_BASE}/jobs/${encodeURIComponent(jobId)}`,
  )
  return response.data
}

export async function fetchActiveMarketDataJobs(): Promise<MarketSyncJobState[]> {
  const response = await request<{ data: MarketSyncJobState[] }>(`${API_BASE}/jobs/active`)
  return response.data
}

export async function cancelMarketDataJob(jobId: string): Promise<{
  job_id: string
  status: string
  cancel_requested: boolean
}> {
  const response = await request<{ data: { job_id: string; status: string; cancel_requested: boolean } }>(
    `${API_BASE}/jobs/${encodeURIComponent(jobId)}/cancel`,
    { method: 'POST' },
  )
  return response.data
}

export type MarketDataImportResult = MarketSyncResult
