
import { ApiError, createRequestClient } from './client'
const API_BASE = '/api/v1/history-compare'

const request = createRequestClient({
  errorClass: ApiError,
  messages: { fallback: '请求失败，请检查所选的历史记录后重试。' },
})

export interface HistoryCompareCandidate {
  history_id: string
  backtest_type: string
  type_label: string
  label: string
  subtitle: string
  created_at: string
  finished_at: string
  date_range: { start: string | null; end: string | null; days: number | null }
  curve_kind: string | null
  curve_kind_label: string
}

export interface HistoryCompareTypeOption {
  backtest_type: string
  type_label: string
}

export interface HistoryCompareCandidates {
  items: HistoryCompareCandidate[]
  types: HistoryCompareTypeOption[]
  max_items: number
}

export interface HistoryComparePoint {
  date: string
  index: number
  value: number
}

export interface HistoryCompareCurve {
  kind: string
  kind_label: string
  unit: string
  label: string
  normalize: string
  points: HistoryComparePoint[]
  truncated: boolean
  note?: string
}

export interface HistoryCompareMetric {
  key: string
  label: string
  value: number | null
  unit: string
  source: string
  text?: string
  note?: string
}

export interface HistoryCompareItem {
  history_id: string
  short_id: string
  backtest_type: string
  type_label: string
  label: string
  subtitle: string
  created_at: string
  finished_at: string
  date_range: { start: string | null; end: string | null; days: number | null }
  curve: HistoryCompareCurve | null
  curve_unavailable_reason: string | null
  metrics: HistoryCompareMetric[]
  native_metrics: HistoryCompareMetric[]
}

export interface HistoryCompareCurveGroup {
  kind: string | null
  kind_label: string
  unit: string
  plotted: boolean
  history_ids: string[]
  excluded: { history_id: string; kind_label: string }[]
  note: string
}

export interface HistoryCompareReport {
  normalize: string
  x_axis: string
  items: HistoryCompareItem[]
  curve_group: HistoryCompareCurveGroup
  warnings: string[]
}

export interface HistoryComparePayload {
  history_ids: string[]
  normalize?: 'capital' | 'first'
  x_axis?: 'date' | 'index'
}

export async function fetchHistoryCompareCandidates(): Promise<HistoryCompareCandidates> {
  const response = await request<{ data: HistoryCompareCandidates }>(`${API_BASE}/candidates`)
  return response.data
}

export async function compareHistory(
  payload: HistoryComparePayload,
): Promise<HistoryCompareReport> {
  const response = await request<{ data: HistoryCompareReport }>(API_BASE, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  return response.data
}
