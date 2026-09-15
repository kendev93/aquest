import { expect, test } from '@playwright/test'

const metadata = {
  indicators: [{
    id: 'indicator',
    display_name: '测试指标',
    minimum_bars: 1,
    recommended_bars: 20,
    signals: [
      { id: 'indicator.buy', display_name: '买入信号', description: '测试买入' },
      { id: 'indicator.sell', display_name: '卖出信号', description: '测试卖出' },
    ],
    values: [{ id: 'indicator.score', display_name: '强度', description: '测试指标值' }],
  }],
  combine_modes: [{ value: 'all', label: '全部满足' }, { value: 'any', label: '任一满足' }],
  condition_operators: [{ value: 'gt', label: '大于' }, { value: 'lt', label: '小于' }],
  supported_markets: ['SH', 'SZ'],
  supported_universe: [{ value: 'all', label: '沪深 A 股' }],
  periods: [{ value: 'daily', label: '日线' }],
  data_directory_help: '本地行情',
}

const summary = {
  pooled_trades: {
    closed_trades: 40,
    wins: 24,
    losses: 16,
    win_rate: 0.6,
    payoff_ratio: 1.3,
    profit_factor: 1.8,
    mean_trade_return: 0.02,
    median_trade_return: 0.015,
    stocks_with_trades: 8,
  },
  per_stock: {
    stock_count: 8,
    median_win_rate: 0.6,
    median_payoff_ratio: 1.2,
    median_total_return: 0.2,
    median_annual_return: 0.04,
    median_max_drawdown: 0.1,
    median_sharpe: 1.1,
    compared_stock_count: 8,
    improved_stock_count: 5,
    improved_stock_ratio: 0.625,
  },
  stocks: [{
    market: 'SH',
    code: '600000',
    name: '浦发银行',
    closed_trades: 5,
    win_rate: 0.8,
    payoff_ratio: 1.5,
    profit_factor: 2,
    mean_trade_return: 0.04,
    median_trade_return: 0.03,
    total_return: 0.2,
    annual_return: 0.04,
    max_drawdown: 0.08,
    sharpe: 1.3,
    baseline_win_rate: 0.6,
    improved_win_rate: true,
  }],
}

const report = {
  universe: 'all',
  start_date: '2020-01-01',
  end_date: '2024-12-31',
  data_fingerprint: 'research-fingerprint',
  search_sides: 'both',
  feature_count: 12,
  total_candidates: 120,
  processed: 120,
  skipped: 0,
  errors: 0,
  failure_reasons: {},
  splits: {
    train: { start_date: '2020-01-01', end_date: '2022-12-31' },
    validation: { start_date: '2023-01-01', end_date: '2023-12-31' },
    holdout: { start_date: '2024-01-01', end_date: '2024-12-31' },
  },
  baseline: { train: summary, validation: summary, full_period: summary, holdout: summary },
  candidates: [{
    candidate_id: 'buy-1',
    side: 'buy',
    condition: { left_value: 'indicator.score', operator: 'gte', right_value: null, right_constant: 2.5 },
    mining: { support_count: 40, support_ratio: 0.2, instrument_count: 10, outcome_rate: 0.8, baseline_rate: 0.6, lift: 1.33 },
    validation: summary,
    passed_constraints: true,
    constraint_failures: [],
    full_period: summary,
    holdout: summary,
  }],
  algorithm: { mining: 'contrastive_threshold_and_pairwise_rules', search: 'single_condition_then_full_stock_replay' },
}

test('user can submit full-market buy/sell research and inspect per-stock results', async ({ page }) => {
  let submitted: Record<string, unknown> | null = null
  await page.route('**/api/v1/formula-screen/metadata', (route) => route.fulfill({ json: { data: metadata } }))
  await page.route('**/api/v1/jobs/active', (route) => route.fulfill({ json: { data: [] } }))
  await page.route('**/api/v1/condition-research/history', (route) => route.fulfill({ json: { data: [] } }))
  await page.route('**/api/v1/condition-research', async (route) => {
    if (route.request().method() === 'POST') {
      submitted = route.request().postDataJSON() as Record<string, unknown>
      await route.fulfill({ status: 202, json: { data: { job_id: 'research-e2e', status: 'queued' } } })
      return
    }
    await route.fallback()
  })
  await page.route('**/api/v1/condition-research/research-e2e', (route) => route.fulfill({
    json: {
      data: {
        job_id: 'research-e2e',
        status: 'completed',
        progress: 1,
        total_candidates: 120,
        total_scanned: 120,
        errors: 0,
        skipped: 0,
        error: null,
        result: null,
      },
    },
  }))
  await page.route('**/api/v1/condition-research/research-e2e/results', (route) => route.fulfill({ json: { data: report } }))

  await page.goto('/condition-research')
  await expect(page.getByRole('heading', { name: '买卖条件研究' })).toBeVisible()
  await page.getByTestId('condition-board-star').check()
  await page.getByTestId('condition-research-config').locator('button[type="submit"]').click()

  await expect(page.getByTestId('condition-research-candidate-buy-1')).toContainText('强度')
  await expect(page.getByTestId('condition-baseline-win-rate')).toContainText('60.00%')
  await expect(page.getByTestId('condition-research-results')).toContainText('浦发银行')
  expect(submitted).toMatchObject({
    universe: 'all',
    boards: ['star'],
    search_sides: 'both',
    buy_rule: { signals: ['indicator.buy'] },
    sell_rule: { signals: ['indicator.sell'] },
  })
})
