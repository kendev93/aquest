import type {
  CustomFormulaMetadata,
  FormulaScreenMetadata,
  InstrumentBoard,
  InstrumentType,
  JobState,
  MarketSyncJobState,
  MarketDataProvider,
  ResultsMeta,
  ScreenHistoryRecord,
  ScreenHistorySummary,
  ScanPayload,
  ScreenResult,
} from '../types'

const API_BASE = '/api/v1/formula-screen'
const API_ROOT = '/api/v1'

interface ApiErrorPayload {
  error?: { message?: string }
  detail?: string
}

export class FormulaScreenApiError extends Error {
  constructor(message: string, public readonly status: number) {
    super(message)
    this.name = 'FormulaScreenApiError'
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  return requestAt<T>(`${API_BASE}${path}`, init)
}

async function requestAt<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  })
  const payload = (await response.json()) as T & ApiErrorPayload
  if (!response.ok) {
    throw new FormulaScreenApiError(
      payload.error?.message ?? payload.detail ?? '请求失败，请检查配置后重试。',
      response.status,
    )
  }
  return payload as T
}

export async function fetchMetadata(): Promise<FormulaScreenMetadata> {
  const payload = await request<{ data: FormulaScreenMetadata }>('/metadata')
  return payload.data
}

export async function parseFormula(formulaText: string): Promise<CustomFormulaMetadata> {
  const response = await request<{ data: CustomFormulaMetadata }>('/parse', {
    method: 'POST',
    body: JSON.stringify({ formula_text: formulaText }),
  })
  return response.data
}

export async function createJob(payload: ScanPayload): Promise<{ job_id: string; status: string }> {
  const response = await request<{ data: { job_id: string; status: string } }>('/jobs', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  return response.data
}

export async function getJob(jobId: string): Promise<JobState> {
  const response = await request<{ data: JobState }>(`/jobs/${encodeURIComponent(jobId)}`)
  return response.data
}

export async function getResults(jobId: string): Promise<{ results: ScreenResult[]; meta: ResultsMeta }> {
  const response = await request<{ data: ScreenResult[]; meta: ResultsMeta }>(
    `/jobs/${encodeURIComponent(jobId)}/results`,
  )
  return { results: response.data, meta: response.meta }
}

export async function fetchScreenHistory(): Promise<ScreenHistorySummary[]> {
  const response = await request<{ data: ScreenHistorySummary[] }>('/history')
  return response.data
}

export async function getScreenHistory(historyId: string): Promise<ScreenHistoryRecord> {
  const response = await request<{ data: ScreenHistoryRecord }>(
    `/history/${encodeURIComponent(historyId)}`,
  )
  return response.data
}

export async function createSyncJob(payload: {
  provider?: MarketDataProvider
  universe?: 'all' | 'sh' | 'sz'
  bars?: number
  instrument_types?: InstrumentType[]
  boards?: InstrumentBoard[]
} = {}): Promise<{ job_id: string; status: string }> {
  const response = await requestAt<{ data: { job_id: string; status: string } }>(`${API_ROOT}/market-data/sync`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  return response.data
}

export async function getSyncJob(jobId: string): Promise<MarketSyncJobState> {
  const response = await requestAt<{ data: MarketSyncJobState }>(`${API_ROOT}/market-data/sync/jobs/${encodeURIComponent(jobId)}`)
  return response.data
}
