import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { FactorTestReport, FormulaScreenMetadata } from '../types'
import FactorTestPage from './FactorTestPage.vue'

import * as factorApi from '../api/factorTest'
import * as formulaApi from '../api/formulaScreen'
import * as jobsApi from '../api/jobs'

vi.mock('../api/formulaScreen', async () => {
  const actual = await vi.importActual<typeof import('../api/formulaScreen')>('../api/formulaScreen')
  return { ...actual, fetchMetadata: vi.fn() }
})

vi.mock('../api/factorTest', async () => {
  const actual = await vi.importActual<typeof import('../api/factorTest')>('../api/factorTest')
  return {
    ...actual,
    createFactorTest: vi.fn(),
    getFactorTest: vi.fn(),
    getFactorTestResults: vi.fn(),
    fetchFactorTestHistory: vi.fn(),
    getFactorTestHistory: vi.fn(),
    deleteFactorTestHistory: vi.fn(),
  }
})

vi.mock('../api/jobs', async () => {
  const actual = await vi.importActual<typeof import('../api/jobs')>('../api/jobs')
  return { ...actual, fetchActiveJobs: vi.fn() }
})

const metadata: FormulaScreenMetadata = {
  indicators: [{
    id: 'technical',
    display_name: '技术指标',
    minimum_bars: 1,
    recommended_bars: 268,
    signals: [],
    values: [
      { id: 'technical.rsi14', display_name: 'RSI14', description: '相对强弱', rankable: true },
      { id: 'technical.bias20', display_name: '乖离率', description: '偏离均线', rankable: true },
      { id: 'technical.ma20', display_name: '20 日均线', description: '价格量纲', rankable: false },
    ],
  }],
  combine_modes: [{ value: 'all', label: '全部满足' }],
  condition_operators: [{ value: 'gt', label: '大于' }],
  supported_markets: ['SH', 'SZ'],
  supported_universe: [{ value: 'all', label: '沪深 A 股' }],
  periods: [{ value: 'daily', label: '日线' }],
  data_directory_help: '本地行情',
}

function layer(index: number, meanReturn: number) {
  return { index, size: 40, mean_return: meanReturn, beat_market_rate: 0.6 }
}

const report: FactorTestReport = {
  universe: 'all',
  stock_count: 2000,
  scanned_stocks: 1980,
  value_count: 2,
  horizon: 5,
  min_cross_section: 20,
  permutations: 200,
  block_size: 20,
  alpha: 0.05,
  quantile_count: 5,
  date_range: { start: '2024-01-02', end: '2024-12-31', days: 240 },
  dates: ['2024-01-02', '2024-01-03'],
  factors: [
    {
      value_id: 'technical.rsi14',
      display_name: 'RSI14',
      order: 'high',
      ic_mean: 0.061,
      icir: 0.72,
      ic_days: 240,
      ic_positive_rate: 0.71,
      ic_series: [0.02, 0.05],
      ic_cumulative: [0.02, 0.07],
      ic_first_half: 0.058,
      ic_second_half: 0.064,
      layers: [layer(1, 0.004), layer(2, 0.001), layer(3, -0.0004), layer(4, -0.002), layer(5, -0.005)],
      layer_inversions: 0,
      long_short: {
        mean_spread: 0.009,
        std: 0.02,
        t_stat: 4.1,
        win_rate: 0.68,
        final: 0.83,
        max_drawdown: 0.12,
        days: 240,
        cumulative: [0.004, 0.009],
      },
      ic_threshold: 0.012,
      pvalue: 0.005,
      qvalue: 0.01,
      significant: true,
      note: '平均信息系数 0.061、信息比率 0.72，基于 240 个交易日；检验通过（p=0.005）且强度可观',
    },
    {
      value_id: 'technical.bias20',
      display_name: '乖离率',
      order: 'low',
      ic_mean: 0.008,
      icir: 0.11,
      ic_days: 240,
      ic_positive_rate: 0.53,
      ic_series: [0.01, null],
      ic_cumulative: [0.01, 0.01],
      // 后端存绝对值：符号已经用在「买高还是买低」上了。
      ic_first_half: 0.004,
      ic_second_half: 0.012,
      layers: [layer(1, 0.0002), layer(2, 0.0001), layer(3, 0.0003), layer(4, -0.0002), layer(5, -0.0005)],
      layer_inversions: 2,
      long_short: {
        mean_spread: 0.0007,
        std: 0.019,
        t_stat: 0.4,
        win_rate: 0.51,
        final: 0.05,
        max_drawdown: 0.09,
        days: 240,
        cumulative: [0.0004, 0.0007],
      },
      ic_threshold: 0.011,
      pvalue: 0.31,
      qvalue: 0.42,
      significant: false,
      note: '平均信息系数 0.008、信息比率 0.11，基于 240 个交易日；噪声门槛检验 p=0.310，无法排除偶然',
    },
  ],
  significant_count: 1,
  reason: null,
  boundaries: [
    '标签是收盘价到收盘价的前瞻收益，不含交易成本、滑点、停牌、涨跌停与流动性约束',
    '没有样本外切分：方向是看全样本的符号定的',
  ],
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(formulaApi.fetchMetadata).mockResolvedValue(metadata)
  vi.mocked(factorApi.createFactorTest).mockResolvedValue({ job_id: 'job-1', status: 'queued' })
  vi.mocked(factorApi.getFactorTest).mockResolvedValue({
    job_id: 'job-1',
    status: 'completed',
    progress: 1,
    error: null,
    result: null,
  })
  vi.mocked(factorApi.getFactorTestResults).mockResolvedValue(report)
  vi.mocked(factorApi.fetchFactorTestHistory).mockResolvedValue([])
  vi.mocked(jobsApi.fetchActiveJobs).mockResolvedValue([])
})

