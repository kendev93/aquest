import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { ConditionResearchReport, FormulaScreenMetadata } from '../types'
import ConditionResearchPage from './ConditionResearchPage.vue'

import * as researchApi from '../api/conditionResearch'
import * as formulaApi from '../api/formulaScreen'
import * as jobsApi from '../api/jobs'

vi.mock('../api/formulaScreen', async () => {
  const actual = await vi.importActual<typeof import('../api/formulaScreen')>('../api/formulaScreen')
  return { ...actual, fetchMetadata: vi.fn(), parseFormula: vi.fn() }
})

vi.mock('../api/conditionResearch', async () => {
  const actual = await vi.importActual<typeof import('../api/conditionResearch')>('../api/conditionResearch')
  return {
    ...actual,
    createConditionResearch: vi.fn(),
    getConditionResearch: vi.fn(),
    cancelConditionResearch: vi.fn(),
    getConditionResearchResults: vi.fn(),
    fetchConditionResearchHistory: vi.fn(),
    getConditionResearchHistory: vi.fn(),
  }
})

vi.mock('../api/jobs', async () => {
  const actual = await vi.importActual<typeof import('../api/jobs')>('../api/jobs')
  return { ...actual, fetchActiveJobs: vi.fn() }
})

const metadata: FormulaScreenMetadata = {
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
    closed_trades: 40, wins: 28, losses: 12, win_rate: 0.7, payoff_ratio: 1.5,
    profit_factor: 2, mean_trade_return: 0.03, median_trade_return: 0.02, stocks_with_trades: 8,
  },
  per_stock: {
    stock_count: 8, median_win_rate: 0.68, median_payoff_ratio: 1.4,
    median_total_return: 0.3, median_annual_return: 0.06, median_max_drawdown: 0.1,
    median_sharpe: 1.2, compared_stock_count: 8, improved_stock_count: 6,
    improved_stock_ratio: 0.75,
  },
  stocks: [{
    market: 'SH', code: '600000', name: '浦发银行', closed_trades: 5, win_rate: 0.8,
    payoff_ratio: 1.5, profit_factor: 2, mean_trade_return: 0.04, median_trade_return: 0.03,
    total_return: 0.2, annual_return: 0.04, max_drawdown: 0.08, sharpe: 1.3,
  }],
}

const result = {
  universe: 'all',
  start_date: '2020-01-01',
  end_date: '2024-12-31',
  search_sides: 'both',
  feature_count: 12,
  min_trades: 20,
  min_instruments: 5,
  sample_totals: {
    buy: { count: 40, unit: 'entry' },
    sell: { count: 200, unit: 'decision_point' },
  },
  disclosure: {
    universe_scope: 'current_listed_only',
    universe: 'all',
    survivorship_bias: true,
    requested_instruments: 100,
    processed_instruments: 98,
    coverage_ratio: 0.98,
    notes: [
      '标的范围取自当前行情库中的在市标的，无法还原历史时点成分或退市标的，结果存在幸存者偏差，不应视为完整历史全市场。',
      '本次仅 98/100 只标的进入统计，未覆盖标的未参与任何汇总。',
    ],
  },
  total_candidates: 100,
  processed: 100,
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
  }, {
    candidate_id: 'buy-2',
    side: 'buy',
    condition: { left_value: 'indicator.score', operator: 'gte', right_value: null, right_constant: 9.5 },
    mining: { support_count: 2, support_ratio: 0.01, instrument_count: 2, outcome_rate: 0.9, baseline_rate: 0.6, lift: 1.5 },
    validation: summary,
    passed_constraints: false,
    constraint_failures: ['交易样本不足', '胜率未高于基础规则'],
    full_period: null,
    holdout: null,
  }, {
    candidate_id: 'sell-1',
    side: 'sell',
    condition: { left_value: 'indicator.score', operator: 'lte', right_value: null, right_constant: -1.5 },
    mining: { support_count: 50, support_ratio: 0.25, instrument_count: 9, outcome_rate: 0.7, baseline_rate: 0.5, lift: 1.4 },
    validation: summary,
    passed_constraints: true,
    constraint_failures: [],
    full_period: summary,
    holdout: summary,
  }],
  algorithm: { mining: 'contrastive_threshold_rules', search: 'single_condition_then_full_stock_replay' },
} as unknown as ConditionResearchReport

