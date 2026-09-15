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
      buy_rule: expect.objectContaining({ signals: ['indicator.buy'] }),
      sell_rule: expect.objectContaining({ signals: ['indicator.sell'] }),
    }))
    expect(wrapper.get('[data-testid="condition-research-results"]').text()).toContain('70.00%')
    expect(wrapper.get('[data-testid="condition-research-candidate-buy-1"]').text()).toContain('强度')
    expect((wrapper.get('[data-testid="condition-stock-view"]').element as HTMLSelectElement).value).toBe('baseline:full')
    expect(wrapper.get('[data-testid="condition-research-results"]').text()).toContain('浦发银行')
    expect(wrapper.text()).toContain('留出期')
    await wrapper.get('[data-testid="condition-stock-filter"]').setValue('浦发')
    expect(wrapper.get('[data-testid="condition-research-results"]').text()).toContain('筛选出 1 / 1')
    await wrapper.get('[data-testid="show-condition-stock-buy-1"]').trigger('click')
    expect((wrapper.get('[data-testid="condition-stock-view"]').element as HTMLSelectElement).value).toBe('buy-1:full')
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
})
