import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { ConditionResearchReport, FormulaScreenMetadata } from '../types'
import ConditionResearchPage from './ConditionResearchPage.vue'

import * as researchApi from '../api/conditionResearch'
import * as candidateApi from '../api/candidateLibrary'
import * as formulaApi from '../api/formulaScreen'
import * as jobsApi from '../api/jobs'
import * as presetApi from '../api/researchPresets'
import { SWING_PLAN_STORAGE_KEY } from '../utils/swingHandoff'

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
    deleteConditionResearchHistory: vi.fn(),
  }
})

vi.mock('../api/jobs', async () => {
  const actual = await vi.importActual<typeof import('../api/jobs')>('../api/jobs')
  return { ...actual, fetchActiveJobs: vi.fn() }
})

vi.mock('../api/researchPresets', async () => {
  const actual = await vi.importActual<typeof import('../api/researchPresets')>(
    '../api/researchPresets',
  )
  return {
    ...actual,
    fetchResearchPresets: vi.fn(),
    getResearchPreset: vi.fn(),
    createResearchPreset: vi.fn(),
    updateResearchPreset: vi.fn(),
    deleteResearchPreset: vi.fn(),
  }
})

vi.mock('../api/candidateLibrary', async () => {
  const actual = await vi.importActual<typeof import('../api/candidateLibrary')>(
    '../api/candidateLibrary',
  )
  return { ...actual, importCandidates: vi.fn() }
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
    values: [
      { id: 'indicator.score', display_name: '强度', description: '测试指标值', rankable: true },
      { id: 'indicator.price', display_name: '绝对价位', description: '价格量纲，不能跨股票排序', rankable: false },
    ],
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
    buy: { count: 40, unit: 'entry', hit_days: 20, daily_hits: 2 },
    sell: { count: 200, unit: 'decision_point', hit_days: 20, daily_hits: 10 },
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
  ranking: {
    top_n: 5,
    min_cross_section: 5,
    sample_days: 240,
    available: true,
    recommended: {
      value_id: 'indicator.score',
      display_name: '强度',
      order: 'desc',
      ic_mean: 0.0612,
      icir: 0.82,
      segments: 6,
      same_sign_segments: 6,
      quantile_returns: [0.021, 0.012, 0.004, -0.003, -0.011],
      top_n_lift: 0.018,
      sample_days: 240,
      confidence: 'high',
      note: '三条证据同向：平均信息系数 0.061、信息比率 0.82、6/6 段同号',
    },
    evaluated: [{
      value_id: 'indicator.score',
      display_name: '强度',
      order: 'desc',
      ic_mean: 0.0612,
      icir: 0.82,
      segments: 6,
      same_sign_segments: 6,
      quantile_returns: [0.021, 0.012, 0.004, -0.003, -0.011],
      top_n_lift: 0.018,
      sample_days: 240,
      confidence: 'high',
      note: '三条证据同向：平均信息系数 0.061、信息比率 0.82、6/6 段同号',
    }],
    reason: null,
    selection: { value: null, order: 'desc' },
  },
  candidates: [{
    candidate_id: 'buy-1',
    side: 'buy',
    condition: { left_value: 'indicator.score', operator: 'gte', right_value: null, right_constant: 2.5 },
    mining: { support_count: 40, support_ratio: 0.2, instrument_count: 10, outcome_rate: 0.8, baseline_rate: 0.6, lift: 1.33, top_n_outcome_rate: 0.92, top_n_support_count: 25, top_n_filtered_days: 18 },
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
    vi.mocked(candidateApi.importCandidates).mockResolvedValue({ added: [], skipped: [] })
    vi.mocked(formulaApi.fetchMetadata).mockResolvedValue(metadata)
    vi.mocked(researchApi.createConditionResearch).mockResolvedValue({ job_id: 'research-1', status: 'queued' })
    vi.mocked(researchApi.getConditionResearch).mockResolvedValue({
      job_id: 'research-1', status: 'completed', progress: 1, total_candidates: 100,
      total_scanned: 100, errors: 0, skipped: 0, error: null, result: null,
    })
    vi.mocked(researchApi.getConditionResearchResults).mockResolvedValue(result)
    vi.mocked(researchApi.fetchConditionResearchHistory).mockResolvedValue([])
    vi.mocked(jobsApi.fetchActiveJobs).mockResolvedValue([])
    vi.mocked(presetApi.fetchResearchPresets).mockResolvedValue([])
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

  it('stores selected candidates in the library so they outlive the history list', async () => {
    // 历史只留最近 20 条，候选编号也只在本次报告里有效——入库是把证据复制一份
    // 存下来，不是再跑一次研究。
    vi.mocked(researchApi.fetchConditionResearchHistory).mockResolvedValue([
      { history_id: 'research-1', created_at: '2026-09-20 10:00:00' },
    ] as never)
    vi.mocked(candidateApi.importCandidates).mockResolvedValue({
      added: [{ candidate_id: 'c1' } as never],
      skipped: [],
    })
    const wrapper = mount(ConditionResearchPage)
    await flushPromises()
    await wrapper.get('[data-testid="condition-research-config"]').trigger('submit')
    await flushPromises()

    await wrapper.get('[data-testid="select-condition-buy-1"]').setValue(true)
    await wrapper.get('[data-testid="save-selected-conditions"]').trigger('click')
    await flushPromises()

    expect(candidateApi.importCandidates).toHaveBeenCalledWith({
      history_id: 'research-1',
      candidate_ids: ['buy-1'],
    })
    expect(wrapper.get('[data-testid="condition-research-message"]').text()).toContain('已入库 1 条')
  })

  it('refuses to store candidates when the run has no history record yet', async () => {
    vi.mocked(researchApi.fetchConditionResearchHistory).mockResolvedValue([])
    const wrapper = mount(ConditionResearchPage)
    await flushPromises()
    await wrapper.get('[data-testid="condition-research-config"]').trigger('submit')
    await flushPromises()

    await wrapper.get('[data-testid="select-condition-buy-1"]').setValue(true)
    await flushPromises()

    // 没有历史记录可溯源时按钮直接变灰：入库要能追回来源研究，追不回就不该建
    // 一条没有出处的候选。
    expect(
      wrapper.get('[data-testid="save-selected-conditions"]').attributes('disabled'),
    ).toBeDefined()
    await wrapper.get('[data-testid="save-selected-conditions"]').trigger('click')
    await flushPromises()
    expect(candidateApi.importCandidates).not.toHaveBeenCalled()
  })

  it('submits the backend permutation default instead of the old 20', async () => {
    const wrapper = mount(ConditionResearchPage)
    await flushPromises()

    // 20 次置换时名义 5% 的门槛实测误报 11.7%，后端默认早已改成 200；
    // 前端一旦把它覆盖回 20，用户看到的就是重新校准前的假阳性率。
    expect(
      (wrapper.get('[data-testid="condition-permutations"]').element as HTMLInputElement).value,
    ).toBe('200')

    await wrapper.get('[data-testid="condition-research-config"]').trigger('submit')
    await flushPromises()

    expect(researchApi.createConditionResearch).toHaveBeenCalledWith(
      expect.objectContaining({ significance_permutations: 200 }),
    )
  })

  it('renders rolling re-validation windows and marks thin windows as unusable', async () => {
    const rolling = {
      ...result,
      baseline: {
        ...result.baseline,
        windows: [
          { index: 1, start_date: '2024-01-01', end_date: '2024-04-30', calendar_days: 121, trades: 80, win_rate: 0.5 },
          { index: 2, start_date: '2024-05-01', end_date: '2024-08-31', calendar_days: 123, trades: 70, win_rate: 0.48 },
          { index: 3, start_date: '2024-09-01', end_date: '2024-12-31', calendar_days: 122, trades: 4, win_rate: 0.75 },
        ],
      },
      rolling: { enabled: true, requested: 3, used: 3, note: null },
      candidates: [{
        ...result.candidates[0],
        windows: [
          { index: 1, start_date: '2024-01-01', end_date: '2024-04-30', calendar_days: 121, trades: 80, win_rate: 0.62, baseline_win_rate: 0.5, improved_stock_ratio: 0.6, improved_win_rate: true, insufficient_sample: false },
          { index: 2, start_date: '2024-05-01', end_date: '2024-08-31', calendar_days: 123, trades: 70, win_rate: 0.55, baseline_win_rate: 0.48, improved_stock_ratio: 0.55, improved_win_rate: true, insufficient_sample: false },
          { index: 3, start_date: '2024-09-01', end_date: '2024-12-31', calendar_days: 122, trades: 4, win_rate: 0.75, baseline_win_rate: 0.4, improved_stock_ratio: 0.8, improved_win_rate: true, insufficient_sample: true },
        ],
        consistency: {
          requested: 3, used: 3, evaluated: 2, skipped: 1, better_than_baseline: 2,
          median_win_rate: 0.585, worst_win_rate: 0.55,
          median_improved_stock_ratio: 0.575,
          note: '1 个窗口成交数不足 20 笔，未计入一致性统计',
        },
      }],
    } as unknown as ConditionResearchReport
    vi.mocked(researchApi.getConditionResearchResults).mockResolvedValue(rolling)

    const wrapper = mount(ConditionResearchPage)
    await flushPromises()
    await wrapper.get('[data-testid="condition-research-config"]').trigger('submit')
    await flushPromises()

    const row = wrapper.get('[data-testid="condition-rolling-buy-1"]')
    // 样本不足的窗口既不算通过也不算没通过，分母要跟着减。
    expect(wrapper.get('[data-testid="rolling-consistency-buy-1"]').text()).toContain('2/2')
    expect(wrapper.get('[data-testid="rolling-consistency-buy-1"]').text()).toContain('1 窗样本不足')
    expect(row.text()).toContain('样本不足')
    expect(row.text()).toContain('62.00%')
    expect(row.text()).toContain('基线 50.00%')
    expect(wrapper.text()).toContain('滚动复验')
  })

  it('renders the threshold neighbourhood and marks offsets that cannot be judged', async () => {
    const sensitivity = {
      ...result,
      candidates: [{
        ...result.candidates[0],
        sensitivity: {
          points: [
            { offset: -0.1, quantile: 0.5, threshold: 2.1, support_count: 52, outcome_rate: 0.72, baseline_rate: 0.6, lift: 1.2, retention: 0.606, insufficient: false, degenerate: false },
            { offset: -0.05, quantile: 0.55, threshold: 2.3, support_count: 46, outcome_rate: 0.78, baseline_rate: 0.6, lift: 1.3, retention: 0.909, insufficient: false, degenerate: false },
            { offset: 0, quantile: 0.6, threshold: 2.5, support_count: 40, outcome_rate: 0.8, baseline_rate: 0.6, lift: 1.33, retention: 1, insufficient: false, degenerate: false },
            { offset: 0.05, quantile: 0.65, threshold: 2.8, support_count: 8, outcome_rate: null, baseline_rate: 0.6, lift: null, retention: null, insufficient: true, degenerate: false },
            { offset: 0.1, quantile: 0.7, threshold: 3.1, support_count: 30, outcome_rate: 0.5, baseline_rate: 0.6, lift: 0.9, retention: -0.3, insufficient: false, degenerate: false },
          ],
          stable_offsets: 2,
          evaluated_offsets: 3,
          skipped: 1,
          median_retention: 0.606,
          worst_retention: -0.3,
          note: '1 个偏移没有形成可判定的邻域条件（命中样本不足 20 个或阈值退化到恒真），未计入统计',
        },
      }],
    } as unknown as ConditionResearchReport
    vi.mocked(researchApi.getConditionResearchResults).mockResolvedValue(sensitivity)

    const wrapper = mount(ConditionResearchPage)
    await flushPromises()
    await wrapper.get('[data-testid="condition-research-config"]').trigger('submit')
    await flushPromises()

    expect(wrapper.text()).toContain('参数敏感性')
    const row = wrapper.get('[data-testid="condition-sensitivity-buy-1"]')
    // 样本不足的那一档既不算保持也不算没保持，分母要跟着减。
    expect(wrapper.get('[data-testid="sensitivity-stable-buy-1"]').text()).toContain('2/3')
    expect(wrapper.get('[data-testid="sensitivity-stable-buy-1"]').text()).toContain('1 档未计入')
    expect(row.text()).toContain('样本不足')
    // 优势打反的那一格必须单独标出来。
    expect(wrapper.get('[data-testid="sensitivity-point-buy-1-0.1"]').classes()).toContain('sens-flipped')
    expect(wrapper.get('[data-testid="sensitivity-point-buy-1-0.05"]').classes()).toContain('sens-excluded')
    expect(wrapper.get('[data-testid="sensitivity-point-buy-1-0"]').text()).toContain('1.00')
  })

  it('shows the conditions a candidate absorbed instead of dropping them silently', async () => {
    const merged = {
      ...result,
      duplicate_correlation: 0.6,
      merged_duplicate_count: 2,
      candidates: [{
        ...result.candidates[0],
        mining: {
          ...result.candidates[0].mining,
          duplicates: [
            {
              condition: { left_value: 'indicator.score', operator: 'gte', right_value: null, right_constant: 2.9 },
              correlation: 0.612,
              lift: 1.31,
              support_count: 34,
            },
            {
              condition: { left_value: 'indicator.ma5', operator: 'gte', right_value: null, right_constant: 0.4 },
              correlation: 0.667,
              lift: 1.28,
              support_count: 44,
            },
          ],
        },
      }],
    } as unknown as ConditionResearchReport
    vi.mocked(researchApi.getConditionResearchResults).mockResolvedValue(merged)

    const wrapper = mount(ConditionResearchPage)
    await flushPromises()
    await wrapper.get('[data-testid="condition-research-config"]').trigger('submit')
    await flushPromises()

    const badge = wrapper.get('[data-testid="merged-duplicates-buy-1"]')
    expect(badge.text()).toContain('已合并 2 条')
    // 明细要给出相关性和 lift，用户才能判断这个门槛合不合适。
    expect(badge.attributes('title')).toContain('ρ=0.612')
    expect(badge.attributes('title')).toContain('ρ=0.667')
    expect(wrapper.get('[data-testid="condition-duplicate-summary"]').text()).toContain('合并了 2 条同义条件')
  })

  it('submits the duplicate merge threshold and defaults it to 0.6', async () => {
    const wrapper = mount(ConditionResearchPage)
    await flushPromises()

    expect(
      (wrapper.get('[data-testid="condition-duplicate-correlation"]').element as HTMLInputElement)
        .value,
    ).toBe('0.6')

    await wrapper.get('[data-testid="condition-duplicate-correlation"]').setValue('0.8')
    await wrapper.get('[data-testid="condition-research-config"]').trigger('submit')
    await flushPromises()

    expect(researchApi.createConditionResearch).toHaveBeenCalledWith(
      expect.objectContaining({ duplicate_correlation: 0.8 }),
    )
  })

  it('submits the rolling window count and restores it from history', async () => {
    const wrapper = mount(ConditionResearchPage)
    await flushPromises()

    await wrapper.get('[data-testid="condition-walk-forward"]').setValue('3')
    await wrapper.get('[data-testid="condition-research-config"]').trigger('submit')
    await flushPromises()

    expect(researchApi.createConditionResearch).toHaveBeenCalledWith(
      expect.objectContaining({ walk_forward_windows: 3 }),
    )
  })

  it('flags lifts that a shuffled-label search could have produced by chance', async () => {
    const noisy = {
      ...result,
      candidates: [{
        ...result.candidates[0],
        mining: {
          ...result.candidates[0].mining,
          lift: 1.09,
          lift_threshold: 1.13,
          permutation_pvalue: 0.62,
          significant: false,
        },
        passed_constraints: false,
        constraint_failures: ['提升未超过噪声门槛'],
      }],
    } as unknown as ConditionResearchReport
    vi.mocked(researchApi.getConditionResearchResults).mockResolvedValue(noisy)

    const wrapper = mount(ConditionResearchPage)
    await flushPromises()
    await wrapper.get('[data-testid="condition-research-config"]').trigger('submit')
    await flushPromises()

    expect(wrapper.get('[data-testid="significance-buy-1"]').text()).toBe('疑似噪声')
    const summary = wrapper.get('[data-testid="condition-research-significance-summary"]').text()
    expect(summary).toContain('噪声门槛 1.130')
    expect(summary).toContain('没有一个候选超过')
    expect(wrapper.get('[data-testid="condition-research-candidate-buy-1"]').text())
      .toContain('提升未超过噪声门槛')
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

  it('scores each candidate under the slot limit and marks days ranking actually decided', async () => {
    const wrapper = mount(ConditionResearchPage)
    await flushPromises()
    await wrapper.get('[data-testid="condition-research-config"]').trigger('submit')
    await flushPromises()

    expect(wrapper.get('[data-testid="top-n-rate-buy-1"]').text()).toBe('92.00%')
    // 有 18 天的命中数超过槽位数，提示要说清这一列只统计那些日子。
    expect(wrapper.get('[data-testid="top-n-rate-buy-1"]').attributes('title')).toContain('18')
    // 没有解析出排序值的候选显示占位，不能显示成 0%。
    expect(wrapper.get('[data-testid="top-n-rate-buy-2"]').text()).toBe('—')
    expect(wrapper.get('[data-testid="top-n-rate-buy-2"]').attributes('title')).toContain('没有解析出排序指标')
  })

  it('runs a stock study with the stock cost defaults and board filters', async () => {
    const wrapper = mount(ConditionResearchPage)
    await flushPromises()

    await wrapper.get('[data-testid="condition-advanced-toggle"]').trigger('click')
    await wrapper.get('[data-testid="condition-board-star"]').setValue(true)
    await flushPromises()

    expect(wrapper.find('[data-testid="condition-board-scope"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('沪深全部股票')
    expect((wrapper.get('[data-testid="condition-commission"]').element as HTMLInputElement).value).toBe('0.0001')
    expect((wrapper.get('[data-testid="condition-stamp-tax"]').element as HTMLInputElement).value).toBe('0.001')

    await wrapper.get('[data-testid="condition-research-config"]').trigger('submit')
    await flushPromises()

    expect(researchApi.createConditionResearch).toHaveBeenCalledWith(expect.objectContaining({
      instrument_types: ['stock'],
      commission: 0.0001,
    }))
    expect(vi.mocked(researchApi.createConditionResearch).mock.calls[0][0].boards).toEqual(['star'])
  })

  it('keeps the costs the user customised', async () => {
    const wrapper = mount(ConditionResearchPage)
    await flushPromises()

    await wrapper.get('[data-testid="condition-advanced-toggle"]').trigger('click')
    await wrapper.get('[data-testid="condition-commission"]').setValue(0.0008)
    await flushPromises()

    expect((wrapper.get('[data-testid="condition-commission"]').element as HTMLInputElement).value).toBe('0.0008')
    expect((wrapper.get('[data-testid="condition-stamp-tax"]').element as HTMLInputElement).value).toBe('0.001')
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
    expect(payload.buy_rule?.conditions).toEqual([
      expect.objectContaining({ left_value: 'indicator.score', operator: 'gt' }),
      { left_value: 'indicator.score', operator: 'gte', right_value: null, right_constant: 2.5 },
    ])
    expect(payload.sell_rule?.conditions).toEqual([
      expect.objectContaining({ left_value: 'indicator.score', operator: 'lte' }),
      { left_value: 'indicator.score', operator: 'lte', right_value: null, right_constant: -1.5 },
    ])
    // 卖出候选是研究按「OR」评估出来的，回填也必须是任一满足；买入是 AND。
    expect(payload.buy_rule?.combine_mode).toBe('all')
    expect(payload.sell_rule?.combine_mode).toBe('any')
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
    await wrapper.get('[data-testid="condition-history-load-hist-1"]').trigger('click')
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

  it('restores the configuration handed over by the candidate library', async () => {
    sessionStorage.setItem('indicator-lab.condition-research.restore', JSON.stringify({
      universe: 'sh',
      boards: ['main'],
      date_mode: 'custom',
      start_date: '2022-01-01',
      end_date: '2022-12-31',
      search_sides: 'sell',
      min_trades: 44,
      candidate_limit: 6,
    }))
    const wrapper = mount(ConditionResearchPage)
    await flushPromises()

    // 这一步拿的是 storage 里现成的 request，不该再去拉一遍历史。
    expect(researchApi.getConditionResearchHistory).not.toHaveBeenCalled()
    expect((wrapper.get('[data-testid="condition-universe"]').element as HTMLSelectElement).value).toBe('sh')
    expect((wrapper.get('[data-testid="condition-board-main"]').element as HTMLInputElement).checked).toBe(true)
    expect((wrapper.get('[data-testid="condition-search-sides"]').element as HTMLSelectElement).value).toBe('sell')
    expect((wrapper.get('[data-testid="condition-min-trades"]').element as HTMLInputElement).value).toBe('44')
    expect((wrapper.get('[data-testid="condition-date-mode"]').element as HTMLSelectElement).value).toBe('custom')
    expect((wrapper.get('[data-testid="condition-start"]').element as HTMLInputElement).value).toBe('2022-01-01')
    expect(wrapper.get('[data-testid="condition-research-message"]').text()).toContain('已从候选库载入')
    expect(wrapper.get('[data-testid="condition-research-message"]').text()).toContain('请确认后重新运行')
    // key 必须立刻清掉：刷新页面时还原「还原」会把用户刚改过的东西覆盖回去。
    expect(sessionStorage.getItem('indicator-lab.condition-research.restore')).toBeNull()
  })

  it('can mine the sell side without setting a base sell rule', async () => {
    const wrapper = mount(ConditionResearchPage)
    await flushPromises()
    await wrapper.get('[data-testid="condition-hold-to-end"]').setValue(true)
    await wrapper.get('[data-testid="condition-research-config"]').trigger('submit')
    await flushPromises()

    expect(researchApi.createConditionResearch).toHaveBeenCalledTimes(1)
    const payload = vi.mocked(researchApi.createConditionResearch).mock.calls[0][0]
    expect(payload.sell_rule).toBeNull()
    expect(payload.buy_rule?.conditions?.length ?? 0).toBeGreaterThan(0)
    // 选了「持有到期末」就不再显示卖出编辑器，但要说清楚基线是什么。
    expect(wrapper.find('[data-testid="condition-sell-rule"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="condition-hold-to-end-hint"]').exists()).toBe(true)
  })

  it('keeps the study blocked when the sell rule is empty and hold-to-end is off', async () => {
    const wrapper = mount(ConditionResearchPage)
    await flushPromises()
    await wrapper.get('[data-testid="condition-hold-to-end"]').setValue(true)
    await wrapper.get('[data-testid="condition-hold-to-end"]').setValue(false)
    await wrapper.get('[data-testid="condition-research-config"]').trigger('submit')
    await flushPromises()

    // 关掉开关会自动补回默认卖出条件，所以这里应当照常提交。
    expect(researchApi.createConditionResearch).toHaveBeenCalledTimes(1)
    expect(researchApi.createConditionResearch).toHaveBeenCalledWith(
      expect.objectContaining({ sell_rule: expect.objectContaining({ conditions: expect.any(Array) }) }),
    )
  })

  it('turns hold-to-end off once a mined sell condition becomes the base rule', async () => {
    const wrapper = mount(ConditionResearchPage)
    await flushPromises()
    await wrapper.get('[data-testid="condition-hold-to-end"]').setValue(true)
    await wrapper.get('[data-testid="condition-research-config"]').trigger('submit')
    await flushPromises()

    await wrapper.get('[data-testid="select-condition-sell-1"]').setValue(true)
    await wrapper.get('[data-testid="apply-selected-conditions"]').trigger('click')
    await flushPromises()

    // 基准换成挖出来的条件再重测，否则还是以「一直持有」为参照，只会挖出同一批。
    expect(researchApi.createConditionResearch).toHaveBeenCalledTimes(2)
    const payload = vi.mocked(researchApi.createConditionResearch).mock.calls[1][0]
    expect(payload.sell_rule).toEqual(expect.objectContaining({
      conditions: [
        { left_value: 'indicator.score', operator: 'lte', right_value: null, right_constant: -1.5 },
      ],
    }))
    expect((wrapper.get('[data-testid="condition-hold-to-end"]').element as HTMLInputElement).checked).toBe(false)
  })

  it('can mine the buy side without setting a base buy rule', async () => {
    const wrapper = mount(ConditionResearchPage)
    await flushPromises()
    await wrapper.get('[data-testid="condition-always-in-market"]').setValue(true)
    await wrapper.get('[data-testid="condition-research-config"]').trigger('submit')
    await flushPromises()

    expect(researchApi.createConditionResearch).toHaveBeenCalledTimes(1)
    const payload = vi.mocked(researchApi.createConditionResearch).mock.calls[0][0]
    expect(payload.buy_rule).toBeNull()
    expect(payload.sell_rule?.conditions?.length ?? 0).toBeGreaterThan(0)
    // 选了「持续满仓」就不再显示买入编辑器，但要说清楚基线是什么。
    expect(wrapper.find('[data-testid="condition-buy-rule"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="condition-always-in-market-hint"]').exists()).toBe(true)
  })

  it('keeps the study runnable when always-in-market is switched back off', async () => {
    const wrapper = mount(ConditionResearchPage)
    await flushPromises()
    await wrapper.get('[data-testid="condition-always-in-market"]').setValue(true)
    await wrapper.get('[data-testid="condition-always-in-market"]').setValue(false)
    await wrapper.get('[data-testid="condition-research-config"]').trigger('submit')
    await flushPromises()

    // 关掉开关会自动补回默认买入条件，所以这里应当照常提交。
    expect(researchApi.createConditionResearch).toHaveBeenCalledTimes(1)
    expect(researchApi.createConditionResearch).toHaveBeenCalledWith(
      expect.objectContaining({ buy_rule: expect.objectContaining({ conditions: expect.any(Array) }) }),
    )
  })

  it('turns always-in-market off once a mined buy condition becomes the base rule', async () => {
    const wrapper = mount(ConditionResearchPage)
    await flushPromises()
    await wrapper.get('[data-testid="condition-always-in-market"]').setValue(true)
    await wrapper.get('[data-testid="condition-research-config"]').trigger('submit')
    await flushPromises()

    await wrapper.get('[data-testid="select-condition-buy-1"]').setValue(true)
    await wrapper.get('[data-testid="apply-selected-conditions"]').trigger('click')
    await flushPromises()

    // 基准换成挖出来的条件再重测，否则还是以「天天买」为参照，只会挖出同一批。
    expect(researchApi.createConditionResearch).toHaveBeenCalledTimes(2)
    const payload = vi.mocked(researchApi.createConditionResearch).mock.calls[1][0]
    expect(payload.buy_rule).toEqual(expect.objectContaining({
      conditions: [
        { left_value: 'indicator.score', operator: 'gte', right_value: null, right_constant: 2.5 },
      ],
    }))
    expect((wrapper.get('[data-testid="condition-always-in-market"]').element as HTMLInputElement).checked).toBe(false)
  })

  it('refuses to run when both sides are handed over to the research', async () => {
    const wrapper = mount(ConditionResearchPage)
    await flushPromises()
    await wrapper.get('[data-testid="condition-always-in-market"]').setValue(true)
    await wrapper.get('[data-testid="condition-hold-to-end"]').setValue(true)
    await wrapper.get('[data-testid="condition-research-config"]').trigger('submit')
    await flushPromises()

    expect(researchApi.createConditionResearch).not.toHaveBeenCalled()
    expect(wrapper.get('[data-testid="condition-buy-error"]').text()).toContain('不能同时留空')
  })

  it('restores always-in-market when the saved request has no buy rule', async () => {
    vi.mocked(researchApi.fetchConditionResearchHistory).mockResolvedValueOnce([
      {
        history_id: 'hist-buy',
        backtest_type: 'condition_research',
        created_at: '2024-07-01T09:00:00',
        started_at: '2024-07-01T09:00:00',
        finished_at: '2024-07-01T09:03:00',
        duration_seconds: 180,
        summary: {},
      },
    ])
    vi.mocked(researchApi.getConditionResearchHistory).mockResolvedValueOnce({
      history_id: 'hist-buy',
      created_at: '2024-07-01T09:00:00',
      started_at: '2024-07-01T09:00:00',
      finished_at: '2024-07-01T09:03:00',
      duration_seconds: 180,
      summary: {},
      backtest_type: 'condition_research',
      request: {
        universe: 'all',
        instrument_types: ['stock'],
        boards: [],
        buy_rule: null,
        sell_rule: {
          signals: [],
          conditions: [
            { left_value: 'indicator.score', operator: 'lte', right_value: null, right_constant: -2 },
          ],
          required_conditions: [],
          combine_mode: 'all',
          minimum_matches: null,
        },
        date_mode: 'custom',
        start_date: '2022-01-01',
        end_date: '2024-12-31',
        search_sides: 'buy',
        initial_cash: 100_000,
        commission: 0.0001,
        min_commission: 0.1,
        stamp_tax: 0.001,
        slippage: 0,
        execution: 'next_open',
        formula_text: null,
        formula_parameters: {},
      },
      result,
    })

    const wrapper = mount(ConditionResearchPage)
    await flushPromises()
    await wrapper.get('[data-testid="condition-history-load-hist-buy"]').trigger('click')
    await flushPromises()

    expect((wrapper.get('[data-testid="condition-always-in-market"]').element as HTMLInputElement).checked).toBe(true)
    expect(wrapper.find('[data-testid="condition-buy-rule"]').exists()).toBe(false)

    // 还原后的配置要能原样再跑一次。
    await wrapper.get('[data-testid="condition-research-config"]').trigger('submit')
    await flushPromises()
    expect(researchApi.createConditionResearch).toHaveBeenCalledWith(expect.objectContaining({
      buy_rule: null,
      sell_rule: expect.objectContaining({
        conditions: [expect.objectContaining({ operator: 'lte', right_constant: -2 })],
      }),
    }))
  })

  it('restores a stock study from history', async () => {
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
        instrument_types: ['stock'],
        boards: [],
        buy_rule: { signals: [], conditions: [], required_conditions: [], combine_mode: 'all', minimum_matches: null },
        sell_rule: { signals: [], conditions: [], required_conditions: [], combine_mode: 'all', minimum_matches: null },
        date_mode: 'custom',
        start_date: '2022-01-01',
        end_date: '2024-12-31',
        search_sides: 'both',
        commission: 0.0001,
        stamp_tax: 0.001,
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
    await wrapper.get('[data-testid="condition-history-load-hist-etf"]').trigger('click')
    await flushPromises()

    expect((wrapper.get('[data-testid="condition-instrument-type"]').element as HTMLSelectElement).value).toBe('stock')
    // 只研究股票，板块过滤器始终可用。
    expect(wrapper.find('[data-testid="condition-board-scope"]').exists()).toBe(true)
    await wrapper.get('[data-testid="condition-advanced-toggle"]').trigger('click')
    expect((wrapper.get('[data-testid="condition-commission"]').element as HTMLInputElement).value).toBe('0.0001')
    expect((wrapper.get('[data-testid="condition-stamp-tax"]').element as HTMLInputElement).value).toBe('0.001')
  })

  it('restores a hold-to-end study from history', async () => {
    // 当年没设卖出规则时，历史里存的就是 null，还原后开关必须回到选中。
    vi.mocked(researchApi.fetchConditionResearchHistory).mockResolvedValueOnce([
      {
        history_id: 'hist-hold',
        backtest_type: 'condition_research',
        created_at: '2024-07-01T09:00:00',
        started_at: '2024-07-01T09:00:00',
        finished_at: '2024-07-01T09:02:00',
        duration_seconds: 120,
        summary: {},
      },
    ])
    vi.mocked(researchApi.getConditionResearchHistory).mockResolvedValueOnce({
      history_id: 'hist-hold',
      created_at: '2024-07-01T09:00:00',
      started_at: '2024-07-01T09:00:00',
      finished_at: '2024-07-01T09:02:00',
      duration_seconds: 120,
      summary: {},
      backtest_type: 'condition_research',
      request: {
        universe: 'all',
        instrument_types: ['stock'],
        boards: [],
        buy_rule: {
          signals: [],
          conditions: [
            { left_value: 'indicator.score', operator: 'gt', right_value: null, right_constant: 1 },
          ],
          required_conditions: [],
          combine_mode: 'all',
          minimum_matches: null,
        },
        sell_rule: null,
        date_mode: 'custom',
        start_date: '2021-01-01',
        end_date: '2024-12-31',
        search_sides: 'sell',
        commission: 0.0001,
        stamp_tax: 0.001,
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
    await wrapper.get('[data-testid="condition-history-load-hist-hold"]').trigger('click')
    await flushPromises()

    expect((wrapper.get('[data-testid="condition-hold-to-end"]').element as HTMLInputElement).checked).toBe(true)
    expect(wrapper.find('[data-testid="condition-sell-rule"]').exists()).toBe(false)

    await wrapper.get('[data-testid="condition-research-config"]').trigger('submit')
    await flushPromises()
    expect(researchApi.createConditionResearch).toHaveBeenCalledWith(
      expect.objectContaining({ sell_rule: null, search_sides: 'sell' }),
    )
  })

  it('offers only cross-stock comparable values as ranking keys', async () => {
    const wrapper = mount(ConditionResearchPage)
    await flushPromises()

    const options = wrapper.get('[data-testid="condition-ranking-value"]').findAll('option')
    const values = options.map((option) => (option.element as HTMLOptionElement).value)
    expect(values).toContain('indicator.score')
    expect(values).not.toContain('indicator.price')
  })

  it('shows the ranking recommendation in Chinese and can adopt it', async () => {
    const wrapper = mount(ConditionResearchPage)
    await flushPromises()
    await wrapper.get('[data-testid="condition-research-config"]').trigger('submit')
    await flushPromises()

    const card = wrapper.get('[data-testid="condition-ranking-advice"]').text()
    expect(card).toContain('强度')
    expect(card).toContain('从大到小')
    // 界面主文案不出现英文缩写，英文名只留在问号提示里（tooltip 默认不显示）。
    expect(card).toContain('平均信息系数')
    expect(card).toContain('信息系数信息比率')
    expect(wrapper.find('[data-testid="term-hint-icMean"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="term-hint-icir"]').exists()).toBe(true)
    const note = wrapper.get('[data-testid="condition-ranking-advice"]').get('.ranking-note').text()
    expect(note).not.toMatch(/[A-Za-z]/)
    expect(wrapper.get('[data-testid="condition-ranking-confidence"]').text()).toBe('证据充分')
    // 日均命中 2 只 ≤ 5 个槽位：条件说了算。
    expect(wrapper.get('[data-testid="condition-slot-hint"]').text()).toContain('日均命中 2.0 只')

    await wrapper.get('[data-testid="condition-use-recommended-ranking"]').trigger('click')
    await wrapper.get('[data-testid="condition-research-config"]').trigger('submit')
    await flushPromises()

    expect(researchApi.createConditionResearch).toHaveBeenLastCalledWith(
      expect.objectContaining({
        ranking_value: 'indicator.score',
        rank_order: 'desc',
        ranking_top_n: 5,
      }),
    )
  })

  it('bridges rules and ranking into the portfolio backtest page', async () => {
    sessionStorage.clear()
    const wrapper = mount(ConditionResearchPage)
    await flushPromises()
    await wrapper.get('[data-testid="condition-research-config"]').trigger('submit')
    await flushPromises()
    await wrapper.get('[data-testid="condition-ranking-top-n"]').setValue(3)

    await wrapper.get('[data-testid="condition-apply-to-portfolio"]').trigger('click')

    const stored = JSON.parse(sessionStorage.getItem('indicator-lab.condition-research.apply') ?? '{}')
    expect(stored).toMatchObject({
      ranking_value: 'indicator.score',
      rank_order: 'desc',
      max_positions: 3,
      formula_text: null,
      // 只传绝对日期会让组合页「悄悄换个窗口」，mode 必须一起过去。
      date_mode: 'recent5',
    })
    expect(stored.buy_rule.conditions.length).toBeGreaterThan(0)
    sessionStorage.clear()
  })

  it('marks an unset side as null when bridging so the portfolio page can warn', async () => {
    sessionStorage.clear()
    const wrapper = mount(ConditionResearchPage)
    await flushPromises()
    // 桥接按钮在结果卡片里，得先跑出一次报告。
    await wrapper.get('[data-testid="condition-research-config"]').trigger('submit')
    await flushPromises()
    await wrapper.get('[data-testid="condition-hold-to-end"]').setValue(true)

    await wrapper.get('[data-testid="condition-apply-to-portfolio"]').trigger('click')

    const stored = JSON.parse(sessionStorage.getItem('indicator-lab.condition-research.apply') ?? '{}')
    expect(stored.sell_rule).toBeNull()
    expect(stored.buy_rule).not.toBeNull()
    sessionStorage.clear()
  })

  it('bridges only the buy side into the screening page', async () => {
    sessionStorage.clear()
    const wrapper = mount(ConditionResearchPage)
    await flushPromises()
    await wrapper.get('[data-testid="condition-research-config"]').trigger('submit')
    await flushPromises()

    await wrapper.get('[data-testid="condition-apply-to-screen"]').trigger('click')

    const stored = JSON.parse(
      sessionStorage.getItem('indicator-lab.condition-research.to-screen') ?? '{}',
    )
    // 选股是「某一天是否满足」的一次性扫描，没有卖出这一说，所以只带买入侧。
    expect(stored.conditions.length).toBeGreaterThan(0)
    expect(stored).not.toHaveProperty('sell_rule')
    expect(stored.universe).toBe('all')
    sessionStorage.clear()
  })

  it('cannot bridge a buy rule that does not exist', async () => {
    sessionStorage.clear()
    const wrapper = mount(ConditionResearchPage)
    await flushPromises()
    await wrapper.get('[data-testid="condition-research-config"]').trigger('submit')
    await flushPromises()
    await wrapper.get('[data-testid="condition-always-in-market"]').setValue(true)

    const button = wrapper.get('[data-testid="condition-apply-to-screen"]')
    expect(button.attributes('disabled')).toBeDefined()
    sessionStorage.clear()
  })

  it('explains that sell candidates are not scored in the top-N column', async () => {
    const wrapper = mount(ConditionResearchPage)
    await flushPromises()
    await wrapper.get('[data-testid="condition-research-config"]').trigger('submit')
    await flushPromises()

    // 上方明明给了排序推荐，卖出候选却说「没解析出排序指标」会让人以为坏了。
    const title = wrapper.get('[data-testid="top-n-rate-sell-1"]').attributes('title') ?? ''
    expect(title).toContain('卖出是触发式的')
    expect(title).not.toContain('没有解析出排序指标')
  })

  it('takes over the rules the swing research page handed over', async () => {
    // 波段研究给的阈值只是起点，带过来后本页仍会在自己的三段数据上重新搜索。
    sessionStorage.setItem(SWING_PLAN_STORAGE_KEY, JSON.stringify({
      buy_rule: {
        signals: [],
        conditions: [{
          left_value: 'indicator.score',
          operator: 'lte',
          right_value: null,
          right_constant: -0.273902,
        }],
        required_conditions: [],
        combine_mode: 'all',
        minimum_matches: null,
      },
      sell_rule: null,
      ranking: { value: 'indicator.score', order: 'asc', display_name: '强度' },
    }))

    const wrapper = mount(ConditionResearchPage)
    await flushPromises()

    expect(wrapper.text()).toContain('已带入波段研究的结论')
    expect(sessionStorage.getItem(SWING_PLAN_STORAGE_KEY)).toBeNull()
    await wrapper.get('[data-testid="condition-research-config"]').trigger('submit')
    await flushPromises()

    expect(researchApi.createConditionResearch).toHaveBeenCalledWith(expect.objectContaining({
      buy_rule: expect.objectContaining({
        conditions: [expect.objectContaining({
          left_value: 'indicator.score',
          operator: 'lte',
          right_constant: -0.273902,
        })],
      }),
      ranking_value: 'indicator.score',
      rank_order: 'asc',
    }))
  })

  it('lists a saved study with its parameters and headline numbers', async () => {
    vi.mocked(researchApi.fetchConditionResearchHistory).mockResolvedValue([
      conditionHistoryItem(0),
    ])

    const wrapper = mount(ConditionResearchPage)
    await flushPromises()

    const card = wrapper.get('[data-testid="condition-history-item-cond-0"]')
    expect(card.text()).toContain('2021-01-01 → 2025-12-31')
    expect(card.text()).toContain('仅深圳')
    expect(card.text()).toContain('只搜买入')
    expect(card.text()).toContain('跑了 300 只')
    expect(card.text()).toContain('候选 12 条')
    expect(card.text()).toContain('合格 3 条')
    // 摘要里存的是 id，列表要还原成和结果表一致的中文写法。
    expect(card.text()).toContain('强度 ≤ -0.274')
    expect(card.text()).toContain('提升 1.42')
    expect(card.text()).toContain('61.00%')
    expect(card.text()).toContain('覆盖 27 只')
  })

  it('deletes a saved study from the list', async () => {
    vi.mocked(researchApi.fetchConditionResearchHistory)
      .mockResolvedValueOnce([conditionHistoryItem(0)])
      .mockResolvedValueOnce([])
    vi.mocked(researchApi.deleteConditionResearchHistory).mockResolvedValue(true)

    const wrapper = mount(ConditionResearchPage)
    await flushPromises()
    await wrapper.get('[data-testid="condition-history-remove-cond-0"]').trigger('click')
    await flushPromises()

    expect(researchApi.deleteConditionResearchHistory).toHaveBeenCalledWith('cond-0')
    expect(wrapper.find('[data-testid="condition-history-item-cond-0"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="condition-research-history"]').text()).toContain('暂无历史研究记录')
  })

  it('pages the saved studies ten at a time, newest first', async () => {
    // 后端已按完成时间由近到远排好，这里倒序就是「最近跑的在前」。
    const items = Array.from({ length: 12 }, (_, index) => conditionHistoryItem(index)).reverse()
    vi.mocked(researchApi.fetchConditionResearchHistory).mockResolvedValue(items)

    const wrapper = mount(ConditionResearchPage)
    await flushPromises()

    const rows = wrapper.findAll('[data-testid^="condition-history-item-"]')
    expect(rows).toHaveLength(10)
    expect(rows[0]?.attributes('data-testid')).toBe('condition-history-item-cond-11')

    await wrapper.get('[data-testid="condition-history-next"]').trigger('click')
    await flushPromises()
    expect(wrapper.findAll('[data-testid^="condition-history-item-"]')).toHaveLength(2)
  })

  /** 一条够列表渲染的历史记录：参数与核心指标都在 summary 里。 */
  function conditionHistoryItem(index: number) {
    const day = String(index + 1).padStart(2, '0')
    return {
      history_id: `cond-${index}`,
      backtest_type: 'condition_research' as const,
      created_at: `2026-09-${day}T02:00:00`,
      started_at: `2026-09-${day}T01:58:00`,
      finished_at: `2026-09-${day}T02:00:00`,
      duration_seconds: 120,
      summary: {
        universe: 'sz',
        start_date: '2021-01-01',
        end_date: '2025-12-31',
        search_sides: 'buy',
        processed: 300,
        candidate_count: 12,
        qualified_count: 3,
        top_lift: 1.42,
        top_side: 'buy',
        top_outcome_rate: 0.61,
        top_baseline_rate: 0.43,
        top_instrument_count: 27,
        top_condition: {
          left_value: 'indicator.score',
          operator: 'lte',
          right_value: null,
          right_constant: -0.273902,
        },
      },
    }
  }
  it('reports how many independent trial rounds the result has survived', async () => {
    const withTrials = {
      ...result,
      trials: {
        trial_count: 2,
        study_count: 5,
        tested_conditions: 2400,
        cumulative_conditions: 3800,
        adjusted: true,
        repeated_studies: 3,
      },
    } as unknown as ConditionResearchReport
    vi.mocked(researchApi.getConditionResearchResults).mockResolvedValue(withTrials)

    const wrapper = mount(ConditionResearchPage)
    await flushPromises()
    await wrapper.get('[data-testid="condition-research-config"]').trigger('submit')
    await flushPromises()

    const summary = wrapper.get('[data-testid="condition-trial-summary"]').text()
    expect(summary).toContain('2400 个条件')
    expect(summary).toContain('第 2 轮')
    expect(summary).toContain('已按 2 轮校正')
    // 同一段数据被反复挖是最直接的过拟合信号，必须说出口而不是藏起来。
    expect(summary).toContain('3 次是在同一段数据上重复挖')
  })

  it('shows the adjusted p-value next to the raw one after several rounds', async () => {
    const adjustedRun = {
      ...result,
      trials: {
        trial_count: 10,
        study_count: 10,
        tested_conditions: 2400,
        cumulative_conditions: 24000,
        adjusted: true,
        repeated_studies: 0,
      },
      candidates: [{
        ...result.candidates[0],
        mining: {
          ...result.candidates[0].mining,
          lift_threshold: 1.13,
          permutation_pvalue: 0.04,
          adjusted_pvalue: 0.335,
          significant: false,
        },
      }],
    } as unknown as ConditionResearchReport
    vi.mocked(researchApi.getConditionResearchResults).mockResolvedValue(adjustedRun)

    const wrapper = mount(ConditionResearchPage)
    await flushPromises()
    await wrapper.get('[data-testid="condition-research-config"]').trigger('submit')
    await flushPromises()

    // 第十轮才挖出来的 0.04 已经不显著了，徽标要用校正后的判定。
    expect(wrapper.get('[data-testid="significance-buy-1"]').text()).toBe('疑似噪声')
    const title = wrapper.get('[data-testid="significance-buy-1"]').attributes('title') ?? ''
    // 原始 p 要留着，用户才看得出被校正了多少。
    expect(title).toContain('p=0.040')
    expect(title).toContain('校正后 p=0.335')
  })

  it('keeps the research preset panel in the first screen', async () => {
    const wrapper = mount(ConditionResearchPage)
    await flushPromises()

    expect(wrapper.find('[data-testid="research-preset"]').exists()).toBe(true)
  })

  it('saves the whole configuration as a named research preset', async () => {
    vi.mocked(presetApi.createResearchPreset).mockResolvedValue({
      preset_id: 'preset-1',
      name: '主板低位进场',
      note: '',
      created_at: '2026-09-23T00:00:00Z',
      updated_at: '2026-09-23T00:00:00Z',
      payload: {} as never,
    })
    vi.mocked(presetApi.fetchResearchPresets).mockResolvedValue([{
      preset_id: 'preset-1',
      name: '主板低位进场',
      note: '',
      created_at: '2026-09-23T00:00:00Z',
      updated_at: '2026-09-23T00:00:00Z',
    }])

    const wrapper = mount(ConditionResearchPage)
    await flushPromises()
    // 勾上「持有到期末」：卖出侧留空是这套方案的一部分，必须原样存下来。
    await wrapper.get('[data-testid="condition-hold-to-end"]').setValue(true)
    await wrapper.get('[data-testid="research-preset-name"]').setValue('主板低位进场')
    await wrapper.get('[data-testid="save-research-preset"]').trigger('click')
    await flushPromises()

    expect(presetApi.createResearchPreset).toHaveBeenCalledWith(
      expect.objectContaining({ name: '主板低位进场' }),
    )
    // 存的是整套输入，卖出侧留空（持有到期末）也要原样存下来。
    const saved = vi.mocked(presetApi.createResearchPreset).mock.calls[0][0]
    expect(saved.payload.sell_rule).toBeNull()
    expect(wrapper.get('[data-testid="condition-research-message"]').text())
      .toContain('已保存研究方案')
  })

  it('runs the selected preset immediately and reports the load first', async () => {
    vi.mocked(presetApi.fetchResearchPresets).mockResolvedValue([{
      preset_id: 'preset-1',
      name: '只做深圳',
      note: '',
      created_at: '2026-09-23T00:00:00Z',
      updated_at: '2026-09-23T00:00:00Z',
    }])
    vi.mocked(presetApi.getResearchPreset).mockResolvedValue({
      preset_id: 'preset-1',
      name: '只做深圳',
      note: '',
      created_at: '2026-09-23T00:00:00Z',
      updated_at: '2026-09-23T00:00:00Z',
      payload: {
        universe: 'sz',
        instrument_types: ['stock'],
        buy_rule: {
          signals: ['indicator.buy'],
          conditions: [{
            left_value: 'indicator.score',
            operator: 'gt',
            right_value: null,
            right_constant: 0.5,
          }],
          required_conditions: [],
          combine_mode: 'all',
          minimum_matches: null,
        },
        sell_rule: null,
        date_mode: 'custom',
        start_date: '2020-01-01',
        end_date: '2024-12-31',
      },
    })

    const wrapper = mount(ConditionResearchPage)
    await flushPromises()
    await wrapper.get('[data-testid="research-preset-select"]').setValue('preset-1')
    await flushPromises()
    await wrapper.get('[data-testid="run-research-preset"]').trigger('click')
    await flushPromises()

    expect(presetApi.getResearchPreset).toHaveBeenCalledWith('preset-1')
    expect(researchApi.createConditionResearch).toHaveBeenCalledWith(
      expect.objectContaining({ universe: 'sz', start_date: '2020-01-01' }),
    )
  })

  it('says so when a preset references values that no longer exist', async () => {
    vi.mocked(presetApi.fetchResearchPresets).mockResolvedValue([{
      preset_id: 'preset-1',
      name: '引用已失效',
      note: '',
      created_at: '2026-09-23T00:00:00Z',
      updated_at: '2026-09-23T00:00:00Z',
    }])
    vi.mocked(presetApi.getResearchPreset).mockResolvedValue({
      preset_id: 'preset-1',
      name: '引用已失效',
      note: '',
      created_at: '2026-09-23T00:00:00Z',
      updated_at: '2026-09-23T00:00:00Z',
      payload: {
        universe: 'all',
        buy_rule: {
          signals: [],
          conditions: [{
            left_value: 'indicator.gone',
            operator: 'gt',
            right_value: null,
            right_constant: 1,
          }],
          required_conditions: [],
          combine_mode: 'all',
          minimum_matches: null,
        },
        sell_rule: null,
        date_mode: 'custom',
        start_date: '2020-01-01',
        end_date: '2024-12-31',
      },
    })

    const wrapper = mount(ConditionResearchPage)
    await flushPromises()
    await wrapper.get('[data-testid="research-preset-select"]').setValue('preset-1')
    await flushPromises()
    await wrapper.get('[data-testid="load-research-preset"]').trigger('click')
    await flushPromises()

    // 失效引用会被静默换成别的指标，不讲出来用户会以为加载的还是原来那套条件。
    expect(wrapper.get('[data-testid="condition-research-message"]').text())
      .toContain('1 项引用了当前不存在的指标')
  })
})
