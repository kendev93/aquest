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
    cancelMarketDataJob: vi.fn(),
    fetchLocalInstruments: vi.fn(),
    fetchLocalChart: vi.fn(),
    fetchStoreStatus: vi.fn(),
    fetchActiveMarketDataJobs: vi.fn(),
    fetchMarketDataCapabilities: vi.fn(),
    createOnlineBackfill: vi.fn(),
    createOnlineSync: vi.fn(),
    detectLocalVipdoc: vi.fn(),
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
    vi.mocked(marketApi.fetchMarketDataCapabilities).mockResolvedValue({
      default_provider: 'eastmoney',
      providers: {
        eastmoney: { provider: 'eastmoney', available: true, version: 'builtin-http-1', instrument_types: ['stock'] },
      },
    })
    vi.mocked(marketApi.fetchActiveMarketDataJobs).mockResolvedValue([])
    vi.mocked(marketApi.cancelMarketDataJob).mockResolvedValue({
      job_id: 'active-1',
      status: 'cancelling',
      cancel_requested: true,
    })
    vi.mocked(marketApi.detectLocalVipdoc).mockResolvedValue({
      candidates: [{ path: '/data/vipdoc', file_count: 5400, markets: ['SH', 'SZ'] }],
      default_path: '/data/vipdoc',
      mount_hint: '后端运行在容器里，需要先挂载。',
    })
    vi.mocked(marketApi.fetchStoreStatus).mockResolvedValue({
      database_path: '/data/market/market.duckdb',
      schema_version: 1,
      instrument_count: 2,
      bar_count: 580,
      data_start: '2020-01-01',
      data_end: '2024-12-31',
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

  it('selects a data-bearing instrument when the first catalog item has no bars', async () => {
    vi.mocked(marketApi.fetchLocalInstruments).mockResolvedValueOnce({
      items: [
        { market: 'SH', code: '000001', name: '上证指数', instrument_type: 'index', bars: 0, data_start: null, data_end: null, last_close: null, error: '没有可用日线数据' },
        instruments[0],
      ],
      meta: { total: 2, page: 1, page_size: 50, pages: 1 },
    })

    const wrapper = mount(MarketDataPage)
    await flushPromises()

    expect(marketApi.fetchLocalChart).toHaveBeenCalledWith(expect.objectContaining({ market: 'SH', code: '600000' }))
    expect(wrapper.get('[data-testid="market-chart-panel"]').text()).toContain('600000')
    expect(wrapper.find('[data-testid="market-data-message"]').exists()).toBe(false)
  })

  it('shows a user-facing error when the local directory cannot be read', async () => {
    vi.mocked(marketApi.fetchLocalInstruments).mockRejectedValueOnce(new FormulaScreenApiError('vipdoc 路径无效', 422))
    const wrapper = mount(MarketDataPage)
    await flushPromises()

    expect(wrapper.get('[data-testid="market-data-message"]').text()).toContain('vipdoc 路径无效')
    expect(wrapper.get('[data-testid="market-chart-empty"]').text()).toContain('请选择品种')
  })

  it('uses one-click sync to update online data without importing local files', async () => {
    vi.mocked(marketApi.createOnlineSync).mockResolvedValue({ job_id: 'sync-1', status: 'queued' })
    vi.mocked(marketApi.getMarketDataJob).mockResolvedValue({
      job_id: 'sync-1', status: 'completed', progress: 1, total_candidates: 1,
      total_scanned: 1, errors: 0, error: null, result: {
        source: 'online',
        updated_files: 1,
        written_bars: 1,
        listed_instruments: 4, filtered_instruments: 2,
        new_instruments: 3, existing_instruments: 1,
      },
    })
    const wrapper = mount(MarketDataPage)
    await flushPromises()

    await wrapper.get('[data-testid="market-sync-online"]').trigger('click')
    await flushPromises()

    expect(marketApi.createOnlineSync).toHaveBeenCalledWith({
      universe: 'all',
      bars: 800,
      instrument_types: ['stock'],
    })
    expect(wrapper.get('[data-testid="market-data-progress"]').text()).not.toContain('文件导入')
    expect(wrapper.get('[data-testid="market-data-progress"]').text()).toContain('更新标的 1')
    expect(wrapper.get('[data-testid="market-data-progress"]').text()).toContain('写入 K 线 1')
    expect(wrapper.get('[data-testid="market-data-progress"]').text()).toContain('证券列表 4')
    expect(wrapper.get('[data-testid="market-data-progress"]').text()).toContain('新增标的 3')
  })

  it('syncs through East Money without offering a provider switch', async () => {
    vi.mocked(marketApi.createOnlineSync).mockResolvedValue({ job_id: 'online-2', status: 'queued' })
    vi.mocked(marketApi.getMarketDataJob).mockResolvedValue({
      job_id: 'online-2', status: 'completed', progress: 1, total_candidates: 0,
      total_scanned: 0, errors: 0, error: null, result: { source: 'online', provider: 'eastmoney' },
    })
    const wrapper = mount(MarketDataPage)
    await flushPromises()

    // TDX 通道已移除，界面上不再有行情源单选。
    expect(wrapper.find('[data-testid="market-provider-tdx"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="market-provider-eastmoney"]').exists()).toBe(false)

    await wrapper.get('[data-testid="market-sync-online"]').trigger('click')
    await flushPromises()

    expect(marketApi.createOnlineSync).toHaveBeenCalledWith(
      expect.not.objectContaining({ provider: expect.anything() }),
    )
  })

  it('starts an East Money full-history backfill for the selected scope', async () => {
    vi.mocked(marketApi.createOnlineBackfill).mockResolvedValue({ job_id: 'backfill-1', status: 'queued' })
    vi.mocked(marketApi.getMarketDataJob).mockResolvedValue({
      job_id: 'backfill-1', status: 'completed', progress: 1, total_candidates: 2,
      total_scanned: 2, errors: 0, error: null, result: { source: 'online', provider: 'eastmoney', written_bars: 1200 },
    })
    const wrapper = mount(MarketDataPage)
    await flushPromises()

    await wrapper.get('[data-testid="market-sync-board-main"]').setValue(true)
    await wrapper.get('[data-testid="market-eastmoney-backfill"]').trigger('click')
    await flushPromises()

    expect(marketApi.createOnlineBackfill).toHaveBeenCalledWith({
      universe: 'all',
      boards: ['main'],
    })
  })

  it('shows the current synchronized instrument and date range', async () => {
    vi.mocked(marketApi.createOnlineSync).mockResolvedValue({ job_id: 'online-1', status: 'queued' })
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

  it('restores active market-data jobs and allows cancelling one', async () => {
    vi.mocked(marketApi.fetchActiveMarketDataJobs).mockResolvedValueOnce([{
      job_id: 'active-1',
      description: 'East Money历史全量回填',
      status: 'running',
      progress: 0.42,
      total_candidates: 5219,
      total_scanned: 2190,
      errors: 0,
      error: null,
      worker_count: 8,
      phase: 'online_bars',
      metadata: {
        provider: 'eastmoney',
        current_instrument: { market: 'SZ', code: '000001', name: '平安银行' },
        range_label: '正在读取完整日线…',
      },
      result: null,
    }]).mockResolvedValueOnce([])
    vi.mocked(marketApi.getMarketDataJob).mockResolvedValue({
      job_id: 'active-1',
      description: 'East Money历史全量回填',
      status: 'cancelled',
      progress: 0.42,
      total_candidates: 5219,
      total_scanned: 2190,
      errors: 0,
      error: '行情同步已取消',
      worker_count: 8,
      phase: 'cancelled',
      cancel_requested: true,
      result: null,
    })
    const wrapper = mount(MarketDataPage)
    await flushPromises()

    expect(wrapper.get('[data-testid="market-data-progress"]').text()).toContain('42%')
    expect(wrapper.get('[data-testid="market-data-progress"]').text()).toContain('SZ 000001 · 平安银行')
    expect(wrapper.find('[data-testid="market-active-jobs"]').exists()).toBe(false)
    expect(wrapper.findAll('[data-testid="market-data-progress"] .progress-track')).toHaveLength(1)

    await wrapper.get('[data-testid="market-sync-cancel"]').trigger('click')
    await flushPromises()

    expect(marketApi.cancelMarketDataJob).toHaveBeenCalledWith('active-1')
    expect(wrapper.get('[data-testid="market-data-message"]').text()).toContain('已取消')
  })

  it('does not show a store-open error while an active sync job is running', async () => {
    vi.mocked(marketApi.fetchStoreStatus).mockRejectedValueOnce(
      new FormulaScreenApiError('行情数据库无法打开，请检查文件权限或数据库完整性', 503),
    )
    vi.mocked(marketApi.fetchActiveMarketDataJobs).mockResolvedValueOnce([{
      job_id: 'active-store-write',
      description: 'East Money历史全量回填',
      status: 'running',
      progress: 0.1,
      total_candidates: 10,
      total_scanned: 1,
      errors: 0,
      error: null,
      worker_count: 8,
      phase: 'online_bars',
      metadata: { provider: 'eastmoney' },
      result: null,
    }])

    const wrapper = mount(MarketDataPage)
    await flushPromises()

    expect(wrapper.find('[data-testid="market-data-message"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="market-data-progress"]').text()).toContain('10%')
    expect(wrapper.find('[data-testid="market-active-jobs"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('shows staged download and merge progress in the single progress panel', async () => {
    vi.mocked(marketApi.fetchActiveMarketDataJobs).mockResolvedValueOnce([{
      job_id: 'staged-1',
      description: '行情同步',
      status: 'running',
      progress: 0.75,
      total_candidates: 8,
      total_scanned: 3,
      errors: 0,
      error: null,
      worker_count: 8,
      phase: 'merging',
      metadata: {
        execution_mode: 'process_pool_staging',
        merged_workers: 3,
        total_workers: 8,
        merged_rows: 1200,
      },
      result: null,
    }])

    const wrapper = mount(MarketDataPage)
    await flushPromises()

    const progress = wrapper.get('[data-testid="market-data-progress"]')
    expect(progress.text()).toContain('已合并 3 / 8 个分片')
    expect(progress.findAll('.progress-track')).toHaveLength(1)
    expect(wrapper.find('[data-testid="market-active-jobs"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('shows the security-list discovery stage while the sync is waiting', async () => {
    vi.useFakeTimers()
    try {
      vi.mocked(marketApi.createOnlineSync).mockResolvedValue({ job_id: 'waiting-1', status: 'queued' })
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
      expect(wrapper.get('[data-testid="market-data-progress"]').text()).toContain('等待 East Money 返回证券列表')

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
      vi.mocked(marketApi.createOnlineSync).mockResolvedValue({ job_id: 'slow-1', status: 'queued' })
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
          result: polls <= 601 ? null : { source: 'online', updated_files: 1703, written_bars: 1000 },
        }
      })
      const wrapper = mount(MarketDataPage)
      await flushPromises()

      await wrapper.get('[data-testid="market-sync-online"]').trigger('click')
      for (let attempt = 0; attempt < 602; attempt += 1) {
        await vi.advanceTimersByTimeAsync(500)
        await flushPromises()
      }

      expect(polls).toBeGreaterThan(600)
      expect(wrapper.get('[data-testid="market-data-progress"]').text()).not.toContain('行情同步超时')
    } finally {
      vi.useRealTimers()
    }
    // 600+ 次 fake timer 迭代在覆盖率插桩下会明显变慢，单独放宽超时。
  }, 20_000)

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
      expect(wrapper.get('[data-testid="market-data-progress"]').text()).toContain('100%')
      expect(wrapper.get('[data-testid="market-data-progress"]').text()).toContain('最近一次任务')

      await vi.advanceTimersByTimeAsync(500)
      await flushPromises()
    } finally {
      vi.clearAllTimers()
      vi.useRealTimers()
    }
  })

  it('toggles sync scope, MA overlays, and paginates the instrument list', async () => {
    vi.mocked(marketApi.fetchLocalInstruments).mockResolvedValue({
      items: instruments,
      meta: { total: 4, page: 1, page_size: 2, pages: 2 },
    })
    const wrapper = mount(MarketDataPage)
    await flushPromises()

    // 证券类型默认勾选「股票/B股」；可以取消再勾回。
    const syncType = wrapper.get<HTMLInputElement>('[data-testid="market-sync-type-stock"]')
    expect(syncType.element.checked).toBe(true)
    await syncType.setValue(false)
    expect(wrapper.get<HTMLInputElement>('[data-testid="market-sync-type-stock"]').element.checked).toBe(false)
    await syncType.setValue(true)
    expect(wrapper.get<HTMLInputElement>('[data-testid="market-sync-type-stock"]').element.checked).toBe(true)

    const ma5 = wrapper.get<HTMLInputElement>('[data-testid="toggle-ma5"]')
    expect(ma5.element.checked).toBe(true)
    await ma5.setValue(false)
    expect(wrapper.get<HTMLInputElement>('[data-testid="toggle-ma5"]').element.checked).toBe(false)

    const pagination = wrapper.get('[data-testid="market-pagination"]')
    expect(pagination.text()).toContain('1 / 2')
    await pagination.findAll('button')[1].trigger('click')
    await flushPromises()

    expect(marketApi.fetchLocalInstruments).toHaveBeenLastCalledWith(expect.objectContaining({ page: 2 }))
  })

  it('auto-fills the vipdoc path from backend detection', async () => {
    const wrapper = mount(MarketDataPage)
    await flushPromises()

    expect(marketApi.detectLocalVipdoc).toHaveBeenCalled()
    expect(wrapper.get<HTMLInputElement>('[data-testid="market-local-import-path"]').element.value).toBe('/data/vipdoc')
    expect(wrapper.get('[data-testid="market-local-import-note"]').text()).toContain('5400')
  })

  it('shows the mount hint when no vipdoc is reachable', async () => {
    vi.mocked(marketApi.detectLocalVipdoc).mockResolvedValue({
      candidates: [],
      default_path: '/data/vipdoc',
      mount_hint: '后端运行在容器里，需要先挂载。',
    })
    const wrapper = mount(MarketDataPage)
    await flushPromises()

    expect(wrapper.get('[data-testid="market-local-import-note"]').text()).toContain('没有在容器里找到')
    expect(wrapper.text()).toContain('docker-compose.tdx.yml')
    expect(wrapper.get<HTMLInputElement>('[data-testid="market-local-import-path"]').element.value).toBe('')
  })
})
