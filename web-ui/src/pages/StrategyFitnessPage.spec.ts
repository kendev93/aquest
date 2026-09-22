import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { FormulaScreenMetadata, StrategyFitnessJobState, StrategyFitnessReport } from '../types'
import StrategyFitnessPage from './StrategyFitnessPage.vue'

import * as fitnessApi from '../api/strategyFitness'
import * as formulaApi from '../api/formulaScreen'
import * as jobsApi from '../api/jobs'
import { FormulaScreenApiError } from '../api/formulaScreen'

vi.mock('../api/formulaScreen', async () => {
  const actual = await vi.importActual<typeof import('../api/formulaScreen')>('../api/formulaScreen')
  return { ...actual, fetchMetadata: vi.fn(), parseFormula: vi.fn() }
})

vi.mock('../api/strategyFitness', async () => {
  const actual = await vi.importActual<typeof import('../api/strategyFitness')>('../api/strategyFitness')
  return {
    ...actual,
    createStrategyFitness: vi.fn(),
    getStrategyFitness: vi.fn(),
    getStrategyFitnessResults: vi.fn(),
    fetchStrategyFitnessHistory: vi.fn(),
    getStrategyFitnessHistory: vi.fn(),
    deleteStrategyFitnessHistory: vi.fn(),
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
      { id: 'indicator_three.varo7', display_name: 'VARO7', description: '趋势值' },
      { id: 'indicator_three.varo6', display_name: 'VARO6', description: '辅助值' },
      { id: 'indicator_three.var1', display_name: 'VAR1', description: '变量' },
    ],
  }],
  combine_modes: [{ value: 'all', label: '全部满足' }, { value: 'any', label: '任一满足' }, { value: 'at_least', label: '至少满足' }],
  supported_markets: ['SH', 'SZ'],
  supported_universe: [{ value: 'all', label: '沪深 A 股' }, { value: 'custom', label: '自定义列表' }],
  periods: [{ value: 'daily', label: '日线' }],
  data_directory_help: '本地目录',
  default_vipdoc_path: '/data/vipdoc',
}

const phase = (name: 'train' | 'validation' | 'test', start: string, end: string, trades: number, totalReturn: number) => ({
  name, start_date: start, end_date: end, bars: 20, total_trades: trades, win_rate: 0.6,
  total_return: totalReturn, annual_return: totalReturn, max_drawdown: 0.1, sharpe: 1.2,
  profit_factor: 1.4, expectancy: 0.02, avg_holding_days: 4, diagnostic: null,
})

const report: StrategyFitnessReport = {
  universe: 'all', total_candidates: 2, processed: 2, skipped: 0, errors: 0, bars: 60,
  start_date: '2020-01-01', end_date: '2024-12-31', train_end_date: '2022-12-31', validation_end_date: '2023-12-31',
  ranking_value: 'indicator_three.varo7', train_ratio: 0.6, validation_ratio: 0.2, min_trades: 5, max_test_drawdown: 0.3,
  results: [{
    market: 'SH', code: '600000', name: '浦发银行', bars: 60, data_start: '2020-01-01', data_end: '2024-12-31', suitability_score: 87.5,
    passed: true, label: 'strong', passed_checks: 7, total_checks: 8, positive_periods: 3,
    checks: [{ id: 'test_return', label: '测试期总收益为正', passed: true }],
    train: phase('train', '2020-01-01', '2022-12-31', 30, 0.42),
    validation: phase('validation', '2023-01-01', '2023-12-31', 9, 0.12),
    test: phase('test', '2024-01-01', '2024-12-31', 8, 0.08),
    failure_reason: null,
  }, {
    market: 'SZ', code: '000001', name: '平安银行', bars: 60, data_start: '2020-01-01', data_end: '2024-12-31', suitability_score: 25,
    passed: false, label: 'insufficient', passed_checks: 2, total_checks: 8, positive_periods: 1, checks: [],
    train: phase('train', '2020-01-01', '2022-12-31', 12, 0.02),
    validation: phase('validation', '2023-01-01', '2023-12-31', 1, -0.03),
    test: phase('test', '2024-01-01', '2024-12-31', 1, 0),
    failure_reason: null,
  }],
  failure_reasons: {}, diagnostic: null,
}

