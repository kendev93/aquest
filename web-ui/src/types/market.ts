/** market 相关的前端类型。 */

import type { InstrumentBoard, InstrumentType, MarketChartPeriod } from './common'


export interface JobState {
  job_id: string
  status: 'queued' | 'running' | 'completed' | 'failed'
  progress: number
  total_candidates: number
  total_scanned: number
  total_signals: number
  errors: number
  skipped: number
  error: string | null
  worker_count?: number | null
}


export interface ActiveJobState {
  job_id: string
  description: string
  status: 'queued' | 'running'
  progress: number
  total_candidates: number
  total_scanned: number
  total_signals?: number
  errors: number
  skipped?: number
  error: string | null
  result?: unknown | null
  worker_count?: number | null
  phase?: string | null
  metadata?: Record<string, unknown>
  cancel_requested?: boolean
  operation?: 'discovery' | 'refinement' | 'replay'
}


export interface MarketSyncResult {
  /** 日线来源：在线同步 online/online_discovery，通达信本地导入 local。 */
  source?: 'online' | 'online_discovery' | 'local'
  /** 后端返回的字符串；历史记录里可能是已移除的 'tdx'。 */
  provider?: string
  status?: 'skipped'
  reason?: string
  updated_files?: number
  unchanged_files?: number
  total_candidates?: number
  processed?: number
  listed_instruments?: number
  filtered_instruments?: number
  new_instruments?: number
  existing_instruments?: number
  target_source?: string
  written_bars?: number
  errors?: number
  failure_reasons?: Record<string, number>
  history_sources?: Record<string, number>
  execution_mode?: string
  worker_count?: number
  staged_rows?: number
  merged_rows?: number
  stage_files?: string[]
  download_seconds?: number
  merge_seconds?: number
  cleanup_seconds?: number
  rate_limit_events?: number
  rate_limit_backoffs?: number
  operation?: string
  success?: boolean
  range_preset?: string
  requested_start?: string | null
  requested_end?: string | null
  actual_start?: string | null
  actual_end?: string | null
  total_targets?: number
  planned_jobs?: number
  completed?: number
  skipped?: number
  failed?: number
  rows?: number
  catalog_counts?: Record<string, number>
  /** 以下是「删除并重下全部历史」专用的键：先删后下，同步/补齐都不会返回。 */
  deleted_bars?: number
  refreshed_instruments?: number
  /** 删除后重新下载却拿不到数据的标的（退市等），后端最多回传 20 个。 */
  empty_after_refresh?: string[]
  /** 以下是通达信本地导入（source='local'）专用的键，在线同步不会返回。 */
  vipdoc_path?: string
  discovered_files?: number
  selected_files?: number
  filtered_files?: number
  imported_instruments?: number
  imported_bars?: number
  replaced_bars?: number
  skipped_files?: number
  missing_files?: number
  range_start?: string | null
  range_end?: string | null
}


export interface MarketSyncJobState {
  job_id: string
  description?: string
  status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'
  progress: number
  total_candidates: number
  total_scanned: number
  errors: number
  error: string | null
  worker_count?: number | null
  phase?: string | null
  metadata?: Record<string, unknown>
  cancel_requested?: boolean
  result: MarketSyncResult | null
}


export interface DataStoreStatus {
  database_path: string
  schema_version: number
  instrument_count: number
  bar_count: number
  data_start: string | null
  data_end: string | null
  last_online_sync_at: string | null
  last_online_provider?: string | null
  last_online_provider_version?: string | null
  market_calendar_latest_date?: string | null
  market_calendar_source?: string | null
  market_calendar_updated_at?: string | null
  last_auto_sync_check_date?: string | null
  last_auto_sync_check_session?: string | null
  auto_sync_job_id?: string | null
  auto_sync_enabled?: boolean
  auto_sync_interval_seconds?: number | null
  market_data_source?: 'aquest' | string
  market_data_read_only?: boolean
  market_data_write_target?: 'aquest' | string
  market_sync_lock?: Record<string, unknown> | null
  last_market_sync_summary?: Record<string, unknown> | null
  legacy_market_write_enabled?: boolean
}


export type LocalMarketScope = 'all' | 'SH' | 'SZ'


export interface LocalVipdocCandidate {
  path: string
  file_count: number
  markets: string[]
}


export interface LocalInstrument {
  market: 'SH' | 'SZ'
  code: string
  name?: string | null
  instrument_type: InstrumentType
  board?: InstrumentBoard
  provider?: string | null
  bars: number
  data_start: string | null
  data_end: string | null
  last_close: number | null
  error: string | null
}


export interface LocalInstrumentMeta {
  total: number
  page: number
  page_size: number
  pages: number
}


/** 画在 K 线上的买卖点（回测成交）。 */
export interface ChartMarker {
  date: string
  side: 'buy' | 'sell'
}


export interface MarketCandle {
  date: string
  open: number | null
  high: number | null
  low: number | null
  close: number | null
  volume: number | null
  amount: number | null
  ma: Record<string, number | null>
  rsi14: number | null
  macd: number | null
  macd_signal: number | null
  macd_histogram: number | null
}


export interface MarketFormulaPanelValue {
  id: string
  display_name: string
  values: Array<number | null>
  latest: number | null
}


export interface MarketFormulaPanel {
  id: string
  display_name: string
  dates: string[]
  values: MarketFormulaPanelValue[]
}


export interface LocalMarketChart {
  market: 'SH' | 'SZ'
  code: string
  period: MarketChartPeriod
  total_daily_bars: number
  bars: number
  available_data_start: string
  available_data_end: string
  data_start: string
  data_end: string
  candles: MarketCandle[]
  formula_panels?: MarketFormulaPanel[]
}


/** 一项健康检查的汇总：有多少标的命中、一共多少次、以及可以点开核对的示例。 */
export interface MarketHealthCheck {
  key: string
  label: string
  description: string
  severity: 'ok' | 'warn' | 'error'
  affected_instruments: number
  occurrences: number
  repairable: boolean
  examples: MarketHealthExample[]
}


export interface MarketHealthExample {
  market: string
  code: string
  name: string | null
  trade_date: string | null
  detail: string
}


/** 体检报告。所有下游结论都建立在「行情没洞」上，这份报告就是把那个前提显式化。 */
export interface MarketDataHealthReport {
  generated_at: string
  instrument_count: number
  bar_count: number
  calendar_days: number
  data_start: string | null
  data_end: string | null
  duration_seconds: number
  checks: MarketHealthCheck[]
}


/** 修复结果。修复是「先删后下」，所以必须把删了没补回来的标的点出来。 */
export interface MarketHealthRepairOutcome {
  checked_keys?: string[]
  requested_codes?: number
  truncated?: boolean
  deleted_bars?: number
  written_bars?: number
  errors?: number
  failure_reasons?: Record<string, number>
  empty_after_repair?: string[]
  message?: string
}


export interface MarketHealthJobState extends Omit<MarketSyncJobState, 'result'> {
  result:
    | (MarketSyncResult & Partial<MarketDataHealthReport> & MarketHealthRepairOutcome)
    | null
}