describe('FactorTestPage', () => {
  it('submits the horizon and the selected values, then renders the factor table', async () => {
    const wrapper = mount(FactorTestPage)
    await flushPromises()

    // 只列可跨股票比较的输出：均线是价格量纲，排序它只会重新发现"有的股票更贵"。
    expect(wrapper.findAll('[data-testid^="factor-value-"]')).toHaveLength(2)
    expect(wrapper.find('[data-testid="factor-value-technical.ma20"]').exists()).toBe(false)

    await wrapper.get('[data-testid="factor-horizon"]').setValue(10)
    await wrapper.get('[data-testid="factor-config"]').trigger('submit')
    await flushPromises()

    expect(factorApi.createFactorTest).toHaveBeenCalledWith(expect.objectContaining({
      horizon: 10,
      value_ids: ['technical.rsi14', 'technical.bias20'],
      permutations: 200,
      quantile_count: 5,
    }))

    const results = wrapper.get('[data-testid="factor-results"]')
    expect(results.text()).toContain('RSI14')
    expect(results.text()).toContain('乖离率')
    expect(results.text()).toContain('买最高一组')
    expect(results.text()).toContain('买最低一组')
    expect(wrapper.get('[data-testid="factor-significant-count"]').text()).toBe('1 / 2')
    // 「通过校正」只标记在真正活下来的那几行上，不是给整张表盖个章。
    expect(wrapper.findAll('[data-testid="factor-significant-badge"]')).toHaveLength(1)
  })

  it('writes the boundaries on the page instead of leaving them implied', async () => {
    const wrapper = mount(FactorTestPage)
    await flushPromises()
    await wrapper.get('[data-testid="factor-config"]').trigger('submit')
    await flushPromises()

    const boundaries = wrapper.get('[data-testid="factor-boundaries"]').text()
    expect(boundaries).toContain('不含交易成本')
    expect(boundaries).toContain('没有样本外切分')
  })

  it('shows the selected factor layers and marks monotone ones', async () => {
    const wrapper = mount(FactorTestPage)
    await flushPromises()
    await wrapper.get('[data-testid="factor-config"]').trigger('submit')
    await flushPromises()

    expect(wrapper.get('[data-testid="factor-detail"]').text()).toContain('第 1 档')
    expect(wrapper.get('[data-testid="factor-layers"]').text()).toContain('日均 40 只')
    // 详情默认落在最强的一条上；切到另一条要换掉整张细节图。
    await wrapper.get('[data-testid="factor-pick-technical.bias20"]').trigger('click')
    expect(wrapper.get('[data-testid="factor-detail"]').text()).toContain('乖离率')
    expect(wrapper.text()).toContain('2 处倒挂')
  })

  it('restores the horizon and value selection from history', async () => {
    vi.mocked(factorApi.getFactorTestHistory).mockResolvedValue({
      history_id: 'h1',
      backtest_type: 'factor_test',
      request: { horizon: 20, value_ids: ['technical.rsi14'], min_cross_section: 30, permutations: 0 },
      result: report,
      started_at: '2026-09-21T00:00:00Z',
      finished_at: '2026-09-21T00:01:00Z',
      duration_seconds: 60,
    } as never)
    vi.mocked(factorApi.fetchFactorTestHistory).mockResolvedValue([{
      history_id: 'h1',
      backtest_type: 'factor_test',
      summary: {},
      started_at: '2026-09-21T00:00:00Z',
      finished_at: '2026-09-21T00:01:00Z',
      duration_seconds: 60,
    }] as never)

    const wrapper = mount(FactorTestPage)
    await flushPromises()
    await wrapper.get('[data-testid="factor-history-load-h1"]').trigger('click')
    await flushPromises()

    expect((wrapper.get('[data-testid="factor-horizon"]').element as HTMLInputElement).value).toBe('20')
    expect((wrapper.get('[data-testid="factor-permutations"]').element as HTMLInputElement).value).toBe('0')
    expect(wrapper.text()).toContain('已加载历史因子检验 h1')
  })

  it('refuses to run without a value selected', async () => {
    const wrapper = mount(FactorTestPage)
    await flushPromises()
    await wrapper.get('[data-testid="factor-clear-values"]').trigger('click')
    await wrapper.get('[data-testid="factor-config"]').trigger('submit')
    await flushPromises()

    expect(factorApi.createFactorTest).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('请至少选择一个指标输出')
  })
})
