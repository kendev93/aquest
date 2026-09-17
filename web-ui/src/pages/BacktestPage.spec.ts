import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { BacktestJobState, FormulaScreenMetadata, RuleGroupPayload } from '../types'
import RuleGroupEditor from '../components/RuleGroupEditor.vue'
import BacktestPage from './BacktestPage.vue'

import * as backtestApi from '../api/backtest'
import * as formulaApi from '../api/formulaScreen'
import * as jobsApi from '../api/jobs'

vi.mock('../api/formulaScreen', async () => {
  const actual = await vi.importActual<typeof import('../api/formulaScreen')>('../api/formulaScreen')
  return { ...actual, fetchMetadata: vi.fn(), parseFormula: vi.fn() }
})

vi.mock('../api/backtest', async () => {
  const actual = await vi.importActual<typeof import('../api/backtest')>('../api/backtest')
  return {
    ...actual,
    createBacktest: vi.fn(),
    getBacktest: vi.fn(),
    getBacktestResults: vi.fn(),
    fetchBacktestHistory: vi.fn(),
    getBacktestHistory: vi.fn(),
    deleteBacktestHistory: vi.fn(),
  }
})

vi.mock('../api/jobs', async () => {
  const actual = await vi.importActual<typeof import('../api/jobs')>('../api/jobs')
  return { ...actual, fetchActiveJobs: vi.fn() }
})

const metadata: FormulaScreenMetadata = {
  indicators: [{
    id: 'indicator_three', display_name: '指标三 · 拉升准备', minimum_bars: 34, recommended_bars: 120,
    signals: [
      { id: 'indicator_three.prepare_rally', display_name: '准备拉升', description: 'VAR1 上穿 8' },
      { id: 'indicator_three.end_zone', display_name: '终', description: 'VAR1 > 90' },
    ],
    values: [
      { id: 'indicator_three.var1', display_name: 'VAR1', description: '变量' },
    ],
  }],
  combine_modes: [],
  supported_markets: ['SH', 'SZ'],
  supported_universe: [],
  periods: [{ value: 'daily', label: '日线' }],
  data_directory_help: '本地目录',
  default_vipdoc_path: '/data/vipdoc',
}

