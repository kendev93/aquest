import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type {
  FormulaScreenMetadata,
  PortfolioBacktestJobState,
  PortfolioBacktestResult,
  SavedStrategyDetail,
  SavedStrategySummary,
  StrategyDiscoveryCandidate,
  StrategyDiscoveryJobState,
  StrategyDiscoveryResult,
} from '../types'
import PortfolioBacktestPage from './PortfolioBacktestPage.vue'

import * as portfolioApi from '../api/portfolioBacktest'
import * as formulaApi from '../api/formulaScreen'
import * as jobsApi from '../api/jobs'
import * as strategiesApi from '../api/savedStrategies'
import * as strategyDiscoveryApi from '../api/strategyDiscovery'
import { FormulaScreenApiError } from '../api/formulaScreen'

vi.mock('../api/formulaScreen', async () => {
  const actual = await vi.importActual<typeof import('../api/formulaScreen')>('../api/formulaScreen')
  return { ...actual, fetchMetadata: vi.fn(), parseFormula: vi.fn() }
})

vi.mock('../api/portfolioBacktest', async () => {
  const actual = await vi.importActual<typeof import('../api/portfolioBacktest')>('../api/portfolioBacktest')
  return {
    ...actual,
    createPortfolioBacktest: vi.fn(),
    getPortfolioBacktest: vi.fn(),
    getPortfolioBacktestResults: vi.fn(),
    fetchPortfolioBacktestHistory: vi.fn(),
    getPortfolioBacktestHistory: vi.fn(),
    deletePortfolioBacktestHistory: vi.fn(),
  }
})

vi.mock('../api/jobs', async () => {
  const actual = await vi.importActual<typeof import('../api/jobs')>('../api/jobs')
  return { ...actual, fetchActiveJobs: vi.fn() }
})

vi.mock('../api/savedStrategies', async () => {
  const actual = await vi.importActual<typeof import('../api/savedStrategies')>('../api/savedStrategies')
  return {
    ...actual,
    fetchStrategies: vi.fn(),
    getStrategy: vi.fn(),
    createStrategy: vi.fn(),
    updateStrategy: vi.fn(),
    deleteStrategy: vi.fn(),
  }
})

vi.mock('../api/strategyDiscovery', async () => {
  const actual = await vi.importActual<typeof import('../api/strategyDiscovery')>('../api/strategyDiscovery')
  return {
    ...actual,
    cancelStrategyDiscovery: vi.fn(),
    createPortfolioRiskOptimization: vi.fn(),
    getStrategyDiscovery: vi.fn(),
    getStrategyDiscoveryResults: vi.fn(),
  }
})

const metadata: FormulaScreenMetadata = {
  indicators: [{
    id: 'indicator_three', display_name: '指标三 · 拉升准备', minimum_bars: 34, recommended_bars: 120,
    signals: [
      { id: 'indicator_three.prepare_rally', display_name: '准备拉升', description: 'VAR1 上穿 8' },
      { id: 'indicator_three.end_zone', display_name: '终', description: 'VAR1 > 90' },
    ],
    values: [
      { id: 'indicator_three.varo7', display_name: 'VARO7', description: '趋势值' },
      { id: 'indicator_three.varo6', display_name: 'VARO6', description: '辅助值' },
      { id: 'indicator_three.var1', display_name: 'VAR1', description: '变量' },
    ],
  }],
  combine_modes: [
    { value: 'all', label: '全部满足' },
    { value: 'any', label: '任一满足' },
    { value: 'at_least', label: '至少满足' },
  ],
  supported_markets: ['SH', 'SZ'],
  supported_universe: [
    { value: 'all', label: '沪深 A 股' },
    { value: 'sh', label: '仅上海' },
    { value: 'sz', label: '仅深圳' },
    { value: 'custom', label: '自定义列表' },
  ],
  periods: [{ value: 'daily', label: '日线' }],
  data_directory_help: '本地目录',
  default_vipdoc_path: '/data/vipdoc',
}

const result: PortfolioBacktestResult = {
  universe: 'all', total_candidates: 3, processed: 3, skipped: 0, errors: 0, bars: 5,
  start_date: '2024-01-02', end_date: '2024-01-06', max_positions: 2,
  ranking_value: 'indicator_three.varo7', rank_order: 'desc',
  performance: { total_return: 0.12, annual_return: 0.22, max_drawdown: 0.03, sharpe: 1.6, total_trades: 4, win_rate: 0.5, end_value: 11200, start_cash: 10000 },
  equity_curve: [
    { date: '2024-01-02', cash: 0, position_value: 10000, total: 10000, positions_count: 2, drawdown: 0, drawdown_pct: 0 },
    { date: '2024-01-06', cash: 200, position_value: 11000, total: 11200, positions_count: 2, drawdown: 0, drawdown_pct: 0 },
  ],
  trades: [
    { date: '2024-01-03', signal_date: '2024-01-02', market: 'SH', code: '600000', name: '浦发银行', direction: 'BUY', size: 400, price: 10, commission: 0, stamp_tax: 0, slippage: 0, pnl: 0, cost_basis: 0, reason: '排名入选', rejected: false },
    { date: '2024-01-04', signal_date: '2024-01-03', market: 'SH', code: '600000', name: '浦发银行', direction: 'SELL', size: 400, price: 11, commission: 0, stamp_tax: 0, slippage: 0, pnl: 400, cost_basis: 4000, reason: '止盈', rejected: false },
  ],
  states: [{
    date: '2024-01-06', cash: 200, position_value: 11000, total: 11200, positions_count: 2,
    holdings: [{ market: 'SH', code: '600000', name: '浦发银行', size: 400, entry_price: 10, close: 11, unrealized_pnl: 400 }],
  }],
  ranking_events: [{
    date: '2024-01-03', slots_available: 1, ranking_value: 'indicator_three.varo7',
    candidates: [{ rank: 1, market: 'SZ', code: '000001', name: '平安银行', score: 9.5, selected: true }],
  }],
  failure_reasons: {},
  diagnostic: null,
  data_fingerprint: 'portfolio-fingerprint',
  benchmark: {
    label: '标的池等权买入持有',
    instrument_count: 3,
    dates: [20240102, 20240106],
    totals: [10000, 10800],
    performance: {
      total_return: 0.08, annual_return: 0.08, max_drawdown: 0.05, max_dd_duration: 9,
      sharpe: 0.9, sortino: 1.1, calmar: 1.6, volatility: 0.18, start_cash: 10000, end_value: 10800,
    },
  },
}

