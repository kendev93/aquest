import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { BacktestHistorySummary, FactorAttributionReport, FormulaScreenMetadata } from '../types'
import FactorAttributionPage from './FactorAttributionPage.vue'

import * as attributionApi from '../api/factorAttribution'
import * as formulaApi from '../api/formulaScreen'
import * as jobsApi from '../api/jobs'
import { FormulaScreenApiError } from '../api/formulaScreen'

vi.mock('../api/formulaScreen', async () => {
  const actual = await vi.importActual<typeof import('../api/formulaScreen')>('../api/formulaScreen')
  return { ...actual, fetchMetadata: vi.fn() }
})

vi.mock('../api/factorAttribution', async () => {
  const actual = await vi.importActual<typeof import('../api/factorAttribution')>(
    '../api/factorAttribution',
  )
  return {
    ...actual,
    createFactorAttribution: vi.fn(),
    getFactorAttribution: vi.fn(),
    getFactorAttributionResults: vi.fn(),
    fetchAttributionSources: vi.fn(),
    fetchFactorAttributionHistory: vi.fn(),
    getFactorAttributionHistory: vi.fn(),
    deleteFactorAttributionHistory: vi.fn(),
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

const source: BacktestHistorySummary = {
  history_id: 'hist-abcdef',
  backtest_type: 'portfolio',
  finished_at: '2024-06-01T02:00:00+00:00',
  summary: {},
} as unknown as BacktestHistorySummary

const report: FactorAttributionReport = {
  source: {
    history_id: 'hist-abcdef',
    backtest_type: 'portfolio',
    finished_at: '2024-06-01T02:00:00+00:00',
    date_range: { start: '2024-01-02', end: '2024-12-31' },
  },
  samples: {
    trades: 96,
    wins: 48,
    losses: 48,
    open_positions: 3,
    orphan_sells: 0,
    rejected: 1,
    unparsable: 0,
    signal_days: 96,
    stocks: 12,
    holding_days: 6.5,
    win_holding_days: 7.0,
    loss_holding_days: 6.0,
    missing_stocks: [],
  },
  value_count: 2,
  significant_count: 1,
  permutations: 200,
  block_size_days: 20,
  block_size_trades: 20,
  alpha: 0.05,
  factors: [
    {
      value_id: 'technical.rsi14',
      display_name: 'RSI14',
      win_mean: 61.2,
      loss_mean: 48.4,
      difference: 12.8,
      standardized: 1.4,
      win_count: 48,
      loss_count: 48,
      missing: 0,
      p_value: 0.005,
      adjusted_p_value: 0.01,
      significant: true,
      note: null,
    },
    {
      value_id: 'technical.bias20',
      display_name: '乖离率',
      win_mean: 0.01,
      loss_mean: -0.004,
      difference: 0.014,
      standardized: 0.2,
      win_count: 48,
      loss_count: 48,
      missing: 2,
      p_value: 0.31,
      adjusted_p_value: 0.42,
      significant: false,
      note: null,
    },
  ],
  notes: ['期末还有 3 笔未平仓，没有已实现收益，不计入胜负'],
  boundaries: ['收益是净口径：pnl 已扣双边佣金、印花税与滑点', '胜 / 败是事后标签'],
  reason: '2 个指标里有 1 个通过多重检验校正；区分最强的是「RSI14」：胜组均值高于败组约 12.8',
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(formulaApi.fetchMetadata).mockResolvedValue(metadata)
  vi.mocked(attributionApi.fetchAttributionSources).mockResolvedValue([source])
  vi.mocked(attributionApi.fetchFactorAttributionHistory).mockResolvedValue([])
  vi.mocked(jobsApi.fetchActiveJobs).mockResolvedValue([])
  vi.mocked(attributionApi.createFactorAttribution).mockResolvedValue({
    job_id: 'job-1',
    status: 'queued',
  })
  vi.mocked(attributionApi.getFactorAttribution).mockResolvedValue({
    job_id: 'job-1',
    status: 'completed',
    progress: 1,
    error: null,
    result: null,
  })
  vi.mocked(attributionApi.getFactorAttributionResults).mockResolvedValue(report)
})

describe('FactorAttributionPage', () => {
  it('offers the archived backtests as sources and sends the chosen one', async () => {
    const wrapper = mount(FactorAttributionPage)
    await flushPromises()

    const options = wrapper.findAll('[data-testid="attribution-source"] option')
    expect(options).toHaveLength(2)
    expect(options[1].attributes('value')).toBe('hist-abcdef')

    await wrapper.get('[data-testid="attribution-source"]').setValue('hist-abcdef')
    await wrapper.get('[data-testid="attribution-config"]').trigger('submit')
    await flushPromises()

    expect(attributionApi.createFactorAttribution).toHaveBeenCalledWith(
      expect.objectContaining({
        source_history_id: 'hist-abcdef',
        value_ids: ['technical.rsi14', 'technical.bias20'],
        permutations: 200,
      }),
    )
  })

  it('refuses to run without saying which backtest to explain', async () => {
    const wrapper = mount(FactorAttributionPage)
    await flushPromises()

    await wrapper.get('[data-testid="attribution-config"]').trigger('submit')
    await flushPromises()

    expect(attributionApi.createFactorAttribution).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('请先选择一条回测记录')
  })

  it('renders the sample shape, the verdict and the factor table', async () => {
    const wrapper = mount(FactorAttributionPage)
    await flushPromises()
    await wrapper.get('[data-testid="attribution-source"]').setValue('hist-abcdef')
    await wrapper.get('[data-testid="attribution-config"]').trigger('submit')
    await flushPromises()

    expect(wrapper.get('[data-testid="attribution-trades"]').text()).toBe('96')
    expect(wrapper.get('[data-testid="attribution-significant-count"]').text()).toBe('1 / 2')
    expect(wrapper.get('[data-testid="attribution-reason"]').text()).toContain('RSI14')

    const row = wrapper.get('[data-testid="attribution-row-technical.rsi14"]').text()
    expect(row).toContain('61.200')
    expect(row).toContain('48.400')
    expect(row).toContain('12.800')
    // 「通过校正」只标记在活下来的那一行上，不是给整张表盖章。
    expect(wrapper.findAll('[data-testid="attribution-significant-badge"]')).toHaveLength(1)
    expect(wrapper.get('[data-testid="attribution-row-technical.bias20"]').text()).toContain('不显著')
  })

  it('reports open positions instead of quietly treating them as losses', async () => {
    const wrapper = mount(FactorAttributionPage)
    await flushPromises()
    await wrapper.get('[data-testid="attribution-source"]').setValue('hist-abcdef')
    await wrapper.get('[data-testid="attribution-config"]').trigger('submit')
    await flushPromises()

    const notes = wrapper.get('[data-testid="attribution-notes"]').text()
    expect(notes).toContain('3 笔未平仓')
    expect(notes).toContain('不计入胜负')
  })

  it('writes the boundaries on the page instead of leaving them implied', async () => {
    const wrapper = mount(FactorAttributionPage)
    await flushPromises()
    await wrapper.get('[data-testid="attribution-source"]').setValue('hist-abcdef')
    await wrapper.get('[data-testid="attribution-config"]').trigger('submit')
    await flushPromises()

    const boundaries = wrapper.get('[data-testid="attribution-boundaries"]').text()
    expect(boundaries).toContain('净口径')
    expect(boundaries).toContain('事后标签')
  })

  it('shows the failure message when the history has no trades', async () => {
    // 服务端的可读文案只在 ApiError 里传出去，普通 Error 会被兜底成通用提示。
    vi.mocked(attributionApi.createFactorAttribution).mockRejectedValue(
      new FormulaScreenApiError('这条回测记录没有成交明细，无法归因', 422),
    )
    const wrapper = mount(FactorAttributionPage)
    await flushPromises()
    await wrapper.get('[data-testid="attribution-source"]').setValue('hist-abcdef')
    await wrapper.get('[data-testid="attribution-config"]').trigger('submit')
    await flushPromises()

    expect(wrapper.get('[data-testid="attribution-message"]').text()).toContain('没有成交明细')
  })

  it('marks the current page in the navigation', async () => {
    const wrapper = mount(FactorAttributionPage)
    await flushPromises()

    const active = wrapper.get('a[href="/factor-attribution"]')
    expect(active.classes()).toContain('active')
    expect(active.attributes('aria-current')).toBe('page')
  })
})
