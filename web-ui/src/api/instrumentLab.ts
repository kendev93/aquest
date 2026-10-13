
import { ApiError, createRequestClient } from './client'
const API_BASE = '/api/v1/instrument-lab'

const request = createRequestClient({
  errorClass: ApiError,
  messages: { fallback: '请求失败，请检查所选标的后重试。' },
  baseUrl: API_BASE,
})

export interface InstrumentLabScope {
  market?: 'SH' | 'SZ'
  code: string
  vipdoc_path?: string
  start_date?: string | null
  end_date?: string | null
}

export interface InstrumentLabValuesQuery extends InstrumentLabScope {
  value_ids: string[]
}

export interface InstrumentLabSeries {
  value_id: string
  label: string
  dates: string[]
  values: Array<number | null>
  last_value: number | null
  percentile: number | null
  note: string | null
}

export interface InstrumentLabValues {
  market: string
  code: string
  name: string | null
  start_date: string | null
  end_date: string | null
  bars: number
  series: InstrumentLabSeries[]
  thinned: boolean
  notes: string[]
}

export interface InstrumentLabCondition {
  left_value: string
  operator: string
  right_value: string | null
  right_constant: number | null
}

export interface InstrumentLabLibraryItem {
  key: string
  label: string
  side: string | null
  condition: InstrumentLabCondition
  lift: number | null
  pvalue: number | null
  support_count: number | null
  instrument_count: number | null
  history_id: string | null
  created_at: string | null
  scanned_stocks: number | null
}

export interface InstrumentLabCheckQuery extends InstrumentLabScope {
  conditions: InstrumentLabCondition[]
  horizon?: number
  permutations?: number
  block_size?: number
  alpha?: number
}

export interface InstrumentLabCheckItem {
  key: string
  label: string
  hit_count: number
  sample_days: number
  hit_mean_return: number | null
  base_mean_return: number | null
  difference: number | null
  p_value: number | null
  adjusted_p_value: number | null
  significant: boolean | null
  hit_dates: string[]
  note: string | null
}

export interface InstrumentLabCheckReport {
  market: string
  code: string
  name: string | null
  horizon: number
  start_date: string | null
  end_date: string | null
  sample_days: number
  permutations: number
  block_size: number
  items: InstrumentLabCheckItem[]
  notes: string[]
}

export async function fetchInstrumentValues(
  query: InstrumentLabValuesQuery,
): Promise<InstrumentLabValues> {
  const response = await request<{ data: InstrumentLabValues }>('/values', {
    method: 'POST',
    body: JSON.stringify(query),
  })
  return response.data
}

export async function fetchInstrumentConditionLibrary(
  limit = 60,
): Promise<{ items: InstrumentLabLibraryItem[] }> {
  const response = await request<{ data: { items: InstrumentLabLibraryItem[] } }>(
    `/conditions/library?limit=${limit}`,
  )
  return response.data
}

export async function checkInstrumentConditions(
  query: InstrumentLabCheckQuery,
): Promise<InstrumentLabCheckReport> {
  const response = await request<{ data: InstrumentLabCheckReport }>('/conditions/check', {
    method: 'POST',
    body: JSON.stringify(query),
  })
  return response.data
}

export async function runInstrumentBacktest(payload: Record<string, unknown>) {
  const response = await request<{ data: Record<string, unknown> }>('/backtest', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  return response.data
}
