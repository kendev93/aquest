import { test, expect } from '@playwright/test'

const metadata = {
  indicators: [{
    id: 'indicator_three', display_name: '指标三 · 拉升准备', minimum_bars: 34, recommended_bars: 120,
    signals: [
      { id: 'indicator_three.prepare_rally', display_name: '准备拉升', description: 'VAR1 上穿 8' },
      { id: 'indicator_three.end_zone', display_name: '终', description: 'VAR1 > 90' },
    ],
    values: [{ id: 'indicator_three.varo7', display_name: 'VARO7', description: '趋势值' }],
  }],
  combine_modes: [{ value: 'all', label: '全部满足' }, { value: 'any', label: '任一满足' }, { value: 'at_least', label: '至少满足' }],
  supported_markets: ['SH', 'SZ'],
  supported_universe: [{ value: 'all', label: '沪深 A 股' }],
  periods: [{ value: 'daily', label: '日线' }],
  data_directory_help: '本地目录',
  default_vipdoc_path: '/data/vipdoc',
}

const phase = (name: 'train' | 'validation' | 'test', start: string, end: string, trades: number, totalReturn: number) => ({
  name, start_date: start, end_date: end, bars: 20, total_trades: trades, win_rate: 0.6,
  total_return: totalReturn, annual_return: totalReturn, max_drawdown: 0.1, sharpe: 1.2,
  profit_factor: 1.4, expectancy: 0.02, avg_holding_days: 4, diagnostic: null,
})

const report = {
  universe: 'all', total_candidates: 1, processed: 1, skipped: 0, errors: 0, bars: 60,
  start_date: '2020-01-01', end_date: '2024-12-31', train_end_date: '2022-12-31', validation_end_date: '2023-12-31',
  ranking_value: 'indicator_three.varo7', train_ratio: 0.6, validation_ratio: 0.2, min_trades: 5, max_test_drawdown: 0.3,
  results: [{
    market: 'SH', code: '600000', bars: 60, data_start: '2020-01-01', data_end: '2024-12-31', suitability_score: 87.5,
    passed: true, label: 'strong', passed_checks: 7, total_checks: 8, positive_periods: 3,
    checks: [{ id: 'test_return', label: '测试期总收益为正', passed: true }],
    train: phase('train', '2020-01-01', '2022-12-31', 30, 0.42),
    validation: phase('validation', '2023-01-01', '2023-12-31', 9, 0.12),
    test: phase('test', '2024-01-01', '2024-12-31', 8, 0.08),
    failure_reason: null,
  }],
  failure_reasons: {}, diagnostic: null,
}

test('user can evaluate strategy fitness across train, validation, and test periods', async ({ page }) => {
  await page.route('**/api/v1/formula-screen/metadata', (route) => route.fulfill({ json: { data: metadata } }))
  await page.route('**/api/v1/strategy-fitness', (route) => route.fulfill({ status: 202, json: { data: { job_id: 'fitness-job', status: 'queued' } } }))
  await page.route('**/api/v1/strategy-fitness/fitness-job', (route) => route.fulfill({ json: { data: { job_id: 'fitness-job', status: 'completed', progress: 1, total_candidates: 1, total_scanned: 1, errors: 0, error: null, result: null } } }))
  await page.route('**/api/v1/strategy-fitness/fitness-job/results', (route) => route.fulfill({ json: { data: report } }))

  await page.goto('/strategy-fitness')
  await expect(page.getByTestId('strategy-fitness-page')).toBeVisible()
  await expect(page.getByTestId('fitness-ranking-value')).toHaveValue('indicator_three.varo7')
  await page.getByTestId('start-fitness').click()

  await expect(page.getByTestId('fitness-results')).toContainText('600000')
  await expect(page.getByTestId('fitness-passed-count')).toContainText('1')
  await expect(page.getByTestId('fitness-train-600000')).toContainText('42.00%')
  await expect(page.getByTestId('fitness-test-600000')).toContainText('8.00%')
})
