import type {
  DataStoreStatus,
  InstrumentBoard,
  InstrumentType,
  LocalInstrument,
  LocalInstrumentMeta,
  LocalMarketChart,
  LocalMarketScope,
  LocalVipdocCandidate,
  MarketChartPeriod,
} from '../types'
import type { MarketSyncJobState, MarketSyncResult } from '../types'

import { ApiError, createRequestClient } from './client'

const API_BASE = '/api/v1/market-data'

const request = createRequestClient({
  errorClass: ApiError,
  messages: { fallback: '请求失败，请检查共享行情数据库后重试。', emptyBody: '服务器返回了空响应，请稍后重试。', invalidBody: (_status, raw) => raw.trim().slice(0, 240) || '服务器返回了无效响应，请稍后重试。' },
})

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

/** 撤销一次本地导入：本地行永久压过在线行，没有这个入口导错了只能重建数据库。 */
export async function deleteMarketBars(payload: {
  source: 'local' | 'online'
  universe?: 'all' | 'sh' | 'sz'
  codes?: string[]
  start_date?: string
  end_date?: string
  confirm: boolean
}): Promise<{ deleted_bars: number; source: string }> {
  const response = await request<{ data: { deleted_bars: number; source: string } }>(
    `${API_BASE}/bars/delete`,
    { method: 'POST', body: JSON.stringify(payload) },
  )
  return response.data
}

/**
 * 「删除并重下全部历史」：先删掉所选范围内的全部日线，再整段重新下载。
 * 常规同步只补最新一段，补齐只补首尾缺口——中间写歪的那段只有这个入口能覆盖。
 */
export async function createOnlineRefresh(payload: {
  universe?: 'all' | 'sh' | 'sz'
  instrument_types?: InstrumentType[]
  boards?: InstrumentBoard[]
  confirm: boolean
}): Promise<{ job_id: string; status: string }> {
  const response = await request<{ data: { job_id: string; status: string } }>(
    `${API_BASE}/refresh`,
    { method: 'POST', body: JSON.stringify(payload) },
  )
  return response.data
}

/**
 * 提交一次行情体检。600 万行的库实测约 10 秒，所以走任务而不是同步返回。
 */
export async function createHealthCheck(): Promise<{ job_id: string; status: string }> {
  const response = await request<{ data: { job_id: string; status: string } }>(
    `${API_BASE}/health/check`,
    { method: 'POST' },
  )
  return response.data
}

/**
 * 一键修复：删掉命中标的的全部历史再全量重下。
 * 在线源已经没有的标的会变成空数据，所以必须显式 confirm。
 */
export async function createHealthRepair(payload: {
  checks?: string[]
  codes?: string[]
  limit?: number
  confirm: boolean
}): Promise<{ job_id: string; status: string }> {
  const response = await request<{ data: { job_id: string; status: string } }>(
    `${API_BASE}/health/repair`,
    { method: 'POST', body: JSON.stringify(payload) },
  )
  return response.data
}

export type MarketDataImportResult = MarketSyncResult
