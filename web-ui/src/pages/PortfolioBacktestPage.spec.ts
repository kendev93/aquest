import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { FormulaScreenMetadata, PortfolioBacktestResult } from '../types'
import PortfolioBacktestPage from './PortfolioBacktestPage.vue'

import * as portfolioApi from '../api/portfolioBacktest'
import * as formulaApi from '../api/formulaScreen'
import { FormulaScreenApiError } from '../api/formulaScreen'

vi.mock('../api/formulaScreen', async () => {
  const actual = await vi.importActual<typeof import('../api/formulaScreen')>('../api/formulaScreen')
  return { ...actual, fetchMetadata: vi.fn(), parseFormula: vi.fn() }
})

vi.mock('../api/portfolioBacktest', async () => {
  const actual = await vi.importActual<typeof import('../api/portfolioBacktest')>('../api/portfolioBacktest')
  return { ...actual, createPortfolioBacktest: vi.fn(), getPortfolioBacktest: vi.fn(), getPortfolioBacktestResults: vi.fn() }
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
    { date: '2024-01-03', signal_date: '2024-01-02', market: 'SH', code: '600000', direction: 'BUY', size: 400, price: 10, commission: 0, stamp_tax: 0, slippage: 0, pnl: 0, cost_basis: 0, reason: '排名入选', rejected: false },
    { date: '2024-01-04', signal_date: '2024-01-03', market: 'SH', code: '600000', direction: 'SELL', size: 400, price: 11, commission: 0, stamp_tax: 0, slippage: 0, pnl: 400, cost_basis: 4000, reason: '止盈', rejected: false },
  ],
  states: [{
    date: '2024-01-06', cash: 200, position_value: 11000, total: 11200, positions_count: 2,
    holdings: [{ market: 'SH', code: '600000', size: 400, entry_price: 10, close: 11, unrealized_pnl: 400 }],
  }],
  ranking_events: [{
    date: '2024-01-03', slots_available: 1, ranking_value: 'indicator_three.varo7',
    candidates: [{ rank: 1, market: 'SZ', code: '000001', score: 9.5, selected: true }],
  }],
  failure_reasons: {},
  diagnostic: null,
}

describe('PortfolioBacktestPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(formulaApi.fetchMetadata).mockResolvedValue(metadata)
    vi.mocked(portfolioApi.createPortfolioBacktest).mockResolvedValue({ job_id: 'portfolio-1', status: 'queued' })
    vi.mocked(portfolioApi.getPortfolioBacktest).mockResolvedValue({
      job_id: 'portfolio-1', status: 'completed', progress: 1, total_candidates: 3,
      total_scanned: 3, errors: 0, error: null, result: null,
    })
    vi.mocked(portfolioApi.getPortfolioBacktestResults).mockResolvedValue(result)
  })

  it('submits ranked slot configuration and renders holdings, trades, and candidates', async () => {
    const wrapper = mount(PortfolioBacktestPage)
    await flushPromises()

    await wrapper.get('[data-testid="portfolio-config"]').trigger('submit')
    await flushPromises()

    expect(portfolioApi.createPortfolioBacktest).toHaveBeenCalledWith(expect.objectContaining({
      selected_signals: [],
      buy_rule: expect.objectContaining({
        signals: [],
        conditions: [expect.objectContaining({ left_value: 'indicator_three.var1' })],
      }),
      ranking_value: 'indicator_three.varo7', max_positions: 5, stop_loss_pct: 0.08,
    }))
    expect(wrapper.get('[data-testid="portfolio-results"]').text()).toContain('12.00%')
    expect(wrapper.get('[data-testid="portfolio-holdings"]').text()).toContain('600000')
    expect(wrapper.get('[data-testid="portfolio-trades"]').text()).toContain('止盈')
    expect(wrapper.get('[data-testid="portfolio-ranking"]').text()).toContain('000001')
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
    await wrapper.get('[data-testid="portfolio-fitness-enabled"]').setValue(true)
    await wrapper.get('[data-testid="portfolio-fitness-score"]').setValue(60)
    await wrapper.get('[data-testid="portfolio-fitness-trades"]').setValue(3)
    await wrapper.get('[data-testid="portfolio-fitness-drawdown"]').setValue(40)
    await wrapper.get('#portfolio-sell-rule-operator-0').setValue('gte')
    await wrapper.get('[data-testid="portfolio-stop-loss-enabled"]').setValue(false)
    await wrapper.get('[data-testid="portfolio-take-profit-enabled"]').setValue(true)
    await wrapper.get('[data-testid="portfolio-advanced-toggle"]').trigger('click')
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
      fitness_filter_enabled: true, fitness_min_score: 60, fitness_min_trades: 3,
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
})