describe('BacktestPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    vi.mocked(formulaApi.fetchMetadata).mockResolvedValue(metadata)
    vi.mocked(backtestApi.createBacktest).mockResolvedValue({ job_id: 'bt-1', status: 'queued' })
    vi.mocked(backtestApi.getBacktest).mockResolvedValue({
      job_id: 'bt-1', status: 'completed', progress: 1, total_candidates: 1, total_scanned: 1,
      errors: 0, error: null, result: null,
    })
    vi.mocked(backtestApi.getBacktestResults).mockResolvedValue({
      market: 'SH', code: '600000', name: '浦发银行', bars: 5, start_date: '2024-01-02', end_date: '2024-01-06',
      buy_signal: 'indicator_three.prepare_rally', sell_signal: 'indicator_three.end_zone',
      performance: { total_return: 0.04, annual_return: 0.1, max_drawdown: 0.02, sharpe: 1.2, total_trades: 2, win_rate: 1, end_value: 10400, start_cash: 10000 },
      equity_curve: [
        { date: '2024-01-02', total: 10000, cash: 10000, position_value: 0, drawdown: 0, drawdown_pct: 0 },
        { date: '2024-01-06', total: 10400, cash: 10400, position_value: 0, drawdown: 0, drawdown_pct: 0 },
      ],
      trades: [{ date: '2024-01-03', name: '浦发银行', direction: 'BUY', size: 800, price: 12, commission: 0, slippage: 0, pnl: 0, rejected: false }],
      positions: [], diagnostic: null,
      configuration: { initial_cash: 10000 },
    })
    vi.mocked(backtestApi.fetchBacktestHistory).mockResolvedValue([])
    vi.mocked(jobsApi.fetchActiveJobs).mockResolvedValue([])
  })

  it('recovers and resumes a running backtest after the page is refreshed', async () => {
    vi.mocked(jobsApi.fetchActiveJobs).mockResolvedValueOnce([{
      job_id: 'bt-refresh', description: '历史回测', status: 'running', progress: 0.42,
      total_candidates: 1, total_scanned: 0, errors: 0, error: null, result: null,
    }])
    let resolveStatus!: (state: BacktestJobState) => void
    vi.mocked(backtestApi.getBacktest).mockImplementationOnce(() => new Promise((resolve) => {
      resolveStatus = resolve
    }))

    const wrapper = mount(BacktestPage)
    await flushPromises()

    expect(jobsApi.fetchActiveJobs).toHaveBeenCalled()
    expect(backtestApi.getBacktest).toHaveBeenCalledWith('bt-refresh')
    expect(wrapper.get('[data-testid="backtest-progress"]').text()).toContain('42%')

    resolveStatus({
      job_id: 'bt-refresh', status: 'completed', progress: 1, total_candidates: 1, total_scanned: 1,
      errors: 0, error: null, result: null,
    })
    await flushPromises()
    expect(wrapper.get('[data-testid="backtest-results"]').text()).toContain('浦发银行')
  })

  it('submits a single-stock backtest and renders performance and trades', async () => {
    const wrapper = mount(BacktestPage)
    await flushPromises()
    expect((wrapper.get('[data-testid="backtest-date-mode"]').element as HTMLSelectElement).value).toBe('recent5')
    expect(wrapper.text()).toContain('默认使用本地最新已完成日线向前 5 年')
    await wrapper.get('[data-testid="backtest-code"]').setValue('600000')
    await wrapper.get('[data-testid="backtest-market"]').setValue('SZ')
    await wrapper.get('[data-testid="backtest-market"]').setValue('SH')
    await wrapper.get('[data-testid="backtest-date-mode"]').setValue('custom')
    await wrapper.get('[data-testid="backtest-start"]').setValue('2024-01-02')
    await wrapper.get('[data-testid="backtest-end"]').setValue('2024-01-06')
    await wrapper.get('[data-testid="backtest-config"]').trigger('submit')
    await flushPromises()

    expect(backtestApi.createBacktest).toHaveBeenCalledWith(expect.objectContaining({
      market: 'SH', code: '600000',
      buy_signal: null, sell_signal: null,
      buy_rule: expect.objectContaining({
        conditions: [expect.objectContaining({ left_value: 'indicator_three.var1', operator: 'cross_up', right_constant: 8 })],
      }),
      sell_rule: expect.objectContaining({
        conditions: [expect.objectContaining({ left_value: 'indicator_three.var1', operator: 'gt', right_constant: 90 })],
      }),
      date_mode: 'custom', start_date: '2024-01-02', end_date: '2024-01-06',
    }))
    expect(wrapper.get('[data-testid="backtest-results"]').text()).toContain('4.00%')
    expect(wrapper.get('[data-testid="backtest-results"]').text()).toContain('浦发银行')
    expect(wrapper.get('[data-testid="backtest-trades"]').text()).toContain('2024-01-03')
    expect(wrapper.get('[data-testid="equity-chart"]')).toBeTruthy()
  })

  it('forwards rule editor updates into the buy and sell payload', async () => {
    const wrapper = mount(BacktestPage)
    await flushPromises()

    const buyRule: RuleGroupPayload = {
      signals: [],
      combine_mode: 'all',
      minimum_matches: null,
      conditions: [{ left_value: 'indicator_three.var1', operator: 'gt', right_value: null, right_constant: 5 }],
    }
    const sellRule: RuleGroupPayload = {
      signals: [],
      combine_mode: 'all',
      minimum_matches: null,
      conditions: [{ left_value: 'indicator_three.var1', operator: 'lt', right_value: null, right_constant: 95 }],
    }
    wrapper.findComponent(RuleGroupEditor).vm.$emit('update:modelValue', buyRule)
    wrapper.findAllComponents(RuleGroupEditor)[1].vm.$emit('update:modelValue', sellRule)
    await flushPromises()

    await wrapper.get('[data-testid="backtest-code"]').setValue('600000')
    await wrapper.get('[data-testid="backtest-config"]').trigger('submit')
    await flushPromises()

    expect(backtestApi.createBacktest).toHaveBeenCalledWith(expect.objectContaining({
      buy_rule: expect.objectContaining({
        conditions: [expect.objectContaining({ operator: 'gt', right_constant: 5 })],
      }),
      sell_rule: expect.objectContaining({
        conditions: [expect.objectContaining({ operator: 'lt', right_constant: 95 })],
      }),
    }))
  })

  it('parses custom buy and sell outputs and invalidates them after editing', async () => {
    vi.mocked(formulaApi.parseFormula).mockResolvedValueOnce({
      parameters: [{ name: 'N', default: 5, minimum: 1, maximum: 10000, step: 1 }],
      signals: [
        { id: 'custom.buy', display_name: '买入', description: '买入条件' },
        { id: 'custom.sell', display_name: '卖出', description: '卖出条件' },
      ],
      values: [
        { id: 'custom.buy', display_name: '买入值', description: '买入数值' },
        { id: 'custom.sell', display_name: '卖出值', description: '卖出数值' },
      ],
      minimum_bars: 6,
      warnings: [],
    })
    const wrapper = mount(BacktestPage)
    await flushPromises()
    await wrapper.get('[data-testid="backtest-mode-custom"]').trigger('click')
    await wrapper.get('[data-testid="backtest-formula"]').setValue('N:=5; 买入:C>10; 卖出:C<10;')
    await wrapper.get('[data-testid="backtest-parse-formula"]').trigger('click')
    await flushPromises()

    await wrapper.get('#backtest-param-N').setValue(0)
    await wrapper.get('[data-testid="backtest-code"]').setValue('600000')
    await wrapper.get('[data-testid="backtest-config"]').trigger('submit')
    expect(wrapper.text()).toContain('参数 N 必须在 1 到 10000 之间')
    await wrapper.get('#backtest-param-N').setValue(8)
    expect((wrapper.get('#backtest-buy-rule-left-0').element as HTMLSelectElement).value).toBe('custom.buy')
    expect((wrapper.get('#backtest-sell-rule-left-0').element as HTMLSelectElement).value).toBe('custom.sell')
    await wrapper.get('[data-testid="backtest-formula"]').setValue('N:=8; 买入:C>10; 卖出:C<10;')
    await flushPromises()
    expect(wrapper.text()).toContain('公式已修改，请重新解析')

    await wrapper.get('[data-testid="backtest-mode-preset"]').trigger('click')
    expect((wrapper.get('#backtest-buy-rule-left-0').element as HTMLSelectElement).value).toBe('indicator_three.var1')
  })

  it('validates required fields and shows a failed request message', async () => {
    const wrapper = mount(BacktestPage)
    await flushPromises()
    await wrapper.get('[data-testid="backtest-config"]').trigger('submit')
    expect(wrapper.text()).toContain('请输入六位股票代码')

    vi.mocked(backtestApi.createBacktest).mockRejectedValueOnce(new Error('network down'))
    await wrapper.get('[data-testid="backtest-code"]').setValue('600000')
    await wrapper.get('[data-testid="backtest-config"]').trigger('submit')
    await flushPromises()
    expect(wrapper.get('[data-testid="backtest-message"]').text()).toContain('回测失败')
  })

  it('requires both dates when a custom range is selected', async () => {
    const wrapper = mount(BacktestPage)
    await flushPromises()
    await wrapper.get('[data-testid="backtest-code"]').setValue('600000')
    await wrapper.get('[data-testid="backtest-date-mode"]').setValue('custom')
    await wrapper.get('[data-testid="backtest-config"]').trigger('submit')

    expect(wrapper.text()).toContain('请选择开始日期')
    expect(wrapper.text()).toContain('请选择结束日期')
    expect(backtestApi.createBacktest).not.toHaveBeenCalled()
  })

  it('validates advanced cash and fixed-size settings before submitting', async () => {
    const wrapper = mount(BacktestPage)
    await flushPromises()
    await wrapper.get('[data-testid="backtest-code"]').setValue('600000')
    await wrapper.get('[data-testid="backtest-advanced-toggle"]').trigger('click')
    await wrapper.get('[data-testid="initial-cash"]').setValue(0)
    await wrapper.get('[data-testid="position-mode"]').setValue('fixed')
    await wrapper.get('[data-testid="fixed-size"]').setValue(50)
    await wrapper.get('[data-testid="backtest-config"]').trigger('submit')

    expect(wrapper.text()).toContain('初始资金必须大于 0')
    expect(wrapper.text()).toContain('固定股数必须是 100 的整数倍')
    expect(backtestApi.createBacktest).not.toHaveBeenCalled()
  })

  it('supports fixed-size orders and exports a sparse result safely', async () => {
    vi.mocked(backtestApi.getBacktestResults).mockResolvedValueOnce({
      market: 'SH', code: '600000', bars: 1, start_date: '2024-01-02', end_date: '2024-01-02',
      buy_signal: 'indicator_three.prepare_rally', sell_signal: 'indicator_three.end_zone',
      performance: { total_return: null, annual_return: null, max_drawdown: null, sharpe: null, total_trades: null, win_rate: null },
      equity_curve: [{ date: '2024-01-02', total: null, cash: null, position_value: null, drawdown: null, drawdown_pct: null }],
      trades: [], positions: [], configuration: { initial_cash: 10000, execution: 'next_open', position_mode: 'fixed', fixed_size: 200 }, diagnostic: '数据不足',
    })
    const wrapper = mount(BacktestPage)
    await flushPromises()
    await wrapper.get('[data-testid="backtest-code"]').setValue('600000')
    await wrapper.get('[data-testid="backtest-advanced-toggle"]').trigger('click')
    await wrapper.get('[data-testid="initial-cash"]').setValue(50000)
    await wrapper.get('[data-testid="execution"]').setValue('next_close')
    await wrapper.get('[data-testid="position-mode"]').setValue('fixed')
    await wrapper.get('[data-testid="fixed-size"]').setValue(200)
    await wrapper.get('[data-testid="commission"]').setValue(0.001)
    await wrapper.get('[data-testid="min-commission"]').setValue(3)
    await wrapper.get('[data-testid="stamp-tax"]').setValue(0.002)
    await wrapper.get('[data-testid="slippage"]').setValue(0.01)
    await wrapper.get('[data-testid="backtest-config"]').trigger('submit')
    await flushPromises()

    expect(backtestApi.createBacktest).toHaveBeenCalledWith(expect.objectContaining({ position_mode: 'fixed', fixed_size: 200 }))
    expect(wrapper.get('[data-testid="backtest-diagnostic"]').text()).toContain('数据不足')
    Object.defineProperty(URL, 'createObjectURL', { value: vi.fn().mockReturnValue('blob:backtest'), configurable: true })
    Object.defineProperty(URL, 'revokeObjectURL', { value: vi.fn(), configurable: true })
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined)
    await wrapper.get('[data-testid="export-backtest"]').trigger('click')
    expect(clickSpy).toHaveBeenCalledTimes(1)
    clickSpy.mockRestore()
  })
})
