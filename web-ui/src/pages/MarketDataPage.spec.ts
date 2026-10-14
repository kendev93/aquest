import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { LocalInstrument, LocalMarketChart } from '../types'
import * as marketApi from '../api/marketData'
import MarketDataPage from './MarketDataPage.vue'
import { ApiError } from '../api/client'
import { INTERACTIVE_JOB_TIMEOUT_MS } from '../composables/usePolling'

vi.mock('../api/marketData', async () => {
  const actual = await vi.importActual<typeof import('../api/marketData')>('../api/marketData')
  return {
    ...actual,
    cancelMarketDataJob: vi.fn(),
    fetchLocalInstruments: vi.fn(),
    fetchLocalChart: vi.fn(),
    fetchStoreStatus: vi.fn(),
    fetchActiveMarketDataJobs: vi.fn(),
    createOnlineSync: vi.fn(),
    createOnlineRefresh: vi.fn(),
    createLocalImport: vi.fn(),
    deleteMarketBars: vi.fn(),
    createHealthCheck: vi.fn(),
    createHealthRepair: vi.fn(),
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
  let mountedWrappers: ReturnType<typeof mount>[] = []

  function mountPage() {
    const wrapper = mount(MarketDataPage)
    mountedWrappers.push(wrapper)
    return wrapper
  }

  beforeEach(() => {
    mountedWrappers = []
    vi.clearAllMocks()
    vi.mocked(marketApi.fetchLocalInstruments).mockResolvedValue({
      items: instruments,
      meta: { total: 2, page: 1, page_size: 50, pages: 1 },
    })
    vi.mocked(marketApi.fetchLocalChart).mockResolvedValue(chart)
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

  afterEach(() => {
    mountedWrappers.forEach((wrapper) => wrapper.unmount())
    mountedWrappers = []
  })

  it('drops a stale chart response when another instrument was picked afterwards', async () => {
    // 连点两只票时，先发的请求可能后到。旧实现拿到响应就无条件写 chart：选中的是
    // SZ 000001，图上画的却是 SH 600000，而且没有任何提示。
    const shChart: LocalMarketChart = { ...chart }
    const szChart: LocalMarketChart = { ...chart, market: 'SZ', code: '000001', total_daily_bars: 280 }
    let releaseStale: ((value: LocalMarketChart) => void) | undefined
    vi.mocked(marketApi.fetchLocalChart).mockImplementation((params) =>
      params.code === '600000'
        ? new Promise<LocalMarketChart>((resolve) => { releaseStale = resolve })
        : Promise.resolve(szChart),
    )

    const wrapper = mountPage()
    await flushPromises()

    await wrapper.get('[data-testid="instrument-SH-600000"]').trigger('click')
    await wrapper.get('[data-testid="instrument-SZ-000001"]').trigger('click')
    await flushPromises()

    expect(wrapper.get('[data-testid="market-chart-summary"]').text()).toContain('280 根')

    // 迟到的旧响应必须被丢弃，而不是把图表改回上一只票。
    releaseStale?.(shChart)
    await flushPromises()

    expect(wrapper.get('[data-testid="market-chart-summary"]').text()).toContain('280 根')
    expect(wrapper.get('[data-testid="market-chart-summary"]').text()).not.toContain('300 根')
  })

  it('lists local instruments and loads daily, monthly, and yearly charts', async () => {
    const wrapper = mountPage()
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

  it('shows the stored trading calendar and the latest automatic session check', async () => {
    vi.mocked(marketApi.fetchStoreStatus).mockResolvedValueOnce({
      database_path: '/data/market/market.duckdb',
      schema_version: 18,
      instrument_count: 2,
      bar_count: 580,
      data_start: '2020-01-01',
      data_end: '2026-09-30',
      last_online_sync_at: '2026-10-02T00:16:41+08:00',
      market_calendar_latest_date: '2026-09-30',
      market_calendar_source: 'eastmoney_sse_composite',
      last_auto_sync_check_date: '2026-10-02',
      last_auto_sync_check_session: '2026-09-30',
    })

    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.get('[data-testid="market-store-meta"]').text()).toContain(
      '最近交易日历 2026-09-30 · eastmoney_sse_composite',
    )
    expect(wrapper.get('[data-testid="market-store-meta"]').text()).toContain(
      '自动同步最近判定「不缺数据」 2026-10-02 · 截至交易日 2026-09-30',
    )
  })

  it('shows the persisted trading-calendar session used by automatic sync', async () => {
    vi.mocked(marketApi.fetchStoreStatus).mockResolvedValueOnce({
      database_path: '/data/market/market.duckdb',
      schema_version: 1,
      instrument_count: 2,
      bar_count: 580,
      data_start: '2020-01-01',
      data_end: '2026-09-30',
      last_online_sync_at: '2026-10-02T00:16:41+08:00',
      market_calendar_latest_date: '2026-09-30',
      market_calendar_source: 'eastmoney_sse_composite',
      last_auto_sync_check_date: '2026-10-02',
      last_auto_sync_check_session: '2026-09-30',
    })

    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.get('[data-testid="market-store-meta"]').text()).toContain(
      '最近交易日历 2026-09-30 · eastmoney_sse_composite',
    )
    expect(wrapper.get('[data-testid="market-store-meta"]').text()).toContain(
      '自动同步最近判定「不缺数据」 2026-10-02 · 截至交易日 2026-09-30',
    )
    // 日历没落后于行情时不提示，免得制造噪声。
    expect(wrapper.find('[data-testid="market-calendar-behind"]').exists()).toBe(false)
  })

  it('renders the store dates in China time and explains a calendar behind the bars', async () => {
    // 面板上那几个日期来自两条路径、两套钟：在线同步写的是带 +08:00 的时间戳，
    // 日历表的时间列是 UTC-naive（容器时区 UTC），而日历只在自动同步收盘后被写。
    // 不格式化、不解释，看着就是"日期错了"。
    vi.mocked(marketApi.fetchStoreStatus).mockResolvedValueOnce({
      database_path: '/data/market/market.duckdb',
      schema_version: 1,
      instrument_count: 2,
      bar_count: 580,
      data_start: '2020-01-01',
      data_end: '2026-10-13',
      last_online_sync_at: '2026-10-13T22:24:43.660850+08:00',
      market_calendar_latest_date: '2026-10-09',
      market_calendar_source: 'eastmoney_sse_composite',
      market_calendar_updated_at: '2026-10-13 13:46:43.407910',
      last_auto_sync_check_date: '2026-10-10',
      last_auto_sync_check_session: '2026-10-09',
    })

    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.get('[data-testid="market-last-online-sync"]').text())
      .toBe('最近在线更新 2026-10-13 22:24:43')
    expect(wrapper.get('[data-testid="market-calendar-meta"]').text())
      .toContain('表更新于 2026-10-13 21:46:43')
    expect(wrapper.get('[data-testid="market-calendar-behind"]').text())
      .toContain('交易日历停在 2026-10-09，行情已到 2026-10-13')
  })

  it('selects a data-bearing instrument when the first catalog item has no bars', async () => {
    vi.mocked(marketApi.fetchLocalInstruments).mockResolvedValueOnce({
      items: [
        { market: 'SH', code: '600001', name: '邯郸钢铁', instrument_type: 'stock', bars: 0, data_start: null, data_end: null, last_close: null, error: '没有可用日线数据' },
        instruments[0],
      ],
      meta: { total: 2, page: 1, page_size: 50, pages: 1 },
    })

    const wrapper = mountPage()
    await flushPromises()

    expect(marketApi.fetchLocalChart).toHaveBeenCalledWith(expect.objectContaining({ market: 'SH', code: '600000' }))
    expect(wrapper.get('[data-testid="market-chart-panel"]').text()).toContain('600000')
    expect(wrapper.find('[data-testid="market-data-message"]').exists()).toBe(false)
  })

  it('shows a user-facing error when the local directory cannot be read', async () => {
    vi.mocked(marketApi.fetchLocalInstruments).mockRejectedValueOnce(new ApiError('vipdoc 路径无效', 422))
    const wrapper = mountPage()
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
    const wrapper = mountPage()
    await flushPromises()

    await wrapper.get('[data-testid="market-sync-online"]').trigger('click')
    await flushPromises()

    expect(marketApi.createOnlineSync).toHaveBeenCalledWith({
      universe: 'all',
      instrument_types: ['stock'],
    })
    expect(wrapper.get('[data-testid="market-data-progress"]').text()).not.toContain('文件导入')
    expect(wrapper.get('[data-testid="market-data-progress"]').text()).toContain('更新标的 1')
    expect(wrapper.get('[data-testid="market-data-progress"]').text()).toContain('写入 K 线 1')
    expect(wrapper.get('[data-testid="market-data-progress"]').text()).toContain('证券列表 4')
    expect(wrapper.get('[data-testid="market-data-progress"]').text()).toContain('新增标的 3')
  })

  it('keeps the upstream data source out of the sync UI', async () => {
    vi.mocked(marketApi.createOnlineSync).mockResolvedValue({ job_id: 'online-2', status: 'queued' })
    vi.mocked(marketApi.getMarketDataJob).mockResolvedValue({
      job_id: 'online-2', status: 'completed', progress: 1, total_candidates: 0,
      total_scanned: 0, errors: 0, error: null, result: { source: 'online', provider: 'eastmoney' },
      metadata: { provider: 'eastmoney', history_source: 'eastmoney' },
    })
    const wrapper = mountPage()
    await flushPromises()

    // 行情源属于内部实现细节，界面不暴露：进度面板和仓储元信息里都不该出现
    // provider / 接口名（"East Money"、"eastmoney" 等）。
    expect(wrapper.text()).not.toContain('East Money')
    expect(wrapper.text()).not.toContain('eastmoney')

    await wrapper.get('[data-testid="market-sync-online"]').trigger('click')
    await flushPromises()

    const progress = wrapper.get('[data-testid="market-data-progress"]').text()
    expect(progress).not.toContain('eastmoney')
    expect(progress).not.toContain('East Money')
    expect(progress).not.toContain('数据源')
    expect(marketApi.createOnlineSync).toHaveBeenCalledWith(
      expect.not.objectContaining({ provider: expect.anything() }),
    )
  })

  it('sends the selected instrument types and boards with the merged sync', async () => {
    vi.mocked(marketApi.createOnlineSync).mockResolvedValue({ job_id: 'online-3', status: 'queued' })
    vi.mocked(marketApi.getMarketDataJob).mockResolvedValue({
      job_id: 'online-3', status: 'completed', progress: 1, total_candidates: 2,
      total_scanned: 2, errors: 0, error: null, result: { source: 'online', provider: 'eastmoney', written_bars: 1200 },
    })
    const wrapper = mountPage()
    await flushPromises()

    await wrapper.get('[data-testid="market-sync-board-main"]').setValue(true)
    await wrapper.get('[data-testid="market-sync-online"]').trigger('click')
    await flushPromises()

    // 类型和板块必须一起带上：以前回填硬编码只发股票，勾了 ETF 也补不到。
    // 两个入口合成一个之后，这条约束落在同步上，不能再退回去。
    expect(marketApi.createOnlineSync).toHaveBeenCalledWith({
      universe: 'all',
      instrument_types: ['stock'],
      boards: ['main'],
    })
  })

  it('offers one sync action plus the destructive re-download, and stops claiming an 800-bar window', async () => {
    // 「拉最近 800 根」是从没人消费的 bars 字段脑补出来的描述：区间只由标的自己的覆盖
    // 决定——库里没有就从 2015-01-01 全量下，已有就补到今天并回看 10 天做逐值对照。
    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.find('[data-testid="market-online-backfill"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="market-sync-online"]').text()).toBe('同步行情')
    expect(wrapper.get('[data-testid="market-online-refresh"]').text()).toBe('删除并重下全部历史')
    const helper = wrapper.get('[data-testid="market-sync-helper"]').text()
    expect(helper).not.toContain('800')
    expect(helper).toContain('从 2015-01-01 全量下载')
    expect(helper).toContain('最近 10 天')
  })

  it('refreshes the selected scope from scratch after an explicit confirmation', async () => {
    vi.mocked(marketApi.createOnlineRefresh).mockResolvedValue({ job_id: 'refresh-1', status: 'queued' })
    vi.mocked(marketApi.getMarketDataJob).mockResolvedValue({
      job_id: 'refresh-1', status: 'completed', progress: 1, total_candidates: 2,
      total_scanned: 2, errors: 0, error: null, result: {
        source: 'online', written_bars: 1200, deleted_bars: 1180,
        refreshed_instruments: 2, empty_after_refresh: ['000001'],
      },
    })
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    const wrapper = mountPage()
    await flushPromises()

    await wrapper.get('[data-testid="market-sync-board-main"]').setValue(true)
    await wrapper.get('[data-testid="market-online-refresh"]').trigger('click')
    await flushPromises()

    // 范围和另外两个按钮保持一致，另外必须带 confirm：这个入口会先删后下。
    expect(marketApi.createOnlineRefresh).toHaveBeenCalledWith({
      universe: 'all',
      instrument_types: ['stock'],
      boards: ['main'],
      confirm: true,
    })
    const message = wrapper.get('[data-testid="market-data-message"]').text()
    expect(message).toContain('删除 1180 根')
    expect(message).toContain('重写 1200 根')
    // 删了没补回来的标的要点名，否则"重刷"会静默变成删数据。
    expect(message).toContain('000001')
    confirmSpy.mockRestore()
  })

  it('does not refresh anything when the confirmation is dismissed', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    const wrapper = mountPage()
    await flushPromises()

    await wrapper.get('[data-testid="market-online-refresh"]').trigger('click')
    await flushPromises()

    expect(marketApi.createOnlineRefresh).not.toHaveBeenCalled()
    confirmSpy.mockRestore()
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
    const wrapper = mountPage()
    await flushPromises()

    await wrapper.get('[data-testid="market-sync-online"]').trigger('click')
    await flushPromises()

    expect(wrapper.get('[data-testid="market-sync-current"]').text()).toContain('SH 600000 · 浦发银行')
    expect(wrapper.get('[data-testid="market-sync-current"]').text()).toContain('最新 800 根日线')
  })

  it('restores active market-data jobs and allows cancelling one', async () => {
    vi.mocked(marketApi.fetchActiveMarketDataJobs).mockResolvedValueOnce([{
      job_id: 'active-1',
      description: '历史全量回填',
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
      description: '历史全量回填',
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
    const wrapper = mountPage()
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
      new ApiError('行情数据库无法打开，请检查文件权限或数据库完整性', 503),
    )
    vi.mocked(marketApi.fetchActiveMarketDataJobs).mockResolvedValueOnce([{
      job_id: 'active-store-write',
      description: '历史全量回填',
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

    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.find('[data-testid="market-data-message"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="market-data-progress"]').text()).toContain('10%')
    expect(wrapper.find('[data-testid="market-active-jobs"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('loads the instrument catalog once the job that was running at mount is gone', async () => {
    // 挂载时有任务在跑，onMounted 的守卫会跳过首屏目录加载；任务结束那一次后台刷新必须
    // 补上，否则品种列表和 K 线一直空白，直到用户手动再点一次同步或刷新浏览器。
    vi.useFakeTimers()
    try {
      vi.mocked(marketApi.fetchActiveMarketDataJobs)
        .mockResolvedValueOnce([{
          job_id: 'active-catalog',
          description: '历史全量回填',
          status: 'running',
          progress: 0.4,
          total_candidates: 10,
          total_scanned: 4,
          errors: 0,
          error: null,
          worker_count: 8,
          phase: 'online_bars',
          metadata: { provider: 'eastmoney' },
          result: null,
        }])
        .mockResolvedValue([])
      vi.mocked(marketApi.getMarketDataJob).mockResolvedValue({
        job_id: 'active-catalog',
        status: 'completed',
        progress: 1,
        total_candidates: 10,
        total_scanned: 10,
        errors: 0,
        error: null,
        result: { source: 'online', updated_files: 1, written_bars: 1 },
      })

      mountPage()
      await flushPromises()
      expect(marketApi.fetchLocalInstruments).not.toHaveBeenCalled()

      await vi.advanceTimersByTimeAsync(1000)
      await flushPromises()

      expect(marketApi.fetchLocalInstruments).toHaveBeenCalled()
    } finally {
      vi.clearAllTimers()
      vi.useRealTimers()
    }
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

    const wrapper = mountPage()
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
      const wrapper = mountPage()
      await flushPromises()

      const click = wrapper.get('[data-testid="market-sync-online"]').trigger('click')
      await flushPromises()

      expect(wrapper.get('[data-testid="market-data-progress"]').text()).toContain('正在刷新证券列表')
      expect(wrapper.get('[data-testid="market-data-progress"]').text()).toContain('等待在线证券列表返回')

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
    let cleanup: () => void = () => {}
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
      const wrapper = mountPage()
      cleanup = () => wrapper.unmount()
      await flushPromises()

      await wrapper.get('[data-testid="market-sync-online"]').trigger('click')
      for (let attempt = 0; attempt < 602; attempt += 1) {
        await vi.advanceTimersByTimeAsync(500)
        await flushPromises()
      }

      expect(polls).toBeGreaterThan(600)
      expect(wrapper.get('[data-testid="market-data-progress"]').text()).not.toContain('行情同步超时')
    } finally {
      cleanup()
      vi.clearAllTimers()
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
      const wrapper = mountPage()
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
    const wrapper = mountPage()
    await flushPromises()

    // 证券类型默认勾选「股票」；可以取消再勾回。
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
    const wrapper = mountPage()
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
    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.get('[data-testid="market-local-import-note"]').text()).toContain('没有在容器里找到')
    expect(wrapper.text()).toContain('docker-compose.tdx.yml')
    expect(wrapper.get<HTMLInputElement>('[data-testid="market-local-import-path"]').element.value).toBe('')
  })

  it('imports local files without the online default of stocks only', async () => {
    // 本地导入曾经复用在线的范围（默认只勾股票），未勾选的板块会被静默过滤掉。
    vi.mocked(marketApi.createLocalImport).mockResolvedValue({ job_id: 'local-1', status: 'queued' })
    vi.mocked(marketApi.getMarketDataJob).mockResolvedValue({
      job_id: 'local-1', status: 'completed', progress: 1, total_candidates: 1,
      total_scanned: 1, errors: 0, error: null, result: {
        source: 'local', discovered_files: 10, selected_files: 10, filtered_files: 0,
        imported_instruments: 10, imported_bars: 5000, replaced_bars: 0, skipped_files: 0,
      },
    })
    const wrapper = mountPage()
    await flushPromises()

    await wrapper.get('[data-testid="market-local-import"]').trigger('click')
    await flushPromises()

    // 不勾任何范围 = 不限制，不能把在线默认的 instrument_types 带过去。
    expect(marketApi.createLocalImport).toHaveBeenCalledWith({
      universe: 'all',
      vipdoc_path: '/data/vipdoc',
    })
  })

  it('renders the local import report with its own keys', async () => {
    // 结果面板以前只认在线同步那套键名，导入 5000 根后显示「写入 K 线 0」。
    vi.mocked(marketApi.createLocalImport).mockResolvedValue({ job_id: 'local-2', status: 'queued' })
    vi.mocked(marketApi.getMarketDataJob).mockResolvedValue({
      job_id: 'local-2', status: 'completed', progress: 1, total_candidates: 1,
      total_scanned: 1, errors: 1, error: null, result: {
        source: 'local', discovered_files: 12, selected_files: 10, filtered_files: 2,
        imported_instruments: 9, imported_bars: 5000, replaced_bars: 300, skipped_files: 1,
        range_start: '2024-01-02', range_end: '2026-09-15',
        failure_reasons: { day_file_truncated: 1 },
      },
    })
    const wrapper = mountPage()
    await flushPromises()

    await wrapper.get('[data-testid="market-local-import"]').trigger('click')
    await flushPromises()

    const result = wrapper.get('[data-testid="local-import-result"]').text()
    expect(result).toContain('写入 K 线 5000')
    expect(result).toContain('范围外跳过 2')
    expect(result).toContain('day_file_truncated × 1')
  })

  it('undoes a local import through the delete endpoint', async () => {
    // 本地行永久压过在线行，导错了必须能撤销，否则只能重建数据库。
    vi.mocked(marketApi.deleteMarketBars).mockResolvedValue({ deleted_bars: 4321, source: 'local' })
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    const wrapper = mountPage()
    await flushPromises()

    await wrapper.get('[data-testid="market-local-import-undo"]').trigger('click')
    await flushPromises()

    expect(marketApi.deleteMarketBars).toHaveBeenCalledWith({
      source: 'local',
      universe: 'all',
      confirm: true,
    })
    expect(wrapper.get('[data-testid="market-data-message"]').text()).toContain('4321')
    confirmSpy.mockRestore()
  })

  it('does not delete anything when the undo prompt is dismissed', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    const wrapper = mountPage()
    await flushPromises()

    await wrapper.get('[data-testid="market-local-import-undo"]').trigger('click')
    await flushPromises()

    expect(marketApi.deleteMarketBars).not.toHaveBeenCalled()
    confirmSpy.mockRestore()
  })

  it('offers only A-share stocks and boards, with nothing greyed out', async () => {
    const wrapper = mountPage()
    await flushPromises()

    // 只做 A 股：指数/ETF/债券/B股这些选项根本不再渲染，而不是置灰。
    for (const gone of ['index', 'fund', 'bond']) {
      expect(wrapper.find(`[data-testid="market-sync-type-${gone}"]`).exists()).toBe(false)
    }
    expect(wrapper.find('[data-testid="market-sync-board-b_share"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="market-local-board-b_share"]').exists()).toBe(false)
    // 在线与本地现在是同一套范围，没有需要置灰的项。
    expect(wrapper.get<HTMLInputElement>('[data-testid="market-sync-type-stock"]').element.disabled).toBe(false)
    expect(wrapper.get<HTMLInputElement>('[data-testid="market-sync-board-main"]').element.disabled).toBe(false)
  })

  it('filters the catalog, pages through it and narrows the local import scope', async () => {
    vi.mocked(marketApi.fetchLocalInstruments).mockResolvedValue({
      items: instruments,
      meta: { total: 4, page: 1, page_size: 2, pages: 2 },
    })
    const wrapper = mountPage()
    await flushPromises()

    // 本地导入范围独立于在线同步范围。
    await wrapper.get('[data-testid="market-local-type-stock"]').setValue(true)
    await wrapper.get('[data-testid="market-local-board-main"]').setValue(true)

    await wrapper.get('[data-testid="market-scope"]').setValue('SH')
    await wrapper.get('[data-testid="market-keyword"]').setValue('600')
    await wrapper.get('form.market-filter-form').trigger('submit')
    await flushPromises()

    expect(marketApi.fetchLocalInstruments).toHaveBeenLastCalledWith(expect.objectContaining({
      market: 'SH', keyword: '600', page: 1,
    }))

    const pageButtons = wrapper.findAll('[data-testid="market-pagination"] button')
    expect(pageButtons).toHaveLength(2)
    await pageButtons[1].trigger('click')
    await flushPromises()
    expect(marketApi.fetchLocalInstruments).toHaveBeenLastCalledWith(expect.objectContaining({ page: 2 }))
    expect(wrapper.get('[data-testid="market-pagination"]').text()).toContain('2 / 2')
  })

  it('reloads a chart with an explicit date range and indicator switches', async () => {
    const wrapper = mountPage()
    await flushPromises()
    await wrapper.get('[data-testid="instrument-SH-600000"]').trigger('click')
    await flushPromises()

    vi.mocked(marketApi.fetchLocalChart).mockClear()
    await wrapper.get('[data-testid="market-chart-start"]').setValue('2024-12-01')
    await wrapper.get('[data-testid="market-chart-end"]').setValue('2024-12-31')
    await wrapper.get('[data-testid="market-chart-refresh"]').trigger('click')
    await flushPromises()

    expect(marketApi.fetchLocalChart).toHaveBeenCalledWith(expect.objectContaining({
      start_date: '2024-12-01', end_date: '2024-12-31',
    }))

    await wrapper.get('[data-testid="toggle-ma5"]').setValue(false)
    await wrapper.get('[data-testid="toggle-ma20"]').setValue(true)
    await wrapper.get('[data-testid="toggle-rsi"]').setValue(false)
    await wrapper.get('[data-testid="toggle-macd"]').setValue(false)

    expect(wrapper.get('[data-testid="market-candlestick-chart"]').text()).not.toContain('RSI')
  })

  it('runs the read-only health check and lists the flagged instruments', async () => {
    vi.mocked(marketApi.createHealthCheck).mockResolvedValue({ job_id: 'health-1', status: 'queued' })
    vi.mocked(marketApi.getMarketDataJob).mockResolvedValue(healthJobState(healthReport()))
    const wrapper = mountPage()
    await flushPromises()

    await wrapper.get('[data-testid="market-health-check"]').trigger('click')
    await flushPromises()

    expect(marketApi.createHealthCheck).toHaveBeenCalled()
    expect(wrapper.get('[data-testid="market-health-table"]').text()).toContain('行情缺口')
    expect(wrapper.get('[data-testid="market-health-row-gap"]').text()).toContain('异常')
    expect(wrapper.get('[data-testid="market-health-row-gap"]').text()).toContain('缺 3 个交易日')
    expect(wrapper.get('[data-testid="market-health-row-gap"]').text()).toContain('000001')
    // 干净的一项必须显示正常，否则用户没法分辨"没查"和"查了没问题"。
    expect(wrapper.get('[data-testid="market-health-row-ohlc"]').text()).toContain('正常')
  })

  it('refuses to read an empty or malformed health payload as a clean store', async () => {
    // 体检的价值在于"它说安全时真的安全"。以前这里是一句 `as unknown as`：`checks` 只要
    // 是真值就照单全收，空数组因此被渲染成「六项检查都没有命中」，而字段漂移会让
    // healthIssues 静默变空、把"一键修复"（先删后下）悄悄禁用。
    vi.mocked(marketApi.createHealthCheck).mockResolvedValue({ job_id: 'health-1', status: 'queued' })

    vi.mocked(marketApi.getMarketDataJob).mockResolvedValue(
      healthJobState({ ...healthReport(), checks: [] }),
    )
    const empty = mountPage()
    await flushPromises()
    await empty.get('[data-testid="market-health-check"]').trigger('click')
    await flushPromises()

    expect(empty.find('[data-testid="market-health-clean"]').exists()).toBe(false)
    expect(empty.get('[data-testid="market-health-message"]').text()).toContain('体检没有返回报告')

    vi.mocked(marketApi.getMarketDataJob).mockResolvedValue(
      healthJobState({ ...healthReport(), checks: [{ label: '行情缺口' }] }),
    )
    const malformed = mountPage()
    await flushPromises()
    await malformed.get('[data-testid="market-health-check"]').trigger('click')
    await flushPromises()

    expect(malformed.find('[data-testid="market-health-clean"]').exists()).toBe(false)
    expect(malformed.get('[data-testid="market-health-message"]').text()).toContain('体检没有返回报告')
  })

  it('stops waiting on a stuck health check after the interactive budget', async () => {
    // 体检正常是十秒级：卡住就停止等待，别把面板锁在「检查中」。
    vi.useFakeTimers()
    vi.mocked(marketApi.createHealthCheck).mockResolvedValue({ job_id: 'health-1', status: 'queued' })
    vi.mocked(marketApi.getMarketDataJob).mockResolvedValue({
      ...healthJobState({}),
      status: 'running',
      progress: 0.4,
      result: null,
    })
    const wrapper = mountPage()
    await vi.advanceTimersByTimeAsync(0)
    await wrapper.get('[data-testid="market-health-check"]').trigger('click')
    await vi.advanceTimersByTimeAsync(INTERACTIVE_JOB_TIMEOUT_MS + 1_000)

    expect(wrapper.get('[data-testid="market-health-message"]').text()).toContain('任务仍在后台运行')
    expect(wrapper.get('[data-testid="market-health-check"]').attributes('disabled')).toBeUndefined()
    vi.useRealTimers()
  })

  it('repairs only the flagged check after an explicit confirmation', async () => {
    vi.mocked(marketApi.createHealthCheck).mockResolvedValue({ job_id: 'health-1', status: 'queued' })
    vi.mocked(marketApi.getMarketDataJob).mockResolvedValue(healthJobState(healthReport()))
    vi.mocked(marketApi.createHealthRepair).mockResolvedValue({ job_id: 'repair-1', status: 'queued' })
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    const wrapper = mountPage()
    await flushPromises()

    await wrapper.get('[data-testid="market-health-check"]').trigger('click')
    await flushPromises()
    vi.mocked(marketApi.getMarketDataJob).mockResolvedValue(
      healthJobState({
        requested_codes: 1,
        deleted_bars: 77,
        written_bars: 60,
        errors: 0,
        empty_after_repair: ['000001'],
      }),
    )
    await wrapper.get('[data-testid="market-health-fix-gap"]').trigger('click')
    await flushPromises()

    expect(confirmSpy).toHaveBeenCalled()
    expect(marketApi.createHealthRepair).toHaveBeenCalledWith({ checks: ['gap'], confirm: true })
    const result = wrapper.get('[data-testid="market-health-repair-result"]').text()
    expect(result).toContain('删除 77 根')
    expect(result).toContain('重写 60 根')
    // 删了没补回来的标的必须点名，否则"修复"会静默变成删库。
    expect(wrapper.get('[data-testid="market-health-empty-codes"]').text()).toContain('000001')
    confirmSpy.mockRestore()
  })

  it('keeps waiting on a repair that legitimately runs for hours', async () => {
    // 先删后下的全市场修复不是「卡住」：给它套交互预算会把正常任务误报成超时，
    // 所以 waitForHealthJob 只有体检传预算。
    vi.useFakeTimers()
    vi.mocked(marketApi.createHealthCheck).mockResolvedValue({ job_id: 'health-1', status: 'queued' })
    vi.mocked(marketApi.createHealthRepair).mockResolvedValue({ job_id: 'repair-1', status: 'queued' })
    vi.mocked(marketApi.getMarketDataJob).mockResolvedValue(healthJobState(healthReport()))
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    const wrapper = mountPage()
    await vi.advanceTimersByTimeAsync(0)
    await wrapper.get('[data-testid="market-health-check"]').trigger('click')
    await vi.advanceTimersByTimeAsync(0)

    vi.mocked(marketApi.getMarketDataJob).mockResolvedValue({
      ...healthJobState({}),
      status: 'running',
      progress: 0.1,
      result: null,
    })
    await wrapper.get('[data-testid="market-health-repair"]').trigger('click')
    await vi.advanceTimersByTimeAsync(INTERACTIVE_JOB_TIMEOUT_MS + 1_000)

    expect(wrapper.get('[data-testid="market-health-repair"]').text()).toContain('修复中')
    expect(wrapper.find('[data-testid="market-health-message"]').exists()).toBe(false)
    confirmSpy.mockRestore()
    vi.useRealTimers()
  })

  it('does not call the repair endpoint when the user cancels the confirmation', async () => {
    vi.mocked(marketApi.createHealthCheck).mockResolvedValue({ job_id: 'health-1', status: 'queued' })
    vi.mocked(marketApi.getMarketDataJob).mockResolvedValue(healthJobState(healthReport()))
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    const wrapper = mountPage()
    await flushPromises()

    await wrapper.get('[data-testid="market-health-check"]').trigger('click')
    await flushPromises()
    await wrapper.get('[data-testid="market-health-repair"]').trigger('click')
    await flushPromises()

    expect(marketApi.createHealthRepair).not.toHaveBeenCalled()
    confirmSpy.mockRestore()
  })

  it('keeps the health panel empty until a check has run', async () => {
    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.get('[data-testid="market-health-empty"]').text()).toContain('还没有检查结果')
    expect(wrapper.find('[data-testid="market-health-table"]').exists()).toBe(false)
  })
})

function healthReport(): Record<string, unknown> {
  return {
    generated_at: '2024-03-01T00:00:00',
    instrument_count: 2,
    bar_count: 157,
    calendar_days: 60,
    data_start: '2024-01-01',
    data_end: '2024-02-29',
    duration_seconds: 9.8,
    checks: [
      {
        key: 'gap',
        label: '行情缺口',
        description: '标的在自己首末日期之间本该有日线却没有的交易日。',
        severity: 'error',
        affected_instruments: 1,
        occurrences: 3,
        repairable: true,
        examples: [
          {
            market: 'SZ',
            code: '000001',
            name: '平安银行',
            trade_date: '2024-01-10',
            detail: '共缺 3 个交易日，分 2 段（最早缺失 2024-01-10）',
          },
        ],
      },
      {
        key: 'ohlc',
        label: '价格结构错误',
        description: '最高价低于最低价或价格不为正数。',
        severity: 'ok',
        affected_instruments: 0,
        occurrences: 0,
        repairable: true,
        examples: [],
      },
    ],
  }
}

function healthJobState(result: Record<string, unknown>) {
  return {
    job_id: 'health-1',
    status: 'completed' as const,
    progress: 1,
    total_candidates: 1,
    total_scanned: 1,
    errors: 0,
    error: null,
    result,
  }
}