const riskCandidate: StrategyDiscoveryCandidate = {
  candidate_hash: 'risk-candidate-1',
  complexity: 3,
  human_readable_rules: '止损8%；止盈20%；移动止盈5%',
  config: {
    universe: 'all',
    start_date: '2024-01-01',
    end_date: '2024-03-31',
    selected_signals: [],
    combine_mode: 'all',
    minimum_matches: null,
    buy_rule: { signals: [], conditions: [{ left_value: 'indicator_three.var1', operator: 'gt', right_constant: 10 }], combine_mode: 'all', minimum_matches: null },
    sell_rule: { signals: [], conditions: [{ left_value: 'indicator_three.var1', operator: 'lt', right_constant: 50 }], combine_mode: 'all', minimum_matches: null },
    ranking_value: 'indicator_three.varo7',
    rank_order: 'desc',
    max_positions: 5,
    rebalance_frequency: 'daily',
    stop_loss_mode: 'pct',
    stop_loss_pct: 0.08,
    stop_loss_atr_multiplier: null,
    stop_loss_cooldown_days: 0,
    take_profit_mode: 'pct',
    take_profit_pct: 0.2,
    take_profit_atr_multiplier: null,
    take_profit_cooldown_days: 0,
    trailing_stop_mode: 'pct',
    trailing_stop_pct: 0.05,
    trailing_stop_atr_multiplier: null,
    trailing_stop_cooldown_days: 0,
    atr_period: 14,
    initial_cash: 1_000_000,
    commission: 0.0001,
    min_commission: 0.1,
    stamp_tax: 0.001,
    slippage: 0,
    execution: 'next_open',
    position_mode: 'full',
    fixed_size: null,
  },
  metrics: {
    train: { annual_return: 0.15, sharpe: 1.1, max_drawdown: 0.12 },
    validation: {
      total_return: 0.12, annual_return: 0.22, sharpe: 1.6, max_drawdown: 0.03,
      total_trades: 4, win_rate: 0.5, turnover: 1.2,
    },
    holdout: { annual_return: 0.1, sharpe: 0.9, max_drawdown: 0.05, constraints_passed: true },
  },
}

const riskOptimizationResult: StrategyDiscoveryResult = {
  mode: 'risk_optimization',
  request: {
    splits: {
      search: { start_date: '2024-01-01', end_date: '2024-02-29' },
      validation: { start_date: '2024-03-01', end_date: '2024-03-31' },
      holdout: { start_date: '2024-04-01', end_date: '2024-04-30' },
    },
  },
  results_by_sharpe: [riskCandidate],
  results_by_return: [riskCandidate],
  search_metadata: {},
  validation_metadata: {},
  data_fingerprint: 'portfolio-fingerprint',
  configuration_fingerprint: 'risk-base-fingerprint',
}