describe('StrategyFitnessPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(formulaApi.fetchMetadata).mockResolvedValue(metadata)
    vi.mocked(fitnessApi.createStrategyFitness).mockResolvedValue({ job_id: 'fitness-1', status: 'queued' })
    vi.mocked(fitnessApi.getStrategyFitness).mockResolvedValue({
      job_id: 'fitness-1', status: 'completed', progress: 1, total_candidates: 2, total_scanned: 2,
      errors: 0, error: null, result: null,
    })
    vi.mocked(fitnessApi.getStrategyFitnessResults).mockResolvedValue(report)
    vi.mocked(fitnessApi.fetchStrategyFitnessHistory).mockResolvedValue([])
    vi.mocked(jobsApi.fetchActiveJobs).mockResolvedValue([])
  })

  it('recovers and resumes a running strategy fitness task after the page is refreshed', async () => {
    vi.mocked(jobsApi.fetchActiveJobs).mockResolvedValueOnce([{
      job_id: 'fitness-refresh', description: '策略适配性评估', status: 'running', progress: 0.42,
      total_candidates: 2, total_scanned: 1, errors: 0, error: null, result: null,
    }])
    let resolveStatus!: (state: StrategyFitnessJobState) => void
    vi.mocked(fitnessApi.getStrategyFitness).mockImplementationOnce(() => new Promise((resolve) => {
      resolveStatus = resolve
    }))

    const wrapper = mount(StrategyFitnessPage)
    await flushPromises()

    expect(jobsApi.fetchActiveJobs).toHaveBeenCalled()
    expect(fitnessApi.getStrategyFitness).toHaveBeenCalledWith('fitness-refresh')
    expect(wrapper.get('[data-testid="fitness-progress"]').text()).toContain('42%')

    resolveStatus({
      job_id: 'fitness-refresh', status: 'completed', progress: 1, total_candidates: 2, total_scanned: 2,
      errors: 0, error: null, result: null,
    })
    await flushPromises()
    expect(wrapper.get('[data-testid="fitness-results"]').text()).toContain('87.50')
  })

  it('submits the strategy and renders compatibility scores with three phases', async () => {
    const wrapper = mount(StrategyFitnessPage)
    await flushPromises()
    expect((wrapper.get('[data-testid="fitness-date-mode"]').element as HTMLSelectElement).value).toBe('recent5')
    expect(wrapper.text()).toContain('默认使用本地最新已完成日线向前 5 年')
    await wrapper.get('[data-testid="fitness-config"]').trigger('submit')
    await flushPromises()

    expect(fitnessApi.createStrategyFitness).toHaveBeenCalledWith(expect.objectContaining({
      selected_signals: [],
      buy_rule: expect.objectContaining({
        signals: [],
        conditions: [expect.objectContaining({ left_value: 'indicator_three.var1', operator: 'cross_up', right_constant: 8 })],
      }),
      ranking_value: 'indicator_three.varo7', train_ratio: 0.6, validation_ratio: 0.2,
      min_trades: 5, max_test_drawdown: 0.3,
      date_mode: 'recent5',
      start_date: null, end_date: null,
    }))
    expect(wrapper.get('[data-testid="fitness-results"]').text()).toContain('600000')
    expect(wrapper.get('[data-testid="fitness-results"]').text()).toContain('浦发银行')
    expect(wrapper.get('[data-testid="fitness-results"]').text()).toContain('87.50')
    expect(wrapper.get('[data-testid="fitness-train-600000"]').text()).toContain('42.00%')
    expect(wrapper.get('[data-testid="fitness-validation-600000"]').text()).toContain('12.00%')
    expect(wrapper.get('[data-testid="fitness-test-600000"]').text()).toContain('8.00%')
  })

  it('parses a custom formula and configures a metric comparison rule', async () => {
    vi.mocked(formulaApi.parseFormula).mockResolvedValueOnce({
      parameters: [{ name: 'N', default: 5, minimum: 1, maximum: 100, step: 1 }],
      signals: [{ id: 'custom.buy', display_name: '买入', description: '买入' }, { id: 'custom.sell', display_name: '卖出', description: '卖出' }],
      values: [
        { id: 'custom.rank', display_name: '排序', description: '排序' },
        { id: 'custom.buy', display_name: '买入值', description: '买入数值' },
        { id: 'custom.sell', display_name: '卖出值', description: '卖出数值' },
      ],
      minimum_bars: 6, warnings: [],
    })
    const wrapper = mount(StrategyFitnessPage)
    await flushPromises()
    await wrapper.get('[data-testid="fitness-mode-custom"]').trigger('click')
    await wrapper.get('[data-testid="fitness-formula"]').setValue('N:=5; 买入:C>0; 卖出:C<0; 排序:C;')
    await wrapper.get('[data-testid="fitness-parse-formula"]').trigger('click')
    await flushPromises()
    expect((wrapper.get('[data-testid="fitness-ranking-value"]').element as HTMLSelectElement).value).toBe('custom.rank')
    await wrapper.get('#fitness-sell-rule-operator-0').setValue('gte')
    await wrapper.get('#fitness-sell-rule-right-type-0').setValue('constant')
    await wrapper.get('#fitness-sell-rule-right-0').setValue(20)
    await wrapper.get('[data-testid="fitness-config"]').trigger('submit')
    await flushPromises()
    expect(fitnessApi.createStrategyFitness).toHaveBeenCalledWith(expect.objectContaining({
      sell_rule: expect.objectContaining({
        conditions: [expect.objectContaining({ right_constant: 20, right_value: null })],
      }),
    }))
  })

  it('shows a metadata loading error from the API', async () => {
    vi.mocked(formulaApi.fetchMetadata).mockRejectedValueOnce(new FormulaScreenApiError('元数据服务不可用', 503))
    const wrapper = mount(StrategyFitnessPage)
    await flushPromises()

    expect(wrapper.get('[data-testid="fitness-message"]').text()).toContain('元数据服务不可用')
  })

  it('handles empty and failed custom formula parsing before returning to presets', async () => {
    const wrapper = mount(StrategyFitnessPage)
    await flushPromises()
    await wrapper.get('[data-testid="fitness-mode-custom"]').trigger('click')

    await wrapper.get('[data-testid="fitness-parse-formula"]').trigger('click')
    expect(wrapper.text()).toContain('请输入通达信公式后再解析')

    vi.mocked(formulaApi.parseFormula).mockRejectedValueOnce(new Error('unsupported formula'))
    await wrapper.get('[data-testid="fitness-formula"]').setValue('BAD:UNKNOWN(C);')
    await wrapper.get('[data-testid="fitness-parse-formula"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('公式解析失败，请检查语法和函数是否受支持')

    vi.mocked(formulaApi.parseFormula).mockRejectedValueOnce(new FormulaScreenApiError('公式被拒绝', 422))
    await wrapper.get('[data-testid="fitness-formula"]').setValue('BAD:UNKNOWN(D);')
    await wrapper.get('[data-testid="fitness-parse-formula"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('公式被拒绝')

    await wrapper.get('[data-testid="fitness-mode-preset"]').trigger('click')
    expect((wrapper.get('[data-testid="fitness-ranking-value"]').element as HTMLSelectElement).value).toBe('indicator_three.varo7')
  })

  it('reports failed assessment jobs and request errors', async () => {
    vi.mocked(fitnessApi.getStrategyFitness).mockResolvedValueOnce({
      job_id: 'fitness-1', status: 'failed', progress: 1, total_candidates: 2, total_scanned: 0,
      errors: 1, error: null, result: null,
    })
    const wrapper = mount(StrategyFitnessPage)
    await flushPromises()
    await wrapper.get('[data-testid="start-fitness"]').trigger('click')
    await flushPromises()

    expect(wrapper.get('[data-testid="fitness-message"]').text()).toContain('策略适配性评估任务失败')

    vi.mocked(fitnessApi.createStrategyFitness).mockRejectedValueOnce(new FormulaScreenApiError('服务拒绝任务', 422))
    await wrapper.get('[data-testid="start-fitness"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-testid="fitness-message"]').text()).toContain('服务拒绝任务')
  })

  it('renders an empty report, failure summary, and export action', async () => {
    const emptyReport: StrategyFitnessReport = {
      ...report,
      total_candidates: 1,
      processed: 0,
      skipped: 1,
      errors: 1,
      results: [],
      failure_reasons: { 数据不足: 1 },
      diagnostic: '没有股票完成三段评估',
    }
    vi.mocked(fitnessApi.getStrategyFitnessResults).mockResolvedValueOnce(emptyReport)
    const wrapper = mount(StrategyFitnessPage)
    await flushPromises()
    await wrapper.get('[data-testid="start-fitness"]').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('没有股票完成三段适配性评估')
    expect(wrapper.text()).toContain('数据不足')
    expect(wrapper.get('[data-testid="fitness-diagnostic"]').text()).toContain('没有股票完成三段评估')

    Object.defineProperty(URL, 'createObjectURL', { value: vi.fn().mockReturnValue('blob:fitness'), configurable: true })
    Object.defineProperty(URL, 'revokeObjectURL', { value: vi.fn(), configurable: true })
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined)
    await wrapper.get('[data-testid="export-fitness"]').trigger('click')
    expect(clickSpy).toHaveBeenCalledTimes(1)
    clickSpy.mockRestore()
  })

  it('validates split, data, trade, and sell-rule inputs', async () => {
    const wrapper = mount(StrategyFitnessPage)
    await flushPromises()
    await wrapper.get('[data-testid="fitness-universe"]').setValue('custom')
    await wrapper.get('[data-testid="fitness-ranking-value"]').setValue('')
    await wrapper.get('[data-testid="fitness-date-mode"]').setValue('custom')
    await wrapper.get('[data-testid="fitness-start"]').setValue('2024-02-01')
    await wrapper.get('[data-testid="fitness-end"]').setValue('2024-01-01')
    await wrapper.get('[data-testid="fitness-train-ratio"]').setValue(0)
    await wrapper.get('[data-testid="fitness-validation-ratio"]').setValue(0)
    await wrapper.get('[data-testid="fitness-min-trades"]').setValue(0)
    await wrapper.get('[data-testid="fitness-max-drawdown"]').setValue(101)
    await wrapper.get('[data-testid="fitness-stop-loss"]').setValue(0)
    await wrapper.get('[data-testid="fitness-advanced-toggle"]').trigger('click')
    await wrapper.get('[data-testid="fitness-initial-cash"]').setValue(0)
    await wrapper.get('[data-testid="fitness-config"]').trigger('submit')

    expect(wrapper.text()).toContain('请输入自定义股票列表文件')
    expect(wrapper.text()).toContain('请选择用于记录策略上下文的指标输出')
    expect(wrapper.text()).toContain('训练比例必须大于 0')
    expect(wrapper.text()).toContain('验证比例必须大于 0')
    expect(wrapper.text()).toContain('最少成交笔数必须是正整数')
    expect(wrapper.text()).toContain('最大回撤阈值必须在 0 到 100% 之间')
    expect(wrapper.text()).toContain('止损比例必须大于 0')
    expect(wrapper.text()).toContain('初始资金必须大于 0')

    await wrapper.get('[data-testid="fitness-stop-loss-enabled"]').setValue(false)
    await wrapper.get('[data-testid="fitness-sell-rule-remove-0"]').trigger('click')
    await wrapper.get('[data-testid="fitness-config"]').trigger('submit')
    expect(wrapper.text()).toContain('至少配置一个卖出规则')
  })

  it('edits the buy rule, scope filters and advanced execution settings', async () => {
    const wrapper = mount(StrategyFitnessPage)
    await flushPromises()

    await wrapper.get('[data-testid="fitness-buy-rule-add-condition"]').trigger('click')
    await wrapper.get('[data-testid="fitness-scope-type-stock"]').setValue(true)
    await wrapper.get('[data-testid="fitness-scope-board-main"]').setValue(true)
    await wrapper.get('[data-testid="fitness-rank-order"]').setValue('asc')
    await wrapper.get('[data-testid="fitness-advanced-toggle"]').trigger('click')
    await wrapper.get('[data-testid="fitness-execution"]').setValue('next_close')
    await wrapper.get('[data-testid="fitness-commission"]').setValue(0.0005)
    await wrapper.get('[data-testid="fitness-min-commission"]').setValue(1)
    await wrapper.get('[data-testid="fitness-stamp-tax"]').setValue(0.001)
    await wrapper.get('[data-testid="fitness-slippage"]').setValue(0.02)
    await wrapper.get('[data-testid="fitness-initial-cash"]').setValue(200000)
    await wrapper.get('[data-testid="fitness-config"]').trigger('submit')
    await flushPromises()

    expect(fitnessApi.createStrategyFitness).toHaveBeenCalledWith(expect.objectContaining({
      instrument_types: ['stock'],
      boards: ['main'],
      rank_order: 'asc',
      execution: 'next_close',
      commission: 0.0005,
      min_commission: 1,
      stamp_tax: 0.001,
      slippage: 0.02,
      initial_cash: 200000,
    }))
  })

  it('restores the saved configuration when loading a fitness history record', async () => {
    vi.mocked(fitnessApi.fetchStrategyFitnessHistory).mockResolvedValue([{
      history_id: 'fitness-history-1', backtest_type: 'strategy_fitness', created_at: '2024-01-06T08:00:00Z',
      started_at: '2024-01-06T08:00:00Z', finished_at: '2024-01-06T08:01:00Z',
      duration_seconds: 60, summary: {},
    }])
    // 每个字段都故意偏离默认值，漏掉哪一个这条测试都会红。
    vi.mocked(fitnessApi.getStrategyFitnessHistory).mockResolvedValue({
      history_id: 'fitness-history-1', backtest_type: 'strategy_fitness', created_at: '2024-01-06T08:00:00Z',
      started_at: '2024-01-06T08:00:00Z', finished_at: '2024-01-06T08:01:00Z',
      duration_seconds: 60, summary: {}, result: report,
      request: {
        universe: 'sz',
        buy_rule: { signals: [], conditions: [], combine_mode: 'any', minimum_matches: null },
        sell_rule: { signals: [], conditions: [], combine_mode: 'any', minimum_matches: null },
        ranking_value: 'indicator_three.varo6',
        rank_order: 'asc',
        date_mode: 'custom',
        start_date: '2023-01-01',
        end_date: '2023-12-31',
        stop_loss_pct: 0.12,
        take_profit_pct: 0.25,
        train_ratio: 0.7,
        validation_ratio: 0.15,
        min_trades: 9,
        max_test_drawdown: 0.4,
        initial_cash: 500000,
        commission: 0.0005,
        min_commission: 1,
        stamp_tax: 0.001,
        slippage: 0.02,
        execution: 'next_close',
      },
    })
    vi.mocked(fitnessApi.deleteStrategyFitnessHistory).mockResolvedValue({ deleted: true })

    const wrapper = mount(StrategyFitnessPage)
    await flushPromises()
    await wrapper.get('[data-testid="fitness-history-load-fitness-history-1"]').trigger('click')
    await flushPromises()
    await wrapper.get('[data-testid="fitness-advanced-toggle"]').trigger('click')
    await flushPromises()

    expect(fitnessApi.getStrategyFitnessHistory).toHaveBeenCalledWith('fitness-history-1')
    const value = (testid: string): string =>
      (wrapper.get(`[data-testid="${testid}"]`).element as HTMLInputElement | HTMLSelectElement).value
    expect(value('fitness-universe')).toBe('sz')
    expect(value('fitness-ranking-value')).toBe('indicator_three.varo6')
    expect(value('fitness-rank-order')).toBe('asc')
    expect(value('fitness-date-mode')).toBe('custom')
    expect(value('fitness-start')).toBe('2023-01-01')
    expect(value('fitness-end')).toBe('2023-12-31')
    expect(value('fitness-stop-loss')).toBe('12')
    expect(value('fitness-take-profit')).toBe('25')
    expect(value('fitness-train-ratio')).toBe('70')
    expect(value('fitness-validation-ratio')).toBe('15')
    expect(value('fitness-min-trades')).toBe('9')
    expect(value('fitness-max-drawdown')).toBe('40')
    expect(value('fitness-initial-cash')).toBe('500000')
    expect(value('fitness-commission')).toBe('0.0005')
    expect(value('fitness-min-commission')).toBe('1')
    expect(value('fitness-stamp-tax')).toBe('0.001')
    expect(value('fitness-slippage')).toBe('0.02')
    expect(value('fitness-execution')).toBe('next_close')
    expect(wrapper.get('[data-testid="fitness-message"]').text()).toContain('已还原历史策略适配性评估')
    expect(wrapper.get('[data-testid="fitness-message"]').text()).toContain('请确认后重新运行')
  })
})
