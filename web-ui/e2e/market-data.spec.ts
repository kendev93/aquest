import { expect, test } from '@playwright/test'

const instruments = {
  data: [
    { market: 'SH', code: '600000', instrument_type: 'stock', bars: 320, data_start: '2020-01-01', data_end: '2024-12-31', last_close: 12.5, error: null },
    { market: 'SZ', code: '000001', instrument_type: 'stock', bars: 300, data_start: '2020-01-01', data_end: '2024-12-31', last_close: 10.2, error: null },
  ],
  meta: { total: 2, page: 1, page_size: 50, pages: 1 },
}

const chart = (period: 'daily' | 'monthly' | 'yearly') => ({
  data: {
    market: 'SH', code: '600000', period, total_daily_bars: 320, bars: 3,
    available_data_start: '2020-01-01', available_data_end: '2024-12-31', data_start: '2024-12-27', data_end: '2024-12-31',
    candles: [
      { date: '2024-12-27', open: 10, high: 12, low: 9, close: 11, volume: 100, amount: 1100, ma: { ma5: null, ma10: null, ma20: null, ma60: null }, rsi14: null, macd: null, macd_signal: null, macd_histogram: null },
      { date: '2024-12-30', open: 11, high: 13, low: 10, close: 12, volume: 120, amount: 1440, ma: { ma5: null, ma10: null, ma20: null, ma60: null }, rsi14: null, macd: null, macd_signal: null, macd_histogram: null },
      { date: '2024-12-31', open: 12, high: 14, low: 11, close: 13, volume: 140, amount: 1820, ma: { ma5: 11.5, ma10: null, ma20: null, ma60: null }, rsi14: 68, macd: 0.5, macd_signal: 0.4, macd_histogram: 0.1 },
    ],
  },
})

test('user can browse local instruments and switch chart periods', async ({ page }) => {
  await page.route('**/api/v1/market-data/store', (route) => route.fulfill({ json: { data: {
    database_path: '/data/market/market.duckdb', schema_version: 1, instrument_count: 2,
    bar_count: 620, data_start: '2020-01-01', data_end: '2024-12-31',
    last_online_sync_at: null,
  } } }))
  await page.route('**/api/v1/market-data/local/instruments**', (route) => route.fulfill({ json: instruments }))
  await page.route('**/api/v1/market-data/local/SH/600000/bars*', async (route) => {
    const url = new URL(route.request().url())
    await route.fulfill({ json: chart((url.searchParams.get('period') as 'daily' | 'monthly' | 'yearly') || 'daily') })
  })

  await page.goto('/market-data')
  await expect(page.getByTestId('local-instruments')).toContainText('600000')
  await expect(page.getByTestId('market-candlestick-chart')).toBeVisible()
  await page.getByTestId('chart-period-monthly').click()
  await expect(page.getByTestId('market-chart-panel')).toContainText('2024-12-27')
  await page.getByTestId('toggle-ma5').uncheck()
  await expect(page.getByTestId('market-candlestick-chart')).toBeVisible()
})

test('user can start a full-history backfill', async ({ page }) => {
  let backfillRequest: unknown
  await page.route('**/api/v1/market-data/store', (route) => route.fulfill({ json: { data: {
    database_path: '/data/market/market.duckdb', schema_version: 12, instrument_count: 1,
    bar_count: 100, data_start: '2010-01-01', data_end: '2026-09-10',
    last_online_sync_at: null,
  } } }))
  await page.route('**/api/v1/market-data/local/instruments**', (route) => route.fulfill({ json: instruments }))
  await page.route('**/api/v1/market-data/local/SH/600000/bars*', (route) => route.fulfill({ json: chart('daily') }))
  await page.route('**/api/v1/market-data/backfill', async (route) => {
    backfillRequest = route.request().postDataJSON()
    await route.fulfill({ json: { data: { job_id: 'backfill-1', status: 'queued' } } })
  })
  await page.route('**/api/v1/market-data/jobs/backfill-1', (route) => route.fulfill({ json: { data: {
    job_id: 'backfill-1', status: 'completed', progress: 1, total_candidates: 2,
    total_scanned: 2, errors: 0, error: null,
    result: { source: 'online', provider: 'eastmoney', written_bars: 100 },
  } } }))

  await page.goto('/market-data')
  await page.getByTestId('market-online-backfill').click()
  // 上游行情源是内部实现细节：结果里带着 provider，但界面不展示它。
  await expect(page.getByTestId('market-data-progress')).toContainText('写入 K 线 100')
  await expect(page.getByTestId('market-data-progress')).not.toContainText('eastmoney')
  // 回填现在把证券类型一起带过去，勾了 ETF 也能补齐。
  expect(backfillRequest).toEqual({ universe: 'all', instrument_types: ['stock'] })
})

test('user can see and cancel an active market-data job', async ({ page }) => {
  const activeJob = {
    job_id: 'active-1', description: '历史全量回填', status: 'running', progress: 0.42,
    total_candidates: 5219, total_scanned: 2190, errors: 0, error: null, worker_count: 8,
    phase: 'online_bars', metadata: {
      provider: 'eastmoney',
      current_instrument: { market: 'SZ', code: '000001', name: '平安银行' },
      range_label: '正在读取完整日线…',
    }, result: null,
  }
  let activeReads = 0
  let cancelRequest: unknown
  await page.route('**/api/v1/market-data/store', (route) => route.fulfill({ json: { data: {
    database_path: '/data/market/market.duckdb', schema_version: 12, instrument_count: 1,
    bar_count: 100, data_start: '2010-01-01', data_end: '2026-09-10',
    last_online_sync_at: null,
  } } }))
  await page.route('**/api/v1/market-data/local/instruments**', (route) => route.fulfill({ json: instruments }))
  await page.route('**/api/v1/market-data/local/SH/600000/bars*', (route) => route.fulfill({ json: chart('daily') }))
  // 任务在用户点取消前必须一直"活着"：页面每秒轮询活动任务，若中途返回空列表，
  // 页面会把任务判定为已结束并移除进度条，取消按钮随之消失（历史 flaky 根因）。
  await page.route('**/api/v1/market-data/jobs/active*', (route) => {
    activeReads += 1
    return route.fulfill({ json: { data: [activeJob] } })
  })
  await page.route('**/api/v1/market-data/jobs/active-1/cancel', async (route) => {
    cancelRequest = route.request().method()
    await route.fulfill({ json: { data: { job_id: 'active-1', status: 'cancelling', cancel_requested: true } } })
  })
  await page.route('**/api/v1/market-data/jobs/active-1', (route) => route.fulfill({ json: { data: {
    ...activeJob, status: 'cancelled', phase: 'cancelled', cancel_requested: true, error: '行情同步已取消',
  } } }))

  await page.goto('/market-data')
  await expect(page.getByTestId('market-data-progress')).toContainText('42%')
  await expect(page.getByTestId('market-data-progress')).toContainText('SZ 000001 · 平安银行')
  await page.getByTestId('market-sync-cancel').click()
  await expect(page.getByTestId('market-data-message')).toContainText('已请求取消')
  expect(cancelRequest).toBe('POST')
})
