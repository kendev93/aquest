
import { ApiError, createRequestClient } from './client'
import type {
  CustomFormulaMetadata,
  FormulaScreenMetadata,
  InstrumentBoard,
  InstrumentType,
  JobState,
  MarketSyncJobState,
  ResultsMeta,
  ScreenHistoryRecord,
  ScreenHistorySummary,
  ScanPayload,
  ScreenResult,
} from '../types'

const API_BASE = '/api/v1/formula-screen'

const clientOptions = {
  errorClass: ApiError,
  messages: { fallback: '请求失败，请检查配置后重试。' },
}

/** 本模块自身的接口：传相对路径，自动补上 /api/v1/formula-screen。 */
const request = createRequestClient({ ...clientOptions, baseUrl: API_BASE })

const API_ROOT = '/api/v1'

/** 打到别的服务前缀的接口（行情同步属于 /market-data），调用方自己传完整 URL。 */
const requestAt = createRequestClient(clientOptions)

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
