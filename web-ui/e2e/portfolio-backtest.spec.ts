import { test, expect } from '@playwright/test'

const metadata = {
  indicators: [{
    id: 'indicator_three', display_name: '指标三 · 拉升准备', minimum_bars: 34, recommended_bars: 120,
    signals: [
      { id: 'indicator_three.prepare_rally', display_name: '准备拉升', description: 'VAR1 上穿 8' },
      { id: 'indicator_three.end_zone', display_name: '终', description: 'VAR1 > 90' },
    ],
    values: [
      { id: 'indicator_three.varo7', display_name: 'VARO7', description: '趋势值' },
      { id: 'indicator_three.varo6', display_name: 'VARO6', description: '辅助值' },
    ],
  }],
  combine_modes: [
    { value: 'all', label: '全部满足' },
    { value: 'any', label: '任一满足' },
    { value: 'at_least', label: '至少满足' },
  ],
  supported_markets: ['SH', 'SZ'],
  supported_universe: [{ value: 'all', label: '沪深 A 股' }],
  periods: [{ value: 'daily', label: '日线' }],
  data_directory_help: '本地目录',
  default_vipdoc_path: '/data/vipdoc',
}

const result = {
  universe: 'all', total_candidates: 3, processed: 3, skipped: 0, errors: 0, bars: 5,
  start_date: '2024-01-02', end_date: '2024-01-06', max_positions: 5,
  ranking_value: 'indicator_three.varo7', rank_order: 'desc',
  fitness_rank_weight: 0.25, fitness_min_trades: 5, fitness_max_drawdown: 0.3,
  performance: { total_return: 0.12, annual_return: 0.22, max_drawdown: 0.03, sharpe: 1.6, total_trades: 4, win_rate: 0.5, start_cash: 1000000, end_value: 1120000 },
  equity_curve: [
    { date: '2024-01-02', cash: 0, position_value: 1000000, total: 1000000, positions_count: 5, drawdown: 0, drawdown_pct: 0 },
    { date: '2024-01-06', cash: 20000, position_value: 1100000, total: 1120000, positions_count: 5, drawdown: 0, drawdown_pct: 0 },
  ],
  trades: [{ date: '2024-01-04', signal_date: '2024-01-03', market: 'SH', code: '600000', direction: 'SELL', size: 1000, price: 11, commission: 2, stamp_tax: 1, slippage: 0, pnl: 1000, cost_basis: 10000, reason: '止盈', rejected: false }],
  states: [{ date: '2024-01-06', cash: 20000, position_value: 1100000, total: 1120000, positions_count: 5, holdings: [{ market: 'SH', code: '600000', size: 1000, entry_price: 10, close: 11, unrealized_pnl: 1000 }] }],
  ranking_events: [{ date: '2024-01-03', slots_available: 1, ranking_value: 'indicator_three.varo7', candidates: [{ rank: 1, market: 'SZ', code: '000001', score: 9.5, selected: true, fitness_score: 100, fitness_trades: 8, fitness_note: '历史成交 8 笔' }] }],
  failure_reasons: {}, diagnostic: null,
}

test('user can run a ranked portfolio backtest and inspect replacement candidates', async ({ page }) => {
  await page.route(/\/api\/v1\/formula-screen\/metadata$/, (route) => route.fulfill({ json: { data: metadata } }))
  await page.route(/\/api\/v1\/portfolio-backtests$/, async (route) => {
    await route.fulfill({ status: 202, json: { data: { job_id: 'portfolio-job', status: 'queued' } } })
  })
  await page.route(/\/api\/v1\/portfolio-backtests\/portfolio-job$/, async (route) => {
    await route.fulfill({ json: { data: { job_id: 'portfolio-job', status: 'completed', progress: 1, total_candidates: 3, total_scanned: 3, errors: 0, error: null, result: null } } })
  })
  await page.route(/\/api\/v1\/portfolio-backtests\/portfolio-job\/results$/, async (route) => {
    await route.fulfill({ json: { data: result } })
  })

  await page.goto('/portfolio-backtest')
  await expect(page.getByTestId('portfolio-backtest-page')).toBeVisible()
  await expect(page.getByTestId('portfolio-ranking-value')).toHaveValue('indicator_three.varo7')
  await expect(page.getByTestId('start-portfolio-backtest')).toBeEnabled()
  await page.getByTestId('portfolio-fitness-weight').selectOption('0.25')
  await page.getByTestId('start-portfolio-backtest').click()

  await expect(page.getByTestId('portfolio-total-return')).toContainText('12.00%')
  await expect(page.getByTestId('portfolio-results')).toContainText('适配性权重 25%')
  await expect(page.getByTestId('portfolio-holdings')).toContainText('600000')
  await expect(page.getByTestId('portfolio-trades')).toContainText('止盈')
  await expect(page.getByTestId('portfolio-ranking')).toContainText('000001')
  await expect(page.getByTestId('portfolio-equity-chart')).toBeVisible()
})

test('user can restrict a portfolio backtest to explicitly named symbols', async ({ page }) => {
  let payload: Record<string, unknown> | null = null
  await page.route(/\/api\/v1\/formula-screen\/metadata$/, (route) => route.fulfill({ json: { data: metadata } }))
  await page.route(/\/api\/v1\/portfolio-backtests$/, async (route) => {
    payload = route.request().postDataJSON() as Record<string, unknown>
    await route.fulfill({ status: 202, json: { data: { job_id: 'codes-job', status: 'queued' } } })
  })
  await page.route(/\/api\/v1\/portfolio-backtests\/codes-job$/, async (route) => {
    await route.fulfill({ json: { data: { job_id: 'codes-job', status: 'completed', progress: 1, total_candidates: 2, total_scanned: 2, errors: 0, error: null, result: null } } })
  })
  await page.route(/\/api\/v1\/portfolio-backtests\/codes-job\/results$/, async (route) => {
    await route.fulfill({ json: { data: { ...result, universe: 'codes' } } })
  })

  await page.goto('/portfolio-backtest')
  await page.getByTestId('portfolio-universe').selectOption('codes')
  await page.getByTestId('portfolio-codes').fill('600519\nSH:510300')
  await page.getByTestId('portfolio-position-mode').selectOption('fixed')
  await page.getByTestId('portfolio-fixed-size').fill('1000')
  await page.getByTestId('start-portfolio-backtest').click()

  await expect(page.getByTestId('portfolio-results')).toBeVisible()
  expect(payload).toMatchObject({
    universe: 'codes',
    codes: ['600519', 'SH:510300'],
    position_mode: 'fixed',
    fixed_size: 1000,
  })
})
