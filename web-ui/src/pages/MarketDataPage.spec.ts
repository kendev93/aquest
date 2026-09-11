import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { FormulaScreenApiError } from '../api/formulaScreen'
import type { LocalInstrument, LocalMarketChart } from '../types'
import * as marketApi from '../api/marketData'
import MarketDataPage from './MarketDataPage.vue'

vi.mock('../api/marketData', async () => {
  const actual = await vi.importActual<typeof import('../api/marketData')>('../api/marketData')
  return {
    ...actual,
    fetchLocalInstruments: vi.fn(),
    fetchLocalChart: vi.fn(),
    fetchStoreStatus: vi.fn(),
    createLocalImport: vi.fn(),
    createMarketDataSync: vi.fn(),
    createOnlineSync: vi.fn(),
    getMarketDataJob: vi.fn(),
  }
})

const instruments: LocalInstrument[] = [
  { market: 'SH', code: '600000', name: '浦发银行', instrument_type: 'stock', bars: 300, data_start: '2020-01-01', data_end: '2024-12-31', last_close: 12.5, error: null },
  { market: 'SZ', code: '000001', name: '平安银行', instrument_type: 'stock', bars: 280, data_start: '2020-01-01', data_end: '2024-12-31', last_close: 10.2, error: null },
]

const chart: LocalMarketChart = {
  market: 'SH', code: '600000', period: 'daily', total_daily_bars: 300, bars: 3,
  available_data_start: '2020-01-01', available_data_end: '2024-12-31', data_start: '2024-12-27', data_end: '2024-12-31',
  candles: [
    { date: '2024-12-27', open: 10, high: 12, low: 9, close: 11, volume: 100, amount: 1100, ma: { ma5: null, ma10: null, ma20: null, ma60: null }, rsi14: null, macd: null, macd_signal: null, macd_histogram: null },
    { date: '2024-12-30', open: 11, high: 13, low: 10, close: 12, volume: 120, amount: 1440, ma: { ma5: null, ma10: null, ma20: null, ma60: null }, rsi14: null, macd: null, macd_signal: null, macd_histogram: null },
    { date: '2024-12-31', open: 12, high: 14, low: 11, close: 13, volume: 140, amount: 1820, ma: { ma5: 11.5, ma10: null, ma20: null, ma60: null }, rsi14: 68, macd: 0.5, macd_signal: 0.4, macd_histogram: 0.1 },
  ],
}

