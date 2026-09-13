import { test, expect } from '@playwright/test'

const metadata = {
  indicators: [{
    id: 'indicator_three', display_name: '指标三 · 拉升准备', minimum_bars: 34, recommended_bars: 120,
    signals: [
      { id: 'indicator_three.prepare_rally', display_name: '准备拉升', description: 'VAR1 上穿 8' },
      { id: 'indicator_three.end_zone', display_name: '终', description: 'VAR1 > 90' },
    ],
  }],
  combine_modes: [],
  supported_markets: ['SH', 'SZ'],
  supported_universe: [],
  periods: [{ value: 'daily', label: '日线' }],
  data_directory_help: '本地目录',
}

const result = {
  market: 'SH', code: '600000', bars: 5, start_date: '2024-01-02', end_date: '2024-01-06',
  buy_signal: 'indicator_three.prepare_rally', sell_signal: 'indicator_three.end_zone',
  performance: { total_return: 0.04, annual_return: 0.1, max_drawdown: 0.02, sharpe: 1.2, total_trades: 2, win_rate: 1, start_cash: 10000, end_value: 10400 },
  equity_curve: [
    { date: '2024-01-02', total: 10000, cash: 10000, position_value: 0, drawdown: 0, drawdown_pct: 0 },
    { date: '2024-01-06', total: 10400, cash: 10400, position_value: 0, drawdown: 0, drawdown_pct: 0 },
  ],
  trades: [{ date: '2024-01-03', direction: 'BUY', size: 800, price: 12, commission: 0, slippage: 0, pnl: 0, rejected: false }],
  positions: [], configuration: { initial_cash: 10000, commission: 0.0001, min_commission: 0.1, execution: 'next_open', position_mode: 'full', fixed_size: null }, diagnostic: null,
}

test('user can run a formula backtest and inspect the equity curve', async ({ page }) => {
  await page.route('**/api/v1/formula-screen/metadata', (route) => route.fulfill({ json: { data: metadata } }))
  await page.route('**/api/v1/backtests', (route) => route.fulfill({ status: 202, json: { data: { job_id: 'bt-job', status: 'queued' } } }))
  await page.route('**/api/v1/backtests/bt-job', (route) => route.fulfill({ json: { data: { job_id: 'bt-job', status: 'completed', progress: 1, total_candidates: 1, total_scanned: 1, errors: 0, error: null, result: null } } }))
  await page.route('**/api/v1/backtests/bt-job/results', (route) => route.fulfill({ json: { data: result } }))

  await page.goto('/backtest')
  await expect(page.getByTestId('backtest-page')).toBeVisible()
  await page.getByTestId('backtest-code').fill('600000')
  await page.getByTestId('backtest-date-mode').selectOption('custom')
  await page.getByTestId('backtest-start').fill('2024-01-02')
  await page.getByTestId('backtest-end').fill('2024-01-06')
  await page.getByTestId('start-backtest').click()

  await expect(page.getByTestId('backtest-results')).toContainText('4.00%')
  await expect(page.getByTestId('equity-chart')).toBeVisible()
  await expect(page.getByTestId('backtest-trades')).toContainText('2024-01-03')
})