describe('ConditionResearchPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(formulaApi.fetchMetadata).mockResolvedValue(metadata)
    vi.mocked(researchApi.createConditionResearch).mockResolvedValue({ job_id: 'research-1', status: 'queued' })
    vi.mocked(researchApi.getConditionResearch).mockResolvedValue({
      job_id: 'research-1', status: 'completed', progress: 1, total_candidates: 100,
      total_scanned: 100, errors: 0, skipped: 0, error: null, result: null,
    })
    vi.mocked(researchApi.getConditionResearchResults).mockResolvedValue(result)
    vi.mocked(researchApi.fetchConditionResearchHistory).mockResolvedValue([])
    vi.mocked(jobsApi.fetchActiveJobs).mockResolvedValue([])
  })

  it('submits fixed buy/sell rules and renders validation and holdout candidates', async () => {
    const wrapper = mount(ConditionResearchPage)
    await flushPromises()

    expect(wrapper.text()).toContain('买卖条件研究')
    expect(wrapper.text()).toContain('不做组合')
    await wrapper.get('[data-testid="condition-board-star"]').setValue(true)
    await wrapper.get('[data-testid="condition-research-config"]').trigger('submit')
    await flushPromises()

    expect(researchApi.createConditionResearch).toHaveBeenCalledWith(expect.objectContaining({
      universe: 'all',
      boards: ['star'],
      search_sides: 'both',
      date_mode: 'recent5',
      min_trades: 20,
      buy_rule: expect.objectContaining({
        signals: [],
        conditions: [expect.objectContaining({ left_value: 'indicator.score', operator: 'gt' })],
      }),
      sell_rule: expect.objectContaining({
        signals: [],
        conditions: [expect.objectContaining({ left_value: 'indicator.score', operator: 'lte' })],
      }),
    }))
    // 该 fixture 没有零轴输出，页面必须提示默认条件只是互补占位。
    expect(wrapper.find('[data-testid="condition-sell-default-hint"]').exists()).toBe(true)
    expect(wrapper.get('[data-testid="condition-research-results"]').text()).toContain('70.00%')
    // 结果页必须披露数据覆盖与幸存者偏差限制。
    const notice = wrapper.get('[data-testid="condition-survivorship-notice"]').text()
    expect(notice).toContain('幸存者偏差')
    expect(notice).toContain('98')
    expect(notice).toContain('100')
    expect(wrapper.get('[data-testid="condition-research-candidate-buy-1"]').text()).toContain('强度')
    expect(wrapper.get('[data-testid="condition-research-candidate-buy-1"]').text()).toContain('100.000%')
    expect(wrapper.get('[data-testid="condition-research-candidate-buy-1"]').text()).toContain('40 / 20')
    const rejected = wrapper.get('[data-testid="condition-research-candidate-buy-2"]').text()
    expect(rejected).toContain('5.000%')
    expect(rejected).toContain('交易样本不足')
    expect(rejected).toContain('胜率未高于基础规则')
    expect(rejected).toContain('未进留出期')
    expect((wrapper.get('[data-testid="condition-stock-view"]').element as HTMLSelectElement).value).toBe('baseline:full')
    expect(wrapper.get('[data-testid="condition-research-results"]').text()).toContain('浦发银行')
    expect(wrapper.text()).toContain('留出期')
    await wrapper.get('[data-testid="condition-stock-filter"]').setValue('浦发')
    expect(wrapper.get('[data-testid="condition-research-results"]').text()).toContain('筛选出 1 / 1')
    await wrapper.get('[data-testid="show-condition-stock-buy-1"]').trigger('click')
    expect((wrapper.get('[data-testid="condition-stock-view"]').element as HTMLSelectElement).value).toBe('buy-1:full')
  })

  it('defaults to a zero-centred operand so the sell condition can actually fire', async () => {
    vi.mocked(formulaApi.fetchMetadata).mockResolvedValueOnce({
      ...metadata,
      indicators: [
        ...metadata.indicators,
        {
          id: 'technical',
          display_name: '技术指标',
          minimum_bars: 1,
          recommended_bars: 20,
          signals: [],
          values: [
            {
              id: 'technical.macd_histogram',
              display_name: 'MACD 柱',
              description: 'MACD 柱数值输出',
              bidirectional: true,
            },
          ],
        },
      ],
    })
    const wrapper = mount(ConditionResearchPage)
    await flushPromises()
    await wrapper.get('[data-testid="condition-research-config"]').trigger('submit')
    await flushPromises()

    expect(researchApi.createConditionResearch).toHaveBeenCalledWith(expect.objectContaining({
      buy_rule: expect.objectContaining({
        conditions: [expect.objectContaining({ left_value: 'technical.macd_histogram', operator: 'gt' })],
      }),
      sell_rule: expect.objectContaining({
        conditions: [expect.objectContaining({ left_value: 'technical.macd_histogram', operator: 'lt' })],
      }),
    }))
    expect(wrapper.find('[data-testid="condition-sell-default-hint"]').exists()).toBe(false)
  })

  it('uses opposite operators for buy/sell after parsing a custom formula', async () => {
    // 回归：解析自定义公式后若把运算符 'gt'/'lt' 当成 'buy'/'sell' 传入 initialRule，
    // 两侧都会落进 else 分支拿到同一个 'lte'，买卖条件完全同构，研究结论失真。
    vi.mocked(formulaApi.parseFormula).mockResolvedValueOnce({
      parameters: [],
      signals: [],
      values: [{ id: 'custom.score', display_name: '强度', description: '自定义数值', bidirectional: true }],
      minimum_bars: 20,
      warnings: [],
    })

    const wrapper = mount(ConditionResearchPage)
    await flushPromises()
    await wrapper.get('[data-testid="condition-mode-custom"]').trigger('click')
    await wrapper.get('[data-testid="condition-formula"]').setValue('强度:C/MA(C,20);')
    await wrapper.get('[data-testid="condition-parse-formula"]').trigger('click')
    await flushPromises()
    await wrapper.get('[data-testid="condition-research-config"]').trigger('submit')
    await flushPromises()

    expect(researchApi.createConditionResearch).toHaveBeenCalledWith(expect.objectContaining({
      buy_rule: expect.objectContaining({
        conditions: [expect.objectContaining({ left_value: 'custom.score', operator: 'gt' })],
      }),
      sell_rule: expect.objectContaining({
        conditions: [expect.objectContaining({ left_value: 'custom.score', operator: 'lt' })],
      }),
    }))
  })

  it('scores a sell candidate against early-exit decision points, not closed trades', async () => {
    // 卖出候选的 50 个命中来自持仓期决策点（分母 200），若错用成交笔数 40
    // 会显示成 125.000%，即超过 100% 的“命中率”。
    vi.mocked(researchApi.getConditionResearchResults).mockResolvedValueOnce({
      ...result,
      candidates: [{
        candidate_id: 'sell-1',
        side: 'sell',
        condition: { left_value: 'indicator.score', operator: 'lte', right_value: null, right_constant: 2.5 },
        mining: {
          support_count: 50, support_ratio: 0.25, instrument_count: 10,
          outcome_rate: 0.8, baseline_rate: 0.6, lift: 1.33,
        },
        validation: summary,
        passed_constraints: true,
        constraint_failures: [],
        full_period: summary,
        holdout: summary,
      }],
    } as unknown as ConditionResearchReport)

    const wrapper = mount(ConditionResearchPage)
    await flushPromises()
    await wrapper.get('[data-testid="condition-research-config"]').trigger('submit')
    await flushPromises()

    const sellRow = wrapper.get('[data-testid="condition-research-candidate-sell-1"]')
    expect(sellRow.text()).toContain('25.000%')
    expect(sellRow.text()).not.toContain('125.000%')
  })

  it('switches to an ETF study with fund costs and no board filters', async () => {
    const wrapper = mount(ConditionResearchPage)
    await flushPromises()

    await wrapper.get('[data-testid="condition-advanced-toggle"]').trigger('click')
    await wrapper.get('[data-testid="condition-board-star"]').setValue(true)
    await wrapper.get('[data-testid="condition-instrument-type"]').setValue('fund')
    await flushPromises()

    // 板块是股票概念，ETF 场景下必须隐藏，残留的板块选择也要一并丢弃。
    expect(wrapper.find('[data-testid="condition-board-scope"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('沪深全部ETF')
    expect(wrapper.text()).toContain('ETF 免印花税')
    expect((wrapper.get('[data-testid="condition-commission"]').element as HTMLInputElement).value).toBe('0.00005')
    expect((wrapper.get('[data-testid="condition-stamp-tax"]').element as HTMLInputElement).value).toBe('0')

    await wrapper.get('[data-testid="condition-research-config"]').trigger('submit')
    await flushPromises()

    expect(researchApi.createConditionResearch).toHaveBeenCalledWith(expect.objectContaining({
      instrument_types: ['fund'],
      commission: 0.00005,
      stamp_tax: 0,
    }))
    expect(vi.mocked(researchApi.createConditionResearch).mock.calls[0][0].boards).toBeUndefined()

    await wrapper.get('[data-testid="condition-instrument-type"]').setValue('stock')
    await flushPromises()
    expect(wrapper.find('[data-testid="condition-board-scope"]').exists()).toBe(true)
    expect((wrapper.get('[data-testid="condition-commission"]').element as HTMLInputElement).value).toBe('0.0001')
  })

  it('keeps costs the user customised when switching instrument type', async () => {
    const wrapper = mount(ConditionResearchPage)
    await flushPromises()

    await wrapper.get('[data-testid="condition-advanced-toggle"]').trigger('click')
    await wrapper.get('[data-testid="condition-commission"]').setValue(0.0008)
    await wrapper.get('[data-testid="condition-instrument-type"]').setValue('fund')
    await flushPromises()

    expect((wrapper.get('[data-testid="condition-commission"]').element as HTMLInputElement).value).toBe('0.0008')
    expect((wrapper.get('[data-testid="condition-stamp-tax"]').element as HTMLInputElement).value).toBe('0')
  })

  it('resumes an active research job after a page refresh', async () => {
    vi.mocked(jobsApi.fetchActiveJobs).mockResolvedValueOnce([{
      job_id: 'research-running',
      description: '买卖条件研究',
      status: 'running',
      progress: 0.4,
      total_candidates: 100,
      total_scanned: 40,
      errors: 0,
      skipped: 0,
      error: null,
      result: null,
    }])
    vi.mocked(researchApi.getConditionResearch).mockResolvedValueOnce({
      job_id: 'research-running',
      status: 'completed',
      progress: 1,
      total_candidates: 100,
      total_scanned: 100,
      errors: 0,
      skipped: 0,
      error: null,
      result: null,
    })

    const wrapper = mount(ConditionResearchPage)
    await flushPromises()

    expect(researchApi.getConditionResearch).toHaveBeenCalledWith('research-running')
    expect(wrapper.get('[data-testid="condition-research-results"]').text()).toContain('2.500')
  })

  it('appends a selected candidate to its own side and reruns the study', async () => {
    const wrapper = mount(ConditionResearchPage)
    await flushPromises()
    await wrapper.get('[data-testid="condition-research-config"]').trigger('submit')
    await flushPromises()
    expect(researchApi.createConditionResearch).toHaveBeenCalledTimes(1)

    await wrapper.get('[data-testid="select-condition-buy-1"]').setValue(true)
    await wrapper.get('[data-testid="select-condition-sell-1"]').setValue(true)
    await wrapper.get('[data-testid="apply-selected-conditions"]').trigger('click')
    await flushPromises()

    expect(researchApi.createConditionResearch).toHaveBeenCalledTimes(2)
    const payload = vi.mocked(researchApi.createConditionResearch).mock.calls[1][0]
    // 买入候选只进买入规则，卖出候选只进卖出规则，两侧互不串台。
    expect(payload.buy_rule.conditions).toEqual([
      expect.objectContaining({ left_value: 'indicator.score', operator: 'gt' }),
      { left_value: 'indicator.score', operator: 'gte', right_value: null, right_constant: 2.5 },
    ])
    expect(payload.sell_rule.conditions).toEqual([
      expect.objectContaining({ left_value: 'indicator.score', operator: 'lte' }),
      { left_value: 'indicator.score', operator: 'lte', right_value: null, right_constant: -1.5 },
    ])
    // 勾选状态在重跑后作废，避免残留指向旧结果的 id。
    expect((wrapper.get('[data-testid="select-condition-buy-1"]').element as HTMLInputElement).checked).toBe(false)
  })

  it('adds a candidate only once when it is applied twice', async () => {
    const wrapper = mount(ConditionResearchPage)
    await flushPromises()
    await wrapper.get('[data-testid="condition-research-config"]').trigger('submit')
    await flushPromises()

    await wrapper.get('[data-testid="select-condition-buy-1"]').setValue(true)
    await wrapper.get('[data-testid="apply-selected-conditions"]').trigger('click')
    await flushPromises()
    expect(researchApi.createConditionResearch).toHaveBeenCalledTimes(2)

    // 同一条候选再点一次：不该重复塞进规则，也不该白跑一次研究。
    await wrapper.get('[data-testid="select-condition-buy-1"]').setValue(true)
    await wrapper.get('[data-testid="apply-selected-conditions"]').trigger('click')
    await flushPromises()
    expect(researchApi.createConditionResearch).toHaveBeenCalledTimes(2)
    expect(wrapper.get('[data-testid="condition-research-message"]').text()).toContain('已经在基础规则里')
  })

  it('restores the saved configuration when loading a history record', async () => {
    vi.mocked(researchApi.fetchConditionResearchHistory).mockResolvedValueOnce([
      {
        history_id: 'hist-1',
        backtest_type: 'condition_research',
        created_at: '2024-05-01T09:00:00',
        started_at: '2024-05-01T09:00:00',
        finished_at: '2024-05-01T09:02:00',
        duration_seconds: 120,
        summary: {},
      },
    ])
    vi.mocked(researchApi.getConditionResearchHistory).mockResolvedValueOnce({
      history_id: 'hist-1',
      created_at: '2024-05-01T09:00:00',
      started_at: '2024-05-01T09:00:00',
      finished_at: '2024-05-01T09:02:00',
      duration_seconds: 120,
      summary: {},
      backtest_type: 'condition_research',
      request: {
        universe: 'sz',
        instrument_types: ['stock'],
        boards: ['chinext'],
        buy_rule: {
          signals: [],
          conditions: [
            { left_value: 'indicator.score', operator: 'gte', right_value: null, right_constant: 3 },
          ],
          required_conditions: [],
          combine_mode: 'all',
          minimum_matches: null,
        },
        sell_rule: {
          signals: [],
          conditions: [
            { left_value: 'indicator.score', operator: 'lte', right_value: null, right_constant: -2 },
          ],
          required_conditions: [],
          combine_mode: 'all',
          minimum_matches: null,
        },
        // 后端保存的是解析后的区间，不是用户选的模式。
        date_mode: 'custom',
        start_date: '2021-03-01',
        end_date: '2024-09-30',
        search_sides: 'buy',
        min_trades: 33,
        min_instruments: 7,
        candidate_limit: 4,
        holdout_candidates: 2,
        initial_cash: 250_000,
        commission: 0.0002,
        min_commission: 1,
        stamp_tax: 0.0005,
        slippage: 0.02,
        execution: 'next_close',
        formula_text: null,
        formula_parameters: {},
      },
      result,
    })

    const wrapper = mount(ConditionResearchPage)
    await flushPromises()
    await wrapper.get('[data-testid="load-condition-history"]').trigger('click')
    await flushPromises()

    expect(wrapper.get('[data-testid="condition-research-results"]').text()).toContain('浦发银行')
    expect((wrapper.get('[data-testid="condition-universe"]').element as HTMLSelectElement).value).toBe('sz')
    expect((wrapper.get('[data-testid="condition-board-chinext"]').element as HTMLInputElement).checked).toBe(true)
    expect((wrapper.get('[data-testid="condition-search-sides"]').element as HTMLSelectElement).value).toBe('buy')
    expect((wrapper.get('[data-testid="condition-min-trades"]').element as HTMLInputElement).value).toBe('33')
    expect((wrapper.get('[data-testid="condition-min-instruments"]').element as HTMLInputElement).value).toBe('7')
    expect((wrapper.get('[data-testid="condition-date-mode"]').element as HTMLSelectElement).value).toBe('custom')
    expect((wrapper.get('[data-testid="condition-start"]').element as HTMLInputElement).value).toBe('2021-03-01')
    expect((wrapper.get('[data-testid="condition-end"]').element as HTMLInputElement).value).toBe('2024-09-30')

    // 还原后的配置要能直接再次提交。
    await wrapper.get('[data-testid="condition-research-config"]').trigger('submit')
    await flushPromises()
    expect(researchApi.createConditionResearch).toHaveBeenCalledWith(expect.objectContaining({
      universe: 'sz',
      boards: ['chinext'],
      search_sides: 'buy',
      min_trades: 33,
      min_instruments: 7,
      candidate_limit: 4,
      holdout_candidates: 2,
      buy_rule: expect.objectContaining({
        conditions: [expect.objectContaining({ operator: 'gte', right_constant: 3 })],
      }),
      sell_rule: expect.objectContaining({
        conditions: [expect.objectContaining({ operator: 'lte', right_constant: -2 })],
      }),
    }))
  })

  it('restores an ETF study without re-introducing stock board filters', async () => {
    vi.mocked(researchApi.fetchConditionResearchHistory).mockResolvedValueOnce([
      {
        history_id: 'hist-etf',
        backtest_type: 'condition_research',
        created_at: '2024-06-01T09:00:00',
        started_at: '2024-06-01T09:00:00',
        finished_at: '2024-06-01T09:01:00',
        duration_seconds: 60,
        summary: {},
      },
    ])
    vi.mocked(researchApi.getConditionResearchHistory).mockResolvedValueOnce({
      history_id: 'hist-etf',
      created_at: '2024-06-01T09:00:00',
      started_at: '2024-06-01T09:00:00',
      finished_at: '2024-06-01T09:01:00',
      duration_seconds: 60,
      summary: {},
      backtest_type: 'condition_research',
      request: {
        universe: 'all',
        instrument_types: ['fund'],
        boards: [],
        buy_rule: { signals: [], conditions: [], required_conditions: [], combine_mode: 'all', minimum_matches: null },
        sell_rule: { signals: [], conditions: [], required_conditions: [], combine_mode: 'all', minimum_matches: null },
        date_mode: 'custom',
        start_date: '2022-01-01',
        end_date: '2024-12-31',
        search_sides: 'both',
        commission: 0.00005,
        stamp_tax: 0,
        min_commission: 0.1,
        slippage: 0,
        initial_cash: 100_000,
        execution: 'next_open',
        formula_text: null,
        formula_parameters: {},
      },
      result,
    })

    const wrapper = mount(ConditionResearchPage)
    await flushPromises()
    await wrapper.get('[data-testid="load-condition-history"]').trigger('click')
    await flushPromises()

    expect((wrapper.get('[data-testid="condition-instrument-type"]').element as HTMLSelectElement).value).toBe('fund')
    // 选 ETF 时板块整块隐藏，历史里的板块过滤也不该回来。
    expect(wrapper.find('[data-testid="condition-board-scope"]').exists()).toBe(false)
    await wrapper.get('[data-testid="condition-advanced-toggle"]').trigger('click')
    expect((wrapper.get('[data-testid="condition-commission"]').element as HTMLInputElement).value).toBe('0.00005')
    expect((wrapper.get('[data-testid="condition-stamp-tax"]').element as HTMLInputElement).value).toBe('0')
  })
})
