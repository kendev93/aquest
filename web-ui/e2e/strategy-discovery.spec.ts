import { expect, test } from '@playwright/test'

const metadata = {
  indicators: [{
    id: 'indicator_three',
    display_name: '指标三 · 拉升准备',
    minimum_bars: 34,
    recommended_bars: 120,
    signals: [],
    values: [
      { id: 'indicator_three.var1', display_name: 'VAR1', description: '变量' },
      { id: 'indicator_three.varo7', display_name: 'VARO7', description: '趋势值' },
    ],
  }],
  combine_modes: [
    { value: 'all', label: '全部满足' },
    { value: 'any', label: '任一满足' },
    { value: 'at_least', label: '至少满足' },
  ],
  condition_operators: [{ value: 'gt', label: '大于' }],
  supported_markets: ['SH', 'SZ'],
  supported_universe: [{ value: 'all', label: '沪深全部 A 股' }],
  periods: [{ value: 'daily', label: '日线' }],
  data_directory_help: '本地目录',
}

const candidateConfig = {
  universe: 'all',
  selected_signals: [],
  combine_mode: 'all',
  minimum_matches: null,
  buy_rule: {
    signals: [],
    conditions: [{ left_value: 'indicator_three.var1', operator: 'gt', right_value: null, right_constant: 60 }],
    combine_mode: 'all',
    minimum_matches: null,
  },
  ranking_value: 'indicator_three.varo7',
  rank_order: 'desc',
  max_positions: 2,
  rebalance_frequency: 'weekly',
  sell_signal: null,
  sell_rule: {
    signals: [],
    conditions: [{ left_value: 'indicator_three.var1', operator: 'lt', right_value: null, right_constant: 40 }],
    combine_mode: 'all',
    minimum_matches: null,
  },
  stop_loss_mode: 'pct',
  stop_loss_pct: 0.08,
  stop_loss_atr_multiplier: null,
  stop_loss_cooldown_days: 2,
  take_profit_mode: 'pct',
  take_profit_pct: null,
  take_profit_atr_multiplier: null,
  take_profit_cooldown_days: 0,
  trailing_stop_mode: 'pct',
  trailing_stop_pct: null,
  trailing_stop_atr_multiplier: null,
  trailing_stop_cooldown_days: 0,
  atr_period: 14,
  start_date: '2024-01-01',
  end_date: '2024-12-31',
  initial_cash: 1000000,
  commission: 0.0001,
  min_commission: 0.1,
  stamp_tax: 0.001,
  slippage: 0,
  execution: 'next_open',
}

test('user can discover a strategy, apply it, and rerun ordinary portfolio backtest', async ({ page }) => {
  await page.route('**/api/v1/strategy-discovery/capabilities', (route) => route.fulfill({ json: {
    data: {
      enabled: true,
      indicator_count: 20,
      signal_count: 15,
      optuna_available: true,
      lightgbm_available: true,
      indicators: [
        { id: 'technical.ma5', display_name: 'MA5', rankable: true },
        { id: 'technical.ma20', display_name: 'MA20', rankable: true },
      ],
    },
  } }))
  await page.route('**/api/v1/strategy-discovery/history', (route) => route.fulfill({ json: { data: [] } }))
  let discoveryPayload: Record<string, unknown> | undefined
  await page.route('**/api/v1/strategy-discovery', (route) => {
    discoveryPayload = route.request().postDataJSON() as Record<string, unknown>
    return route.fulfill({ status: 202, json: { data: { job_id: 'discovery-job', status: 'queued' } } })
  })
  await page.route('**/api/v1/strategy-discovery/discovery-job', (route) => route.fulfill({ json: {
    data: { job_id: 'discovery-job', status: 'completed', progress: 1, total_candidates: 1, total_scanned: 1, errors: 0, skipped: 0, error: null, result: null, phase: 'completed', metadata: {} },
  } }))
  await page.route('**/api/v1/strategy-discovery/discovery-job/results', (route) => route.fulfill({ json: { data: {
    request: {},
    results_by_sharpe: [{ candidate_hash: 'candidate-1', complexity: 5, human_readable_rules: '买入[VAR1 > 60]；卖出[VAR1 < 40]；排序[VARO7 高→低]；持仓2个', config: candidateConfig, metrics: { validation: { annual_return: 0.2, sharpe: 1.5, max_drawdown: 0.1 }, holdout: { annual_return: 0.1, sharpe: 0.8, max_drawdown: 0.12 } } }],
    results_by_return: [],
    search_metadata: { evaluated_trials: 1 },
    validation_metadata: { leaderboard_period: 'validation' },
  } } }))
  await page.route('**/api/v1/formula-screen/metadata', (route) => route.fulfill({ json: { data: metadata } }))
  await page.route('**/api/v1/portfolio-backtests', (route) => route.fulfill({ status: 202, json: { data: { job_id: 'portfolio-job', status: 'queued' } } }))
  await page.route('**/api/v1/portfolio-backtests/portfolio-job', (route) => route.fulfill({ json: { data: { job_id: 'portfolio-job', status: 'completed', progress: 1, total_candidates: 1, total_scanned: 1, errors: 0, error: null, result: null } } }))
  await page.route('**/api/v1/portfolio-backtests/portfolio-job/results', (route) => route.fulfill({ json: { data: {
    universe: 'all', total_candidates: 1, processed: 1, skipped: 0, errors: 0, bars: 20, start_date: '2024-01-01', end_date: '2024-12-31', max_positions: 2, ranking_value: 'indicator_three.varo7', rank_order: 'desc', fitness_filter_enabled: false, fitness_min_score: 75, fitness_min_trades: 5, fitness_max_drawdown: 0.3, performance: { total_return: 0.1, annual_return: 0.1, max_drawdown: 0.05, sharpe: 1.2, total_trades: 6, win_rate: 0.5, end_value: 1100000 }, equity_curve: [], trades: [], states: [], ranking_events: [], failure_reasons: {}, diagnostic: null,
  } } }))

  await page.goto('/strategy-lab')
  await page.getByTestId('strategy-required-buy-rule-add-condition').click()
  await page.getByTestId('strategy-discovery-positions').fill('2')
  await page.getByTestId('strategy-discovery-submit').click()

  await expect.poll(() => discoveryPayload).toMatchObject({
    required_buy_conditions: [{
      left_value: 'technical.ma5',
      operator: 'gt',
      right_value: 'technical.ma20',
      right_constant: null,
    }],
  })
  await expect(page.getByTestId('strategy-discovery-results')).toContainText('夏普榜前 10')
  await expect(page.getByTestId('strategy-discovery-results')).toContainText('VAR1 > 60')
  await page.getByTestId('strategy-discovery-apply-candidate-1').click()

  await expect(page).toHaveURL(/portfolio-backtest$/)
  await expect(page.getByTestId('portfolio-max-positions')).toHaveValue('2')
  await expect(page.getByTestId('portfolio-ranking-value')).toHaveValue('indicator_three.varo7')
  await page.getByTestId('start-portfolio-backtest').click()
  await expect(page.getByTestId('portfolio-total-return')).toContainText('10.00%')
})