describe('MarketDataPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(marketApi.fetchLocalInstruments).mockResolvedValue({
      items: instruments,
      meta: { total: 2, page: 1, page_size: 50, pages: 1 },
    })
    vi.mocked(marketApi.fetchLocalChart).mockResolvedValue(chart)
    vi.mocked(marketApi.fetchStoreStatus).mockResolvedValue({
      database_path: '/data/market/market.duckdb',
      schema_version: 1,
      instrument_count: 2,
      bar_count: 580,
      data_start: '2020-01-01',
      data_end: '2024-12-31',
      last_local_import_at: null,
      last_online_sync_at: null,
    })
  })

  it('lists local instruments and loads daily, monthly, and yearly charts', async () => {
    const wrapper = mount(MarketDataPage)
    await flushPromises()

    expect(wrapper.get('[data-testid="local-instruments"]').text()).toContain('600000')
    expect(wrapper.get('[data-testid="local-instruments"]').text()).toContain('浦发银行')
    await wrapper.get('[data-testid="instrument-SH-600000"]').trigger('click')
    await flushPromises()

    expect(marketApi.fetchLocalChart).toHaveBeenCalledWith(expect.objectContaining({ market: 'SH', code: '600000', period: 'daily' }))
    expect(wrapper.get('[data-testid="market-candlestick-chart"]')).toBeTruthy()
    expect(wrapper.get('[data-testid="market-chart-summary"]').text()).toContain('2024-12-31')

    await wrapper.get('[data-testid="chart-period-monthly"]').trigger('click')
    await flushPromises()
    await wrapper.get('[data-testid="chart-period-yearly"]').trigger('click')
    await flushPromises()

    expect(marketApi.fetchLocalChart).toHaveBeenLastCalledWith(expect.objectContaining({ period: 'yearly' }))
    expect(wrapper.get('[data-testid="market-chart-panel"]').text()).toContain('MA5')
  })

  it('shows a user-facing error when the local directory cannot be read', async () => {
    vi.mocked(marketApi.fetchLocalInstruments).mockRejectedValueOnce(new FormulaScreenApiError('vipdoc 路径无效', 422))
    const wrapper = mount(MarketDataPage)
    await flushPromises()

    expect(wrapper.get('[data-testid="market-data-message"]').text()).toContain('vipdoc 路径无效')
    expect(wrapper.get('[data-testid="market-chart-empty"]').text()).toContain('请选择品种')
  })

  it('imports local vipdoc data and refreshes the DuckDB status', async () => {
    vi.mocked(marketApi.createLocalImport).mockResolvedValue({ job_id: 'local-1', status: 'queued' })
    vi.mocked(marketApi.getMarketDataJob).mockResolvedValue({
      job_id: 'local-1', status: 'completed', progress: 1, total_candidates: 1,
      total_scanned: 1, errors: 0, error: null, result: null,
    })
    const wrapper = mount(MarketDataPage)
    await flushPromises()

    await wrapper.get('[data-testid="market-import-local"]').trigger('click')
    await flushPromises()

    expect(marketApi.createLocalImport).toHaveBeenCalledWith({
      vipdoc_path: '/data/vipdoc', universe: 'all',
    })
    expect(marketApi.getMarketDataJob).toHaveBeenCalledWith('local-1')
    expect(marketApi.fetchStoreStatus).toHaveBeenCalledTimes(2)
  })

  it('sends the configured market-data scope to local import', async () => {
    vi.mocked(marketApi.createLocalImport).mockResolvedValue({ job_id: 'local-2', status: 'queued' })
    vi.mocked(marketApi.getMarketDataJob).mockResolvedValue({
      job_id: 'local-2', status: 'completed', progress: 1, total_candidates: 1,
      total_scanned: 1, errors: 0, error: null, result: null,
    })
    const wrapper = mount(MarketDataPage)
    await flushPromises()

    await wrapper.get('[data-testid="market-sync-universe"]').setValue('sh')
    await wrapper.get('[data-testid="market-sync-type-stock"]').setValue(true)
    await wrapper.get('[data-testid="market-sync-board-main"]').setValue(true)
    await wrapper.get('[data-testid="market-import-local"]').trigger('click')
    await flushPromises()

    expect(marketApi.createLocalImport).toHaveBeenCalledWith({
      vipdoc_path: '/data/vipdoc',
      universe: 'sh',
      instrument_types: ['stock'],
      boards: ['main'],
    })
  })

  it('uses one-click sync to import local data and then update online data', async () => {
    vi.mocked(marketApi.createMarketDataSync).mockResolvedValue({ job_id: 'sync-1', status: 'queued' })
    vi.mocked(marketApi.getMarketDataJob).mockResolvedValue({
      job_id: 'sync-1', status: 'completed', progress: 1, total_candidates: 1,
      total_scanned: 1, errors: 0, error: null, result: {
        source: 'combined',
        imported_files: 0,
        updated_files: 1,
        written_bars: 1,
        local_import: { source: 'local', imported_files: 1, imported_bars: 2 },
        online_sync: {
          source: 'online', updated_files: 1, written_bars: 1,
          listed_instruments: 4, filtered_instruments: 2,
          new_instruments: 3, existing_instruments: 1,
        },
      },
    })
    const wrapper = mount(MarketDataPage)
    await flushPromises()

    await wrapper.get('[data-testid="market-sync-online"]').trigger('click')
    await flushPromises()

    expect(marketApi.createMarketDataSync).toHaveBeenCalledWith({
      vipdoc_path: '/data/vipdoc',
      universe: 'all',
      bars: 800,
    })
    expect(wrapper.get('[data-testid="market-data-progress"]').text()).toContain('本地导入 1 个文件')
    expect(wrapper.get('[data-testid="market-data-progress"]').text()).toContain('在线写入 1 根')
    expect(wrapper.get('[data-testid="market-data-progress"]').text()).toContain('证券列表 4')
    expect(wrapper.get('[data-testid="market-data-progress"]').text()).toContain('新增标的 3')
  })

  it('shows the current synchronized instrument and date range', async () => {
    vi.mocked(marketApi.createMarketDataSync).mockResolvedValue({ job_id: 'online-1', status: 'queued' })
    let polls = 0
    vi.mocked(marketApi.getMarketDataJob).mockImplementation(async () => {
      polls += 1
      return {
        job_id: 'online-1', status: polls === 1 ? 'running' : 'completed',
        progress: polls === 1 ? 0.42 : 1, total_candidates: 17525,
        total_scanned: polls === 1 ? 7350 : 17525, errors: 0, error: null,
        metadata: {
          current_instrument: { market: 'SH', code: '600000', name: '浦发银行' },
          current_status: 'fetching',
          range_label: '最新 800 根日线',
        },
        result: polls === 1 ? null : { source: 'online', updated_files: 1, written_bars: 800 },
      }
    })
    const wrapper = mount(MarketDataPage)
    await flushPromises()

    await wrapper.get('[data-testid="market-sync-online"]').trigger('click')
    await flushPromises()

    expect(wrapper.get('[data-testid="market-sync-current"]').text()).toContain('SH 600000 · 浦发银行')
    expect(wrapper.get('[data-testid="market-sync-current"]').text()).toContain('最新 800 根日线')
  })

  it('shows the security-list discovery stage while the sync is waiting', async () => {
    vi.useFakeTimers()
    try {
      vi.mocked(marketApi.createMarketDataSync).mockResolvedValue({ job_id: 'waiting-1', status: 'queued' })
      let polls = 0
      vi.mocked(marketApi.getMarketDataJob).mockImplementation(async () => {
        polls += 1
        return {
          job_id: 'waiting-1',
          status: polls === 1 ? 'running' : 'completed',
          progress: polls === 1 ? 0 : 1,
          total_candidates: polls === 1 ? 0 : 17525,
          total_scanned: polls === 1 ? 0 : 17525,
          errors: 0,
          error: null,
          phase: 'instrument_discovery',
          metadata: {
            current_status: 'fetching',
            range_label: '正在刷新沪深完整证券列表…',
          },
          result: polls === 1 ? null : { source: 'online', updated_files: 1, written_bars: 800 },
        }
      })
      const wrapper = mount(MarketDataPage)
      await flushPromises()

      const click = wrapper.get('[data-testid="market-sync-online"]').trigger('click')
      await flushPromises()

      expect(wrapper.get('[data-testid="market-data-progress"]').text()).toContain('正在刷新证券列表')
      expect(wrapper.get('[data-testid="market-data-progress"]').text()).toContain('等待 TDX 返回证券列表')

      await vi.advanceTimersByTimeAsync(500)
      await flushPromises()
      await click
    } finally {
      vi.clearAllTimers()
      vi.useRealTimers()
    }
  })

  it('keeps polling a slow market-data job without a fixed attempt timeout', async () => {
    vi.useFakeTimers()
    try {
      vi.mocked(marketApi.createLocalImport).mockResolvedValue({ job_id: 'slow-1', status: 'queued' })
      let polls = 0
      vi.mocked(marketApi.getMarketDataJob).mockImplementation(async (jobId) => {
        polls += 1
        return {
          job_id: jobId,
          status: polls <= 601 ? 'running' : 'completed',
          progress: polls <= 601 ? 0.5 : 1,
          total_candidates: 1703,
          total_scanned: 850,
          errors: 0,
          error: null,
          result: polls <= 601 ? null : { source: 'local', imported_files: 1703, imported_bars: 1000 },
        }
      })
      const wrapper = mount(MarketDataPage)
      await flushPromises()

      await wrapper.get('[data-testid="market-import-local"]').trigger('click')
      for (let attempt = 0; attempt < 602; attempt += 1) {
        await vi.advanceTimersByTimeAsync(500)
        await flushPromises()
      }

      expect(polls).toBeGreaterThan(600)
      expect(wrapper.get('[data-testid="market-data-progress"]').text()).not.toContain('行情同步超时')
    } finally {
      vi.useRealTimers()
    }
  })

  it('waits for an automatic startup import before loading the local list', async () => {
    vi.mocked(marketApi.fetchStoreStatus)
      .mockResolvedValueOnce({
        database_path: '/data/market/market.duckdb',
        schema_version: 2,
        instrument_count: 0,
        bar_count: 0,
        data_start: null,
        data_end: null,
        last_local_import_at: null,
        last_online_sync_at: null,
        startup_import_job_id: 'startup-1',
      })
      .mockResolvedValueOnce({
        database_path: '/data/market/market.duckdb',
        schema_version: 2,
        instrument_count: 1,
        bar_count: 2,
        data_start: '2024-01-02',
        data_end: '2024-01-03',
        last_local_import_at: '2026-08-30T10:00:00',
        last_online_sync_at: null,
        startup_import_job_id: null,
      })
    vi.mocked(marketApi.getMarketDataJob).mockResolvedValue({
      job_id: 'startup-1', status: 'completed', progress: 1, total_candidates: 1,
      total_scanned: 1, errors: 0, error: null,
      result: { source: 'local', imported_files: 1, imported_bars: 2 },
    })
    const wrapper = mount(MarketDataPage)
    await flushPromises()

    expect(marketApi.getMarketDataJob).toHaveBeenCalledWith('startup-1')
    expect(marketApi.fetchLocalInstruments).toHaveBeenCalled()
    expect(wrapper.get('[data-testid="market-store-status"]').text()).toContain('K线 2')
  })

  it('discovers an automatic sync started after the page has loaded and shows progress', async () => {
    vi.useFakeTimers()
    try {
      vi.mocked(marketApi.fetchStoreStatus)
        .mockResolvedValueOnce({
          database_path: '/data/market/market.duckdb',
          schema_version: 5,
          instrument_count: 2,
          bar_count: 580,
          data_start: '2020-01-01',
          data_end: '2024-12-31',
          last_local_import_at: null,
          last_online_sync_at: null,
          auto_sync_enabled: true,
          auto_sync_job_id: null,
          auto_sync_interval_seconds: 900,
        })
        .mockResolvedValue({
          database_path: '/data/market/market.duckdb',
          schema_version: 5,
          instrument_count: 2,
          bar_count: 580,
          data_start: '2020-01-01',
          data_end: '2024-12-31',
          last_local_import_at: null,
          last_online_sync_at: null,
          auto_sync_enabled: true,
          auto_sync_job_id: 'auto-1',
          auto_sync_interval_seconds: 900,
        })
      let polls = 0
      vi.mocked(marketApi.getMarketDataJob).mockImplementation(async (jobId) => {
        polls += 1
        return {
          job_id: jobId,
          status: polls === 1 ? 'running' : 'completed',
          progress: polls === 1 ? 0.42 : 1,
          total_candidates: 1703,
          total_scanned: polls === 1 ? 716 : 1703,
          errors: 0,
          error: null,
          result: polls === 1 ? null : { source: 'online', updated_files: 100, written_bars: 100 },
        }
      })
      const wrapper = mount(MarketDataPage)
      await flushPromises()

      await vi.advanceTimersByTimeAsync(5000)
      await flushPromises()

      expect(marketApi.getMarketDataJob).toHaveBeenCalledWith('auto-1')
      expect(wrapper.get('[data-testid="market-data-progress"]').text()).toContain('自动同步最新行情')
      expect(wrapper.get('[data-testid="market-data-progress"]').text()).toContain('42%')

      await vi.advanceTimersByTimeAsync(500)
      await flushPromises()
    } finally {
      vi.clearAllTimers()
      vi.useRealTimers()
    }
  })
})
