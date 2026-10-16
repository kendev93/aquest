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
  // 没 mock 的 /jobs/active 会穿过 vite 代理打到真实后端：那台容器只要正在跑自动同步，
  // 页面就会进入"有任务在跑"的状态，列表被清空、同步按钮禁用，测试红的还不是自己的代码。
  await page.route('**/api/v1/market-data/jobs/active*', (route) => route.fulfill({ json: { data: [] } }))
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

test('user can start a market-data sync', async ({ page }) => {
  let syncRequest: unknown
  await page.route('**/api/v1/market-data/jobs/active*', (route) => route.fulfill({ json: { data: [] } }))
  await page.route('**/api/v1/market-data/store', (route) => route.fulfill({ json: { data: {
    database_path: '/data/market/market.duckdb', schema_version: 12, instrument_count: 1,
    bar_count: 100, data_start: '2010-01-01', data_end: '2026-09-10',
    last_online_sync_at: null,
  } } }))
  await page.route('**/api/v1/market-data/local/instruments**', (route) => route.fulfill({ json: instruments }))
  await page.route('**/api/v1/market-data/local/SH/600000/bars*', (route) => route.fulfill({ json: chart('daily') }))
  await page.route('**/api/v1/market-data/sync-online', async (route) => {
    syncRequest = route.request().postDataJSON()
    await route.fulfill({ json: { data: { job_id: 'online-e2e-1', status: 'queued' } } })
  })
  await page.route('**/api/v1/market-data/jobs/online-e2e-1', (route) => route.fulfill({ json: { data: {
    job_id: 'online-e2e-1', status: 'completed', progress: 1, total_candidates: 2,
    total_scanned: 2, errors: 0, error: null,
    result: { source: 'online', provider: 'eastmoney', written_bars: 100 },
  } } }))

  await page.goto('/market-data')
  await page.getByTestId('market-sync-online').click()
  // 上游行情源是内部实现细节：结果里带着 provider，但界面不展示它。
  await expect(page.getByTestId('market-data-progress')).toContainText('写入 K 线 100')
  await expect(page.getByTestId('market-data-progress')).not.toContainText('eastmoney')
  // 同步把证券类型一起带过去（勾了 ETF 也能同步），并且不再发那个无人消费的 bars。
  expect(syncRequest).toEqual({ universe: 'all', instrument_types: ['stock'] })
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
  let cancelRequested = false
  let cancelReads = 0
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
  // 取消之后先让「已请求取消」这句提示站稳两次轮询，再把任务从活动列表里摘掉——
  // 终态文案要页面对上「列表里没了 + 详情已是 cancelled」两步才会出现。
  await page.route('**/api/v1/market-data/jobs/active*', (route) => {
    const listed = !cancelRequested || (cancelReads += 1) < 2
    return route.fulfill({ json: { data: listed ? [activeJob] : [] } })
  })
  await page.route('**/api/v1/market-data/jobs/active-1/cancel', async (route) => {
    cancelRequest = route.request().method()
    cancelRequested = true
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
  // 任务消失后页面会去读详情并给出终态；只验「已请求取消」等于没验取消真的收尾了。
  await expect(page.getByTestId('market-data-message')).toContainText('已取消，已经写入的数据已保留')
  expect(cancelRequest).toBe('POST')
})

const healthReport = {
  generated_at: '2026-10-16T13:33:20', instrument_count: 5346, bar_count: 6180000, calendar_days: 2308,
  data_start: '2015-01-01', data_end: '2026-10-16', duration_seconds: 61.7,
  checks: [
    { key: 'gap', label: '行情缺口', description: '标的在自己首末交易日之间本该有日线却没有的交易日。',
      severity: 'error', affected_instruments: 1836, occurrences: 68023, repairable: true,
      examples: [{ market: 'SZ', code: '000029', name: '深深房A', trade_date: '2017-04-12', detail: '共缺 869 个交易日，分 1 段' }] },
    { key: 'stale', label: '数据陈旧', description: '最后一条日线落后全市场最新交易日两周以上。',
      severity: 'warn', affected_instruments: 7, occurrences: 7, repairable: true, examples: [] },
  ],
}

test('a repair discloses its stages, its cost and the symbols that never got a turn', async ({ page }) => {
  // 先删后下是唯一会先把标的清空的任务：用户必须看得见「现在在删、接下来在传哪只、
  // 名单还剩多少只没轮到」，否则只能靠猜——2026-10-16 就是这样被读成「卡住了」。
  let confirmText = ''
  page.on('dialog', async (dialog) => {
    confirmText = dialog.message()
    await dialog.accept()
  })
  let repairBody: Record<string, unknown> | null = null
  let repairReads = 0
  const base = { description: '行情修复', errors: 0, error: null, worker_count: 8, cancel_requested: false, result: null }
  await page.route('**/api/v1/market-data/jobs/active*', (route) => route.fulfill({ json: { data: [] } }))
  await page.route('**/api/v1/market-data/store', (route) => route.fulfill({ json: { data: {
    database_path: '/data/market/market.duckdb', schema_version: 12, instrument_count: 2,
    bar_count: 6180000, data_start: '2015-01-01', data_end: '2026-10-16', last_online_sync_at: null,
  } } }))
  await page.route('**/api/v1/market-data/local/instruments**', (route) => route.fulfill({ json: instruments }))
  await page.route('**/api/v1/market-data/local/SH/600000/bars*', (route) => route.fulfill({ json: chart('daily') }))
  await page.route('**/api/v1/market-data/health/check', (route) => route.fulfill({ json: { data: { job_id: 'check-1', status: 'queued' } } }))
  await page.route('**/api/v1/market-data/jobs/check-1', (route) => route.fulfill({ json: { data: {
    job_id: 'check-1', description: '行情健康检查', status: 'completed', progress: 1,
    total_candidates: 1, total_scanned: 1, errors: 0, error: null, result: healthReport,
  } } }))
  await page.route('**/api/v1/market-data/health/repair', async (route) => {
    repairBody = route.request().postDataJSON() as Record<string, unknown>
    await route.fulfill({ json: { data: { job_id: 'repair-1', status: 'queued' } } })
  })
  await page.route('**/api/v1/market-data/jobs/repair-1', (route) => {
    const read = repairReads
    repairReads += 1
    if (read < 2) {
      return route.fulfill({ json: { data: { ...base, job_id: 'repair-1', status: 'running', progress: 0,
        total_candidates: 500, total_scanned: 0, phase: 'deleting',
        metadata: { worker_count: 8, delete_targets: 500, delete_range: '第 1 ~ 500 只' } } } })
    }
    if (read < 6) {
      return route.fulfill({ json: { data: { ...base, job_id: 'repair-1', status: 'running', progress: 0.23,
        total_candidates: 500, total_scanned: 115, phase: 'fetching',
        metadata: { worker_count: 8, worker_id: 6, downloaded_targets: 115, current_status: 'fetching',
          range_label: '2015-01-01 → 2026-10-16',
          current_instrument: { market: 'SH', code: '601231', name: '环旭电子' } } } } })
    }
    return route.fulfill({ json: { data: { ...base, job_id: 'repair-1', status: 'completed', progress: 1,
      total_candidates: 500, total_scanned: 500, phase: 'completed', metadata: { worker_count: 8 },
      result: { checked_keys: ['gap', 'stale'], requested_codes: 500, offset: 0, flagged_total: 2272,
        remaining: 1772, truncated: true, deleted_bars: 1133865, written_bars: 1133865, errors: 0,
        empty_after_repair: [] } } } })
  })

  await page.goto('/market-data')
  await page.getByTestId('market-health-check').click()
  await expect(page.getByTestId('market-health-repair')).toContainText('一键修复 2 项')
  await page.getByTestId('market-health-repair').click()

  expect(confirmText).toContain('全市场研究会因为打不开库而失败')
  expect(confirmText).toContain('重下改不出任何东西')
  await expect(page.getByTestId('market-health-stage')).toContainText('正在删除待修标的的全部历史')
  await expect(page.getByTestId('market-health-progress')).toContainText('待删除 500 只标的的全部历史（第 1 ~ 500 只）')
  // 8 路并发时那只「最近上报」的标的不能被称为「当前同步」。
  await expect(page.getByTestId('market-health-current')).toContainText('8 路并发下载中')
  await expect(page.getByTestId('market-health-current')).toContainText('SH 601231 · 环旭电子')
  await expect(page.getByTestId('market-health-current')).toContainText('worker 6 上报')
  // 同一个任务不在上方同步面板再画一条进度条，两处数字才会互相对不上。
  await expect(page.getByTestId('market-data-progress')).toHaveCount(0)
  await expect(page.getByTestId('market-health-remaining')).toContainText('还剩 1772 只没轮到')
  await expect(page.getByTestId('market-health-no-change')).toContainText('原样写回 1133865 根')
  expect(repairBody).toMatchObject({ checks: ['gap', 'stale'], offset: 0, confirm: true })

  await page.getByTestId('market-health-repair-next').click()
  await expect.poll(() => repairBody).toMatchObject({ offset: 500 })
})