describe('PortfolioBacktestPage', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
    vi.clearAllMocks()
    sessionStorage.clear()
    vi.mocked(formulaApi.fetchMetadata).mockResolvedValue(metadata)
    vi.mocked(portfolioApi.createPortfolioBacktest).mockResolvedValue({ job_id: 'portfolio-1', status: 'queued' })
    vi.mocked(portfolioApi.getPortfolioBacktest).mockResolvedValue({
      job_id: 'portfolio-1', status: 'completed', progress: 1, total_candidates: 3,
      total_scanned: 3, errors: 0, error: null, result: null,
    })
    vi.mocked(portfolioApi.getPortfolioBacktestResults).mockResolvedValue(result)
    vi.mocked(portfolioApi.fetchPortfolioBacktestHistory).mockResolvedValue([])
    vi.mocked(jobsApi.fetchActiveJobs).mockResolvedValue([])
    vi.mocked(strategiesApi.fetchStrategies).mockResolvedValue([])
    vi.mocked(strategiesApi.deleteStrategy).mockResolvedValue({ deleted: true })
    vi.mocked(strategyDiscoveryApi.createPortfolioRiskOptimization).mockResolvedValue({ job_id: 'risk-opt-1', status: 'queued' })
    vi.mocked(strategyDiscoveryApi.getStrategyDiscovery).mockResolvedValue({
      job_id: 'risk-opt-1', status: 'completed', progress: 1, total_candidates: 1000,
      total_scanned: 1000, errors: 0, skipped: 0, error: null, result: null,
    } as StrategyDiscoveryJobState)
    vi.mocked(strategyDiscoveryApi.getStrategyDiscoveryResults).mockResolvedValue(riskOptimizationResult)
    vi.mocked(strategyDiscoveryApi.cancelStrategyDiscovery).mockResolvedValue({
      job_id: 'risk-opt-1', status: 'cancelling', cancel_requested: true,
    })
  })

  const savedStrategy: SavedStrategySummary = {
    strategy_id: 'strategy-1',
    name: '主力进场轮动',
    note: '',
    source_history_id: null,
    created_at: '2026-09-21 01:00:00',
    updated_at: '2026-09-21 01:00:00',
    summary: {
      start_date: '2024-01-02',
      end_date: '2024-01-06',
      max_positions: 2,
      performance: { annual_return: 0.22, max_drawdown: 0.03 },
    },
  }
  const savedStrategyDetail: SavedStrategyDetail = {
    ...savedStrategy,
    request: {
      universe: 'all',
      date_mode: 'custom',
      start_date: '2024-01-02',
      end_date: '2024-01-06',
      selected_signals: [],
      combine_mode: 'all',
      minimum_matches: null,
      buy_rule: { signals: ['indicator_three.prepare_rally'], conditions: [], combine_mode: 'any', minimum_matches: null },
      sell_rule: { signals: [], conditions: [], combine_mode: 'any', minimum_matches: null },
      ranking_value: 'indicator_three.varo7',
      rank_order: 'desc',
      max_positions: 2,
      rebalance_frequency: 'daily',
    },
  }

  it('shows the equal-weight benchmark and how much the strategy beat it by', async () => {
    const wrapper = mount(PortfolioBacktestPage)
    await flushPromises()
    await wrapper.get('[data-testid="portfolio-config"]').trigger('submit')
    await flushPromises()

    const benchmark = wrapper.get('[data-testid="portfolio-benchmark"]')
    expect(benchmark.text()).toContain('标的池等权买入持有')
    expect(benchmark.text()).toContain('3 只标的')
    expect(benchmark.get('[data-testid="portfolio-benchmark-total"]').text()).toBe('8.00%')
    expect(benchmark.get('[data-testid="portfolio-benchmark-annual"]').text()).toBe('8.00%')
    expect(benchmark.get('[data-testid="portfolio-benchmark-drawdown"]').text()).toBe('5.00%')
    // 策略年化 22% − 基准年化 8% = 14%
    expect(benchmark.get('[data-testid="portfolio-excess-annual"]').text()).toBe('14.00%')
    // 基准要和策略画在同一张图上，否则"跑赢了"只是个说法。
    expect(wrapper.get('[data-testid="portfolio-benchmark-chart"]').attributes('points')).toBeTruthy()
  })

  it('opens the pnl breakdown panel on demand and folds it again', async () => {
    const wrapper = mount(PortfolioBacktestPage)
    await flushPromises()
    await wrapper.get('[data-testid="portfolio-config"]').trigger('submit')
    await flushPromises()

    // 分析是按需展开的：默认不占结果页的空间。
    expect(wrapper.find('[data-testid="portfolio-pnl-analysis"]').exists()).toBe(false)
    await wrapper.get('[data-testid="portfolio-pnl-analysis-toggle"]').trigger('click')
    const panel = wrapper.get('[data-testid="portfolio-pnl-analysis"]')
    expect(panel.get('[data-testid="portfolio-pnl-analysis-trips"]').text()).toBe('1')
    expect(panel.get('[data-testid="portfolio-pnl-analysis-by-holding"]').text()).toContain('≤10 日')
    expect(panel.get('[data-testid="portfolio-pnl-analysis-unrealized"]').text()).toContain('浦发银行')
    // 这次回测没设止损，面板要把它点出来。
    expect(panel.get('[data-testid="portfolio-pnl-analysis-no-stop-loss"]').text()).toContain('止损')

    await wrapper.get('[data-testid="portfolio-pnl-analysis-toggle"]').trigger('click')
    expect(wrapper.find('[data-testid="portfolio-pnl-analysis"]').exists()).toBe(false)
  })

  it('hides the benchmark block when the backtest cannot produce one', async () => {
    vi.mocked(portfolioApi.getPortfolioBacktestResults).mockResolvedValue({ ...result, benchmark: null })
    const wrapper = mount(PortfolioBacktestPage)
    await flushPromises()
    await wrapper.get('[data-testid="portfolio-config"]').trigger('submit')
    await flushPromises()

    expect(wrapper.find('[data-testid="portfolio-benchmark"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="portfolio-benchmark-chart"]').exists()).toBe(false)
    // 没有基准不代表没有结果。
    expect(wrapper.get('[data-testid="portfolio-results"]').text()).toContain('12.00%')
  })

  it('recovers and resumes a running portfolio backtest after the page is refreshed', async () => {
    vi.mocked(jobsApi.fetchActiveJobs).mockResolvedValueOnce([{
      job_id: 'portfolio-refresh', description: '组合回测', status: 'running', progress: 0.42,
      total_candidates: 3, total_scanned: 1, errors: 0, error: null, result: null,
    }])
    let resolveStatus!: (state: PortfolioBacktestJobState) => void
    vi.mocked(portfolioApi.getPortfolioBacktest).mockImplementationOnce(() => new Promise((resolve) => {
      resolveStatus = resolve
    }))

    const wrapper = mount(PortfolioBacktestPage)
    await flushPromises()

    expect(jobsApi.fetchActiveJobs).toHaveBeenCalled()
    expect(portfolioApi.getPortfolioBacktest).toHaveBeenCalledWith('portfolio-refresh')
    expect(wrapper.get('[data-testid="portfolio-progress"]').text()).toContain('42%')

    resolveStatus({
      job_id: 'portfolio-refresh', status: 'completed', progress: 1, total_candidates: 3, total_scanned: 3,
      errors: 0, error: null, result: null,
    })
    await flushPromises()
    expect(wrapper.get('[data-testid="portfolio-results"]').text()).toContain('12.00%')
  })

  it('submits ranked slot configuration and renders holdings, trades, and candidates', async () => {
    const wrapper = mount(PortfolioBacktestPage)
    await flushPromises()
    expect((wrapper.get('[data-testid="portfolio-date-mode"]').element as HTMLSelectElement).value).toBe('recent5')
    expect(wrapper.text()).toContain('默认使用本地最新已完成日线向前 5 年')

    await wrapper.get('[data-testid="portfolio-config"]').trigger('submit')
    await flushPromises()

    expect(portfolioApi.createPortfolioBacktest).toHaveBeenCalledWith(expect.objectContaining({
      selected_signals: [],
      buy_rule: expect.objectContaining({
        signals: [],
        conditions: [expect.objectContaining({ left_value: 'indicator_three.var1' })],
      }),
      ranking_value: 'indicator_three.varo7', max_positions: 5, stop_loss_pct: 0.08,
      date_mode: 'recent5', start_date: null, end_date: null,
    }))
    expect(wrapper.get('[data-testid="portfolio-results"]').text()).toContain('12.00%')
    expect(wrapper.get('[data-testid="portfolio-holdings"]').text()).toContain('600000')
    expect(wrapper.get('[data-testid="portfolio-trades"]').text()).toContain('止盈')
    expect(wrapper.get('[data-testid="portfolio-trades"]').text()).toContain('浦发银行')
    expect(wrapper.get('[data-testid="portfolio-holdings"]').text()).toContain('浦发银行')
    expect(wrapper.get('[data-testid="portfolio-ranking"]').text()).toContain('000001')
    expect(wrapper.get('[data-testid="portfolio-ranking"]').text()).toContain('平安银行')
    expect(wrapper.get('[data-testid="portfolio-equity-chart"]')).toBeTruthy()
  })

  it('parses custom formula outputs and invalidates selections after editing', async () => {
    vi.mocked(formulaApi.parseFormula).mockResolvedValueOnce({
      parameters: [{ name: 'N', default: 5, minimum: 1, maximum: 100, step: 1 }],
      signals: [{ id: 'custom.buy', display_name: '买入', description: '买入条件' }, { id: 'custom.sell', display_name: '卖出', description: '卖出条件' }],
      values: [{ id: 'custom.rank', display_name: '排序值', description: '排序' }, { id: 'custom.alt', display_name: '比较值', description: '比较' }],
      minimum_bars: 6,
      warnings: [],
    })
    const wrapper = mount(PortfolioBacktestPage)
    await flushPromises()
    await wrapper.get('[data-testid="portfolio-mode-custom"]').trigger('click')
    await wrapper.get('[data-testid="portfolio-formula"]').setValue('N:=5; 买入:C>10; 卖出:C<10; 排序:C;')
    await wrapper.get('[data-testid="portfolio-parse-formula"]').trigger('click')
    await flushPromises()

    expect((wrapper.get('[data-testid="portfolio-ranking-value"]').element as HTMLSelectElement).value).toBe('custom.rank')
    expect(wrapper.get('[data-testid="portfolio-param-N"]')).toBeTruthy()
    await wrapper.get('[data-testid="portfolio-formula"]').setValue('N:=8; 买入:C>10; 卖出:C<10; 排序:C;')
    await flushPromises()
    expect(wrapper.text()).toContain('公式已修改，请重新解析')

    await wrapper.get('[data-testid="portfolio-mode-preset"]').trigger('click')
    expect((wrapper.get('[data-testid="portfolio-ranking-value"]').element as HTMLSelectElement).value).toBe('indicator_three.varo7')
  })

  it('configures optional sell rules and reports a failed job', async () => {
    vi.mocked(portfolioApi.getPortfolioBacktest).mockResolvedValueOnce({
      job_id: 'portfolio-1', status: 'failed', progress: 1, total_candidates: 3,
      total_scanned: 3, errors: 1, error: '组合任务失败', result: null,
    })
    const wrapper = mount(PortfolioBacktestPage)
    await flushPromises()

    await wrapper.get('[data-testid="portfolio-buy-rule-add-condition"]').trigger('click')
    await wrapper.get('[data-testid="portfolio-buy-rule-combine-mode"]').setValue('at_least')
    await wrapper.get('[data-testid="portfolio-buy-rule-minimum-matches"]').setValue(2)
    await wrapper.get('[data-testid="portfolio-universe"]').setValue('custom')
    await wrapper.get('[data-testid="portfolio-universe-file"]').setValue('/data/list.txt')
    await wrapper.get('[data-testid="portfolio-rank-order"]').setValue('asc')
    await wrapper.get('[data-testid="portfolio-frequency"]').setValue('weekly')
    await wrapper.get('[data-testid="portfolio-fitness-weight"]').setValue(0.25)
    await wrapper.get('[data-testid="portfolio-fitness-trades"]').setValue(3)
    await wrapper.get('[data-testid="portfolio-fitness-drawdown"]').setValue(40)
    await wrapper.get('#portfolio-sell-rule-operator-0').setValue('gte')
    await wrapper.get('[data-testid="portfolio-stop-loss-enabled"]').setValue(false)
    await wrapper.get('[data-testid="portfolio-take-profit-enabled"]').setValue(true)
    await wrapper.get('[data-testid="portfolio-advanced-toggle"]').trigger('click')
    await wrapper.get('[data-testid="portfolio-date-mode"]').setValue('custom')
    await wrapper.get('[data-testid="portfolio-start"]').setValue('2024-01-02')
    await wrapper.get('[data-testid="portfolio-end"]').setValue('2024-01-06')
    await wrapper.get('[data-testid="portfolio-initial-cash"]').setValue(50000)
    await wrapper.get('[data-testid="portfolio-execution"]').setValue('next_close')
    await wrapper.get('[data-testid="portfolio-commission"]').setValue(0.001)
    await wrapper.get('[data-testid="portfolio-min-commission"]').setValue(3)
    await wrapper.get('[data-testid="portfolio-stamp-tax"]').setValue(0.002)
    await wrapper.get('[data-testid="portfolio-slippage"]').setValue(0.01)
    await wrapper.get('[data-testid="portfolio-config"]').trigger('submit')
    await flushPromises()

    expect(portfolioApi.createPortfolioBacktest).toHaveBeenCalledWith(expect.objectContaining({
      universe: 'custom', universe_file: '/data/list.txt', selected_signals: [],
      date_mode: 'custom',
      buy_rule: expect.objectContaining({
        signals: [],
        combine_mode: 'at_least', minimum_matches: 2,
        conditions: expect.any(Array),
      }),
      combine_mode: 'all', minimum_matches: null, rank_order: 'asc',
      rebalance_frequency: 'weekly', sell_signal: null, stop_loss_pct: null,
      take_profit_pct: 0.2, sell_value: null, sell_value_operator: null,
      sell_value_threshold: null, compare_left_value: null, compare_operator: null,
      compare_right_value: null, execution: 'next_close',
      sell_rule: expect.objectContaining({
        combine_mode: 'all',
        conditions: [expect.objectContaining({
          left_value: 'indicator_three.var1', operator: 'gte', right_constant: 90, right_value: null,
        })],
      }),
      fitness_rank_weight: 0.25, fitness_min_trades: 3,
      fitness_max_drawdown: 0.4,
    }))
    expect(wrapper.get('[data-testid="portfolio-message"]').text()).toContain('组合任务失败')
  })

  it('renders an empty diagnostic report and exports it', async () => {
    const emptyResult: PortfolioBacktestResult = {
      ...result,
      universe: 'custom',
      rank_order: 'asc',
      performance: { total_return: null, max_drawdown: null, sharpe: null, total_trades: null, win_rate: null, end_value: null },
      equity_curve: [{ date: '2024-01-02', cash: 10000, position_value: 0, total: 10000, positions_count: 0, drawdown: 0, drawdown_pct: 0 }],
      trades: [],
      states: [{ date: '2024-01-02', cash: 10000, position_value: 0, total: 10000, positions_count: 0, holdings: [] }],
      ranking_events: [],
      failure_reasons: { '数据不足': 2 },
      diagnostic: '组合没有成交记录',
    }
    vi.mocked(portfolioApi.getPortfolioBacktestResults).mockResolvedValueOnce(emptyResult)
    const wrapper = mount(PortfolioBacktestPage)
    await flushPromises()
    await wrapper.get('[data-testid="portfolio-config"]').trigger('submit')
    await flushPromises()

    expect(wrapper.get('[data-testid="portfolio-diagnostic"]').text()).toContain('组合没有成交记录')
    expect(wrapper.text()).toContain('数据不足')
    expect(wrapper.text()).toContain('回测结束时没有持仓')
    expect(wrapper.text()).toContain('没有可展示的排名候选')
    Object.defineProperty(URL, 'createObjectURL', { value: vi.fn().mockReturnValue('blob:portfolio'), configurable: true })
    Object.defineProperty(URL, 'revokeObjectURL', { value: vi.fn(), configurable: true })
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined)
    await wrapper.get('[data-testid="export-portfolio-backtest"]').trigger('click')
    expect(clickSpy).toHaveBeenCalledTimes(1)
    clickSpy.mockRestore()
  })

  it('shows a request error when the job cannot be created', async () => {
    vi.mocked(portfolioApi.createPortfolioBacktest).mockRejectedValueOnce(new FormulaScreenApiError('服务器拒绝任务', 422))
    const wrapper = mount(PortfolioBacktestPage)
    await flushPromises()
    await wrapper.get('[data-testid="portfolio-config"]').trigger('submit')
    await flushPromises()

    expect(wrapper.get('[data-testid="portfolio-message"]').text()).toContain('服务器拒绝任务')
  })

  it('keeps the successful backtest message if refreshing history fails', async () => {
    vi.mocked(portfolioApi.fetchPortfolioBacktestHistory)
      .mockResolvedValueOnce([])
      .mockRejectedValueOnce(new Error('历史服务暂不可用'))
    const wrapper = mount(PortfolioBacktestPage)
    await flushPromises()
    await wrapper.get('[data-testid="portfolio-config"]').trigger('submit')
    await flushPromises()

    expect(wrapper.get('[data-testid="portfolio-message"]').text()).toContain('组合回测完成')
    expect(wrapper.get('[data-testid="portfolio-history-error"]').text()).toContain('历史服务暂不可用')
  })

  it('shows validation when no buy signal is selected', async () => {
    const wrapper = mount(PortfolioBacktestPage)
    await flushPromises()
    await wrapper.get('[data-testid="portfolio-buy-rule-remove-0"]').trigger('click')
    await wrapper.get('[data-testid="portfolio-config"]').trigger('submit')
    expect(wrapper.text()).toContain('请至少设置一个买入条件')
  })

  it('submits an indicator-to-fixed-value rule', async () => {
    const wrapper = mount(PortfolioBacktestPage)
    await flushPromises()
    await wrapper.get('#portfolio-buy-rule-right-type-0').setValue('constant')
    await wrapper.get('#portfolio-buy-rule-right-0').setValue(60)
    await wrapper.get('[data-testid="portfolio-config"]').trigger('submit')
    await flushPromises()

    expect(portfolioApi.createPortfolioBacktest).toHaveBeenCalledWith(expect.objectContaining({
      buy_rule: expect.objectContaining({
        conditions: [expect.objectContaining({ right_constant: 60, right_value: null })],
      }),
    }))
  })

  it('submits an explicit code list when the 指定标的 universe is selected', async () => {
    const wrapper = mount(PortfolioBacktestPage)
    await flushPromises()
    await wrapper.get('[data-testid="portfolio-universe"]').setValue('codes')
    await wrapper.get('[data-testid="portfolio-codes"]').setValue('600519\nSH:510300，000001')
    await wrapper.get('[data-testid="portfolio-config"]').trigger('submit')
    await flushPromises()

    expect(portfolioApi.createPortfolioBacktest).toHaveBeenCalledWith(expect.objectContaining({
      universe: 'codes',
      codes: ['600519', 'SH:510300', '000001'],
    }))
  })

  it('requires at least one code in the 指定标的 universe', async () => {
    const wrapper = mount(PortfolioBacktestPage)
    await flushPromises()
    await wrapper.get('[data-testid="portfolio-universe"]').setValue('codes')
    await wrapper.get('[data-testid="portfolio-config"]').trigger('submit')
    await flushPromises()

    expect(wrapper.text()).toContain('请填写至少一个证券代码')
    expect(portfolioApi.createPortfolioBacktest).not.toHaveBeenCalled()
  })

  it('toggles instrument scope filters and removes a history record', async () => {
    vi.mocked(portfolioApi.fetchPortfolioBacktestHistory).mockResolvedValue([{
      history_id: 'history-1', backtest_type: 'portfolio', created_at: '2024-01-06T08:00:00Z',
      started_at: '2024-01-06T08:00:00Z', finished_at: '2024-01-06T08:01:00Z',
      duration_seconds: 60, summary: {},
    }])
    vi.mocked(portfolioApi.deletePortfolioBacktestHistory).mockResolvedValue({ deleted: true })
    const wrapper = mount(PortfolioBacktestPage)
    await flushPromises()

    await wrapper.get('[data-testid="portfolio-scope-type-stock"]').setValue(true)
    await wrapper.get('[data-testid="portfolio-config"]').trigger('submit')
    await flushPromises()

    expect(portfolioApi.createPortfolioBacktest).toHaveBeenCalledWith(expect.objectContaining({
      instrument_types: ['stock'],
    }))

    await wrapper.get('[data-testid="portfolio-history-remove-history-1"]').trigger('click')
    await flushPromises()

    expect(portfolioApi.deletePortfolioBacktestHistory).toHaveBeenCalledWith('history-1')
    expect(wrapper.text()).toContain('历史组合回测已删除')
  })

  it('excludes ST symbols by default and honours the toggle', async () => {
    const wrapper = mount(PortfolioBacktestPage)
    await flushPromises()
    await wrapper.get('[data-testid="portfolio-config"]').trigger('submit')
    await flushPromises()

    expect(portfolioApi.createPortfolioBacktest).toHaveBeenCalledWith(expect.objectContaining({
      exclude_st: true,
    }))

    await wrapper.get('[data-testid="portfolio-exclude-st"]').setValue(false)
    await wrapper.get('[data-testid="portfolio-config"]').trigger('submit')
    await flushPromises()

    const lastCall = vi.mocked(portfolioApi.createPortfolioBacktest).mock.calls.at(-1)
    expect(lastCall?.[0].exclude_st).toBe(false)
  })

  it('submits a fixed share count and validates its range', async () => {
    const wrapper = mount(PortfolioBacktestPage)
    await flushPromises()
    await wrapper.get('[data-testid="portfolio-position-mode"]').setValue('fixed')
    await wrapper.get('[data-testid="portfolio-fixed-size"]').setValue(10)
    await wrapper.get('[data-testid="portfolio-config"]').trigger('submit')
    await flushPromises()

    expect(wrapper.text()).toContain('固定买入股数必须是 100 到 100000000 的整数')
    expect(portfolioApi.createPortfolioBacktest).not.toHaveBeenCalled()

    await wrapper.get('[data-testid="portfolio-fixed-size"]').setValue(300)
    await wrapper.get('[data-testid="portfolio-config"]').trigger('submit')
    await flushPromises()

    expect(portfolioApi.createPortfolioBacktest).toHaveBeenCalledWith(expect.objectContaining({
      position_mode: 'fixed',
      fixed_size: 300,
    }))
  })

  it('loads a strategy discovery candidate into the ordinary portfolio form', async () => {
    sessionStorage.setItem('aquest.strategy-discovery.apply', JSON.stringify({
      universe: 'all',
      start_date: '2024-01-02',
      end_date: '2024-01-06',
      ranking_value: 'indicator_three.varo7',
      rank_order: 'asc',
      max_positions: 3,
      rebalance_frequency: 'weekly',
      buy_rule: {
        signals: [],
        required_conditions: [{ left_value: 'indicator_three.varo6', operator: 'gte', right_value: null, right_constant: 20 }],
        conditions: [{ left_value: 'indicator_three.var1', operator: 'gt', right_value: null, right_constant: 60 }],
        combine_mode: 'all',
        minimum_matches: null,
      },
      sell_rule: {
        signals: [],
        conditions: [{ left_value: 'indicator_three.var1', operator: 'lt', right_value: null, right_constant: 40 }],
        combine_mode: 'all',
        minimum_matches: null,
      },
      stop_loss_mode: 'atr',
      stop_loss_pct: null,
      stop_loss_atr_multiplier: 2,
      stop_loss_cooldown_days: 3,
      take_profit_mode: 'pct',
      take_profit_pct: 0.2,
      take_profit_atr_multiplier: null,
      take_profit_cooldown_days: 5,
      trailing_stop_mode: 'pct',
      trailing_stop_pct: null,
      trailing_stop_atr_multiplier: null,
      trailing_stop_cooldown_days: 0,
      atr_period: 14,
      commission: 0.0001,
      min_commission: 0.1,
    }))
    sessionStorage.setItem('aquest.strategy-discovery.apply-meta', JSON.stringify({
      candidate_hash: 'candidate-1',
      validation_metrics: {
        total_return: 0.12,
        annual_return: 0.22,
        sharpe: 1.6,
        max_drawdown: 0.03,
        total_trades: 4,
        win_rate: 0.5,
      },
    }))

    const wrapper = mount(PortfolioBacktestPage)
    await flushPromises()

    expect((wrapper.get('[data-testid="portfolio-max-positions"]').element as HTMLInputElement).value).toBe('3')
    expect((wrapper.get('[data-testid="portfolio-ranking-value"]').element as HTMLSelectElement).value).toBe('indicator_three.varo7')
    expect((wrapper.get('[data-testid="portfolio-rank-order"]').element as HTMLSelectElement).value).toBe('asc')
    expect((wrapper.get('[data-testid="portfolio-stop-loss-mode"]').element as HTMLSelectElement).value).toBe('atr')
    expect((wrapper.get('[data-testid="portfolio-stop-loss-atr"]').element as HTMLInputElement).value).toBe('2')
    expect((wrapper.get('[data-testid="portfolio-stop-loss-cooldown"]').element as HTMLInputElement).value).toBe('3')

    await wrapper.get('[data-testid="portfolio-config"]').trigger('submit')
    await flushPromises()
    expect(portfolioApi.createPortfolioBacktest).toHaveBeenCalledWith(expect.objectContaining({
      ranking_value: 'indicator_three.varo7',
      max_positions: 3,
      buy_rule: expect.objectContaining({
        required_conditions: [expect.objectContaining({ left_value: 'indicator_three.varo6', right_constant: 20 })],
      }),
      stop_loss_mode: 'atr',
      stop_loss_pct: null,
      stop_loss_atr_multiplier: 2,
      stop_loss_cooldown_days: 3,
      take_profit_pct: 0.2,
      take_profit_cooldown_days: 5,
    }))
    expect(wrapper.get('[data-testid="portfolio-replay-comparison"]').text()).toContain('绩效摘要：一致')
    expect(sessionStorage.getItem('aquest.strategy-discovery.apply')).toBeNull()
  })

  it('loads a condition-research study including its ranking advice', async () => {
    sessionStorage.clear()
    sessionStorage.setItem('aquest.condition-research.apply', JSON.stringify({
      universe: 'sh',
      instrument_types: ['stock'],
      boards: ['main'],
      start_date: '2023-01-01',
      end_date: '2024-12-31',
      buy_rule: {
        signals: [],
        conditions: [{ left_value: 'indicator_three.var1', operator: 'gt', right_value: null, right_constant: 12 }],
        combine_mode: 'all',
        minimum_matches: null,
      },
      sell_rule: null,
      ranking_value: 'indicator_three.varo7',
      rank_order: 'asc',
      max_positions: 4,
      formula_text: null,
      formula_parameters: {},
    }))

    const wrapper = mount(PortfolioBacktestPage)
    await flushPromises()

    expect((wrapper.get('[data-testid="portfolio-ranking-value"]').element as HTMLSelectElement).value).toBe('indicator_three.varo7')
    expect((wrapper.get('[data-testid="portfolio-rank-order"]').element as HTMLSelectElement).value).toBe('asc')
    expect((wrapper.get('[data-testid="portfolio-max-positions"]').element as HTMLInputElement).value).toBe('4')
    expect(wrapper.text()).toContain('买卖条件研究')

    await wrapper.get('[data-testid="portfolio-config"]').trigger('submit')
    await flushPromises()
    expect(portfolioApi.createPortfolioBacktest).toHaveBeenCalledWith(expect.objectContaining({
      ranking_value: 'indicator_three.varo7',
      rank_order: 'asc',
      max_positions: 4,
      buy_rule: expect.objectContaining({
        conditions: [expect.objectContaining({ right_constant: 12 })],
      }),
    }))
    expect(sessionStorage.getItem('aquest.condition-research.apply')).toBeNull()
    sessionStorage.clear()
  })

  it('warns when a one-sided study leaves a rule unset instead of reusing defaults', async () => {
    sessionStorage.clear()
    sessionStorage.setItem('aquest.condition-research.apply', JSON.stringify({
      universe: 'sh',
      instrument_types: ['stock'],
      boards: [],
      date_mode: 'full',
      buy_rule: {
        signals: [],
        conditions: [{ left_value: 'indicator_three.var1', operator: 'gt', right_value: null, right_constant: 12 }],
        combine_mode: 'all',
        minimum_matches: null,
      },
      sell_rule: null,
      ranking_value: 'indicator_three.varo7',
      rank_order: 'desc',
      max_positions: 4,
      formula_text: null,
      formula_parameters: {},
    }))

    const wrapper = mount(PortfolioBacktestPage)
    await flushPromises()

    // 研究没设卖出条件时不能默默沿用组合页默认（默认卖出 + 8% 止损），
    // 否则用户以为在复现研究基线。
    expect(wrapper.get('[data-testid="portfolio-message"]').text()).toContain('未设卖出')
    // 日期区间要连 mode 一起还原，只回填绝对日期会显示错误的窗口。
    expect((wrapper.get('[data-testid="portfolio-date-mode"]').element as HTMLSelectElement).value).toBe('full')
    sessionStorage.clear()
  })

  it('loads a saved history detail and toggles board filters', async () => {
    vi.mocked(portfolioApi.fetchPortfolioBacktestHistory).mockResolvedValue([{
      history_id: 'history-1', backtest_type: 'portfolio', created_at: '2024-01-06T08:00:00Z',
      started_at: '2024-01-06T08:00:00Z', finished_at: '2024-01-06T08:01:00Z',
      duration_seconds: 60, summary: {},
    }])
    vi.mocked(portfolioApi.getPortfolioBacktestHistory).mockResolvedValue({
      history_id: 'history-1', backtest_type: 'portfolio', created_at: '2024-01-06T08:00:00Z',
      started_at: '2024-01-06T08:00:00Z', finished_at: '2024-01-06T08:01:00Z',
      duration_seconds: 60, summary: {}, request: {}, result,
    })
    const wrapper = mount(PortfolioBacktestPage)
    await flushPromises()

    await wrapper.get('[data-testid="portfolio-history-load-history-1"]').trigger('click')
    await flushPromises()

    expect(portfolioApi.getPortfolioBacktestHistory).toHaveBeenCalledWith('history-1')
    expect(wrapper.get('[data-testid="portfolio-message"]').text()).toContain('已还原历史组合回测')
    expect(wrapper.get('[data-testid="portfolio-message"]').text()).toContain('请确认后重新运行')
    expect(wrapper.get('[data-testid="portfolio-results"]').text()).toContain('12.00%')

    await wrapper.get('[data-testid="portfolio-scope-board-star"]').setValue(true)
    await wrapper.get('[data-testid="portfolio-config"]').trigger('submit')
    await flushPromises()

    expect(portfolioApi.createPortfolioBacktest).toHaveBeenCalledWith(expect.objectContaining({
      boards: ['star'],
    }))
  })

  it('binds the slot count, ranking value and risk cooldowns', async () => {
    const wrapper = mount(PortfolioBacktestPage)
    await flushPromises()

    await wrapper.get('[data-testid="portfolio-max-positions"]').setValue(8)
    await wrapper.get('[data-testid="portfolio-ranking-value"]').setValue('indicator_three.var1')
    await wrapper.get('[data-testid="portfolio-stop-loss-cooldown"]').setValue(2)
    await wrapper.get('[data-testid="portfolio-take-profit-enabled"]').setValue(true)
    await wrapper.get('[data-testid="portfolio-take-profit-cooldown"]').setValue(3)
    await wrapper.get('[data-testid="portfolio-trailing-stop-enabled"]').setValue(true)
    await wrapper.get('[data-testid="portfolio-trailing-stop-cooldown"]').setValue(4)
    await wrapper.get('[data-testid="portfolio-config"]').trigger('submit')
    await flushPromises()

    expect(portfolioApi.createPortfolioBacktest).toHaveBeenCalledWith(expect.objectContaining({
      max_positions: 8,
      ranking_value: 'indicator_three.var1',
      stop_loss_cooldown_days: 2,
      take_profit_cooldown_days: 3,
      trailing_stop_cooldown_days: 4,
    }))
  })

  it('switches every risk control to the ATR mode', async () => {
    const wrapper = mount(PortfolioBacktestPage)
    await flushPromises()

    await wrapper.get('[data-testid="portfolio-stop-loss-mode"]').setValue('atr')
    await wrapper.get('[data-testid="portfolio-take-profit-enabled"]').setValue(true)
    await wrapper.get('[data-testid="portfolio-take-profit-mode"]').setValue('atr')
    await wrapper.get('[data-testid="portfolio-trailing-stop-enabled"]').setValue(true)
    await wrapper.get('[data-testid="portfolio-trailing-stop-mode"]').setValue('atr')
    await wrapper.get('[data-testid="portfolio-config"]').trigger('submit')
    await flushPromises()

    expect(portfolioApi.createPortfolioBacktest).toHaveBeenCalledWith(expect.objectContaining({
      stop_loss_mode: 'atr',
      take_profit_mode: 'atr',
      trailing_stop_mode: 'atr',
    }))
  })

  it('applies exit risk without replacing the current date range with the validation range', async () => {
    const wrapper = mount(PortfolioBacktestPage)
    await flushPromises()
    await wrapper.get('[data-testid="portfolio-risk-search-level"]').setValue(2000)
    await wrapper.get('[data-testid="start-portfolio-risk-optimization"]').trigger('click')
    await flushPromises()

    expect(strategyDiscoveryApi.createPortfolioRiskOptimization).toHaveBeenCalledWith(expect.objectContaining({
      max_trials: 2000,
      stop_loss_pct: 0.08,
      buy_rule: expect.objectContaining({ conditions: [expect.objectContaining({ left_value: 'indicator_three.var1' })] }),
      ranking_value: 'indicator_three.varo7',
    }))
    expect(strategyDiscoveryApi.getStrategyDiscovery).toHaveBeenCalledWith('risk-opt-1')
    expect(wrapper.get('[data-testid="portfolio-risk-optimization-results"]').text()).toContain('退出风控候选')
    expect(wrapper.get('[data-testid="portfolio-risk-candidates"]').text()).toContain('15.00%')
    expect(wrapper.get('[data-testid="portfolio-risk-candidates"]').text()).toContain('22.00%')

    await wrapper.get('[data-testid="portfolio-risk-apply-risk-candidate-1"]').trigger('click')
    expect((wrapper.get('[data-testid="portfolio-date-mode"]').element as HTMLSelectElement).value).toBe('recent5')
    expect(wrapper.find('[data-testid="portfolio-start"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="portfolio-end"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="portfolio-message"]').text()).toContain('保留当前区间')

    await wrapper.get('[data-testid="portfolio-config"]').trigger('submit')
    await flushPromises()

    expect(portfolioApi.createPortfolioBacktest).toHaveBeenCalledWith(expect.objectContaining({
      date_mode: 'recent5',
      start_date: null,
      end_date: null,
      trailing_stop_pct: 0.05,
      stop_loss_pct: 0.08,
      take_profit_pct: 0.2,
    }))
    expect(wrapper.find('[data-testid="portfolio-replay-comparison"]').exists()).toBe(false)
  })

  it('blocks candidate application after fixed strategy settings change', async () => {
    const wrapper = mount(PortfolioBacktestPage)
    await flushPromises()
    await wrapper.get('[data-testid="start-portfolio-risk-optimization"]').trigger('click')
    await flushPromises()
    await wrapper.get('[data-testid="portfolio-ranking-value"]').setValue('indicator_three.var1')

    expect(wrapper.get('[data-testid="portfolio-risk-optimization-stale"]').text()).toContain('请重新寻优')
    expect((wrapper.get('[data-testid="portfolio-risk-apply-risk-candidate-1"]').element as HTMLButtonElement).disabled).toBe(true)
  })

  it('edits a custom formula parameter before submitting', async () => {
    vi.mocked(formulaApi.parseFormula).mockResolvedValueOnce({
      parameters: [{ name: 'N', default: 5, minimum: 1, maximum: 100, step: 1 }],
      signals: [{ id: 'custom.buy', display_name: '买入', description: '' }, { id: 'custom.sell', display_name: '卖出', description: '' }],
      values: [{ id: 'custom.rank', display_name: '排序值', description: '' }],
      minimum_bars: 6,
      warnings: [],
    })
    const wrapper = mount(PortfolioBacktestPage)
    await flushPromises()
    await wrapper.get('[data-testid="portfolio-mode-custom"]').trigger('click')
    await wrapper.get('[data-testid="portfolio-formula"]').setValue('N:=5; 买入:C>10; 卖出:C<10; 排序:C;')
    await wrapper.get('[data-testid="portfolio-parse-formula"]').trigger('click')
    await flushPromises()

    await wrapper.get('[data-testid="portfolio-param-N"]').setValue(12)
    await wrapper.get('[data-testid="portfolio-config"]').trigger('submit')
    await flushPromises()

    expect(portfolioApi.createPortfolioBacktest).toHaveBeenCalledWith(expect.objectContaining({
      formula_parameters: { N: 12 },
      ranking_value: 'custom.rank',
    }))
  })

  it('tells the user where strategies come from before any is saved', async () => {
    const wrapper = mount(PortfolioBacktestPage)
    await flushPromises()

    expect(wrapper.get('[data-testid="strategy-count"]').text()).toBe('0 个策略')
    expect(wrapper.get('[data-testid="strategy-empty"]').text()).toContain('保存为策略')
    expect(wrapper.find('[data-testid="strategy-save-bar"]').exists()).toBe(false)
  })

  it('saves the current configuration with the result snapshot', async () => {
    vi.mocked(strategiesApi.createStrategy).mockResolvedValue(savedStrategyDetail)
    vi.mocked(strategiesApi.fetchStrategies)
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([savedStrategy])

    const wrapper = mount(PortfolioBacktestPage)
    await flushPromises()
    await wrapper.get('[data-testid="portfolio-config"]').trigger('submit')
    await flushPromises()

    await wrapper.get('[data-testid="strategy-name-input"]').setValue('主力进场轮动')
    await wrapper.get('[data-testid="strategy-save"]').trigger('click')
    await flushPromises()

    expect(strategiesApi.createStrategy).toHaveBeenCalledWith(expect.objectContaining({
      name: '主力进场轮动',
    }))
    const payload = vi.mocked(strategiesApi.createStrategy).mock.calls[0][0]
    // 存的是当前表单配置，不是这次回测的请求快照——用户可能在结果上微调过。
    expect(payload.request.ranking_value).toBe('indicator_three.varo7')
    const savedSummary = payload.summary as { performance: { annual_return: number } }
    expect(savedSummary.performance.annual_return).toBe(0.22)
    expect(wrapper.get('[data-testid="strategy-count"]').text()).toBe('1 个策略')
    expect(wrapper.get('[data-testid="portfolio-message"]').text()).toContain('已保存')
  })

  it('refuses to save without a name', async () => {
    const wrapper = mount(PortfolioBacktestPage)
    await flushPromises()
    await wrapper.get('[data-testid="portfolio-config"]').trigger('submit')
    await flushPromises()

    await wrapper.get('[data-testid="strategy-save"]').trigger('click')
    await flushPromises()

    expect(strategiesApi.createStrategy).not.toHaveBeenCalled()
    expect(wrapper.get('[data-testid="portfolio-message"]').text()).toContain('策略名称')
  })

  it('loads a saved strategy back into the form', async () => {
    vi.mocked(strategiesApi.fetchStrategies).mockResolvedValue([savedStrategy])
    vi.mocked(strategiesApi.getStrategy).mockResolvedValue(savedStrategyDetail)

    const wrapper = mount(PortfolioBacktestPage)
    await flushPromises()

    expect(wrapper.get('[data-testid="strategy-range-strategy-1"]').text()).toBe('2024-01-02 ~ 2024-01-06')
    expect(wrapper.get('[data-testid="strategy-row-strategy-1"]').text()).toContain('22.00%')

    await wrapper.get('[data-testid="strategy-load-strategy-1"]').trigger('click')
    await flushPromises()

    expect(strategiesApi.getStrategy).toHaveBeenCalledWith('strategy-1')
    expect(wrapper.get('[data-testid="portfolio-message"]').text()).toContain('已载入策略「主力进场轮动」')
    // 固化下来的绝对区间要回到日期选择器上，而不是"最近五年"。
    expect((wrapper.get('[data-testid="portfolio-date-mode"]').element as HTMLSelectElement).value).toBe('custom')
  })

  it('renames a strategy inline without touching its configuration', async () => {
    vi.mocked(strategiesApi.fetchStrategies).mockResolvedValue([savedStrategy])
    vi.mocked(strategiesApi.updateStrategy).mockResolvedValue({ ...savedStrategyDetail, name: '新名字' })

    const wrapper = mount(PortfolioBacktestPage)
    await flushPromises()
    await wrapper.get('[data-testid="strategy-rename-strategy-1"]').trigger('click')
    await flushPromises()

    await wrapper.get('[data-testid="strategy-rename-input-strategy-1"]').setValue('新名字')
    await wrapper.get('[data-testid="strategy-rename-save-strategy-1"]').trigger('click')
    await flushPromises()

    expect(strategiesApi.updateStrategy).toHaveBeenCalledWith('strategy-1', { name: '新名字' })
    expect(wrapper.get('[data-testid="portfolio-message"]').text()).toContain('已重命名')
  })

  it('deletes a strategy only after a second confirmation', async () => {
    vi.mocked(strategiesApi.fetchStrategies).mockResolvedValue([savedStrategy])

    const wrapper = mount(PortfolioBacktestPage)
    await flushPromises()
    await wrapper.get('[data-testid="strategy-delete-strategy-1"]').trigger('click')
    await flushPromises()

    // 第一下只是把按钮换成"确认删除"，策略还在。
    expect(strategiesApi.deleteStrategy).not.toHaveBeenCalled()
    expect(wrapper.find('[data-testid="strategy-confirm-delete-strategy-1"]').exists()).toBe(true)

    vi.mocked(strategiesApi.fetchStrategies).mockResolvedValue([])
    await wrapper.get('[data-testid="strategy-confirm-delete-strategy-1"]').trigger('click')
    await flushPromises()

    expect(strategiesApi.deleteStrategy).toHaveBeenCalledWith('strategy-1')
    expect(wrapper.get('[data-testid="strategy-count"]').text()).toBe('0 个策略')
    expect(wrapper.get('[data-testid="portfolio-message"]').text()).toContain('已删除')
  })
})
