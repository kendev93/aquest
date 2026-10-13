import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type {
  BacktestHistoryRecord,
  BacktestHistorySummary,
  FactorAttributionReport,
  FormulaScreenMetadata,
} from '../types'
import FactorAttributionPage from './FactorAttributionPage.vue'

import { PORTFOLIO_BACKTEST_HANDOFF_KEY } from '../utils/portfolioBacktestHandoff'

import * as attributionApi from '../api/factorAttribution'
import * as formulaApi from '../api/formulaScreen'
import * as jobsApi from '../api/jobs'
import { ApiError } from '../api/client'
import { syncPath } from '../router'

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
      new ApiError('这条回测记录没有成交明细，无法归因', 422),
    )
    const wrapper = mount(FactorAttributionPage)
    await flushPromises()
    await wrapper.get('[data-testid="attribution-source"]').setValue('hist-abcdef')
    await wrapper.get('[data-testid="attribution-config"]').trigger('submit')
    await flushPromises()

    expect(wrapper.get('[data-testid="attribution-message"]').text()).toContain('没有成交明细')
  })

  it('marks the current page in the navigation', async () => {
    // 导航高亮现在由地址决定，不再由页面自己写死。
    window.history.replaceState({}, '', '/factor-attribution')
    syncPath()

    const wrapper = mount(FactorAttributionPage)
    await flushPromises()

    const active = wrapper.get('a[href="/factor-attribution"]')
    expect(active.classes()).toContain('active')
    expect(active.attributes('aria-current')).toBe('page')
  })

  it('lets the operator drop, re-add and bulk-pick the factors under test', async () => {
    const wrapper = mount(FactorAttributionPage)
    await flushPromises()
    const rsi = wrapper.get('[data-testid="attribution-value-technical.rsi14"]')
    const bias = wrapper.get('[data-testid="attribution-value-technical.bias20"]')
    // 挂载时默认全选技术指标里可排名的那些（ma20 rankable=false，根本不进这个列表）。
    expect((rsi.element as HTMLInputElement).checked).toBe(true)
    expect((bias.element as HTMLInputElement).checked).toBe(true)

    await rsi.trigger('change')
    expect((rsi.element as HTMLInputElement).checked).toBe(false)
    await wrapper.get('[data-testid="attribution-clear-values"]').trigger('click')
    expect((bias.element as HTMLInputElement).checked).toBe(false)
    await rsi.trigger('change')
    expect((rsi.element as HTMLInputElement).checked).toBe(true)
    await wrapper.get('[data-testid="attribution-select-technical"]').trigger('click')
    expect((bias.element as HTMLInputElement).checked).toBe(true)
  })

  it('names every out-of-range parameter instead of silently clamping', async () => {
    const wrapper = mount(FactorAttributionPage)
    await flushPromises()
    await wrapper.get('[data-testid="attribution-source"]').setValue('hist-abcdef')
    await wrapper.get('[data-testid="attribution-min-group"]').setValue('1')
    await wrapper.get('[data-testid="attribution-permutations"]').setValue('99999')
    await wrapper.get('[data-testid="attribution-block"]').setValue('0')
    await wrapper.get('[data-testid="attribution-alpha"]').setValue('1')
    await wrapper.get('[data-testid="attribution-config"]').trigger('submit')
    await flushPromises()

    const text = wrapper.text()
    expect(text).toContain('每组最少成交笔数至少为 2')
    expect(text).toContain('0 到 5000')
    expect(text).toContain('显著性水平必须介于 0 与 1 之间')
    expect(text).toContain('置换块长度至少为 1')
    expect(attributionApi.createFactorAttribution).not.toHaveBeenCalled()
  })

  it('replays a stored run into the form and can delete it', async () => {
    vi.mocked(attributionApi.fetchFactorAttributionHistory).mockResolvedValue([
      {
        history_id: 'hist-old',
        backtest_type: 'portfolio',
        created_at: '2024-05-01T01:00:00+00:00',
        started_at: '2024-05-01T01:00:00+00:00',
        finished_at: '2024-05-01T02:00:00+00:00',
        duration_seconds: 3600,
        summary: {
          start_date: '2024-01-02',
          end_date: '2024-04-30',
          performance: { total_return: 0.12, sharpe: 1.1 },
        },
      } as unknown as BacktestHistorySummary,
    ])
    vi.mocked(attributionApi.getFactorAttributionHistory).mockResolvedValue({
      history_id: 'hist-old',
      request: {
        source_history_id: 'hist-old',
        // 非字符串项是脏数据，不该被搬进表单。
        value_ids: ['technical.bias20', 42],
        include_custom: true,
        min_group_trades: 3,
        permutations: 100,
        block_size: 10,
        alpha: 0.1,
      },
      result: report,
    } as unknown as BacktestHistoryRecord<FactorAttributionReport>)
    vi.mocked(attributionApi.deleteFactorAttributionHistory).mockResolvedValue({ deleted: true })

    const wrapper = mount(FactorAttributionPage)
    await flushPromises()
    await wrapper.get('[data-testid="attribution-history-load-hist-old"]').trigger('click')
    await flushPromises()

    // 还原的是"当时那份输入"，所以提示要求重新运行，而不是假装报告已更新。
    expect(wrapper.get('[data-testid="attribution-message"]').text()).toContain(
      '已还原历史因子归因 hist-old',
    )
    expect((wrapper.get('[data-testid="attribution-value-technical.bias20"]').element as HTMLInputElement)
      .checked).toBe(true)
    expect((wrapper.get('[data-testid="attribution-include-custom"]').element as HTMLInputElement)
      .checked).toBe(true)
    expect((wrapper.get('[data-testid="attribution-alpha"]').element as HTMLInputElement).value)
      .toBe('0.1')

    await wrapper.get('[data-testid="attribution-history-remove-hist-old"]').trigger('click')
    await flushPromises()
    expect(attributionApi.deleteFactorAttributionHistory).toHaveBeenCalledWith('hist-old')
    expect(wrapper.get('[data-testid="attribution-message"]').text()).toContain('历史因子归因已删除')
  })

  it('hands a rankable factor to the portfolio page with the sample verdict attached', async () => {
    sessionStorage.clear()
    const wrapper = mount(FactorAttributionPage)
    await flushPromises()
    await wrapper.get('[data-testid="attribution-source"]').setValue('hist-abcdef')
    await wrapper.get('[data-testid="attribution-config"]').trigger('submit')
    await flushPromises()

    await wrapper.get('[data-testid="attribution-to-portfolio-technical.rsi14"]').trigger('click')

    const raw = sessionStorage.getItem(PORTFOLIO_BACKTEST_HANDOFF_KEY)
    expect(raw).not.toBeNull()
    expect(JSON.parse(raw as string)).toMatchObject({
      kind: 'ranking',
      source: 'factor-attribution',
      value_id: 'technical.rsi14',
      // 胜组均值高于败组 -> 按降序取用。
      rank_order: 'desc',
    })
    expect(JSON.parse(raw as string).evidence).toContain('通过校正')
  })

  it('picks up an attribution job left running by an earlier session', async () => {
    vi.mocked(jobsApi.fetchActiveJobs).mockResolvedValue([
      {
        job_id: 'job-9',
        description: '因子归因',
        status: 'running',
        progress: 0.4,
      } as unknown as Awaited<ReturnType<typeof jobsApi.fetchActiveJobs>>[number],
    ])
    vi.mocked(attributionApi.getFactorAttribution).mockResolvedValue({
      job_id: 'job-9',
      status: 'completed',
      progress: 1,
      error: null,
      result: null,
    })

    const wrapper = mount(FactorAttributionPage)
    await flushPromises()

    expect(attributionApi.getFactorAttribution).toHaveBeenCalledWith('job-9')
    expect(wrapper.get('[data-testid="attribution-trades"]').text()).toBe('96')
  })

  it('says so when the running-job lookup itself fails', async () => {
    vi.mocked(jobsApi.fetchActiveJobs).mockRejectedValue(new Error('boom'))

    const wrapper = mount(FactorAttributionPage)
    await flushPromises()

    expect(wrapper.get('[data-testid="attribution-message"]').text()).toContain('因子归因失败')
  })

  it('exports the finished report as a JSON file', async () => {
    const createObjectURL = vi.fn().mockReturnValue('blob:report')
    const revokeObjectURL = vi.fn()
    vi.stubGlobal('URL', { createObjectURL, revokeObjectURL })
    try {
      const wrapper = mount(FactorAttributionPage)
      await flushPromises()
      await wrapper.get('[data-testid="attribution-source"]').setValue('hist-abcdef')
      await wrapper.get('[data-testid="attribution-config"]').trigger('submit')
      await flushPromises()

      await wrapper.get('[data-testid="export-attribution"]').trigger('click')
      expect(createObjectURL).toHaveBeenCalledTimes(1)
      // 报告本体进了 Blob，URL 在下一个宏任务里 revoke 掉，不留着占内存。
      await new Promise((resolve) => { window.setTimeout(resolve, 1) })
      expect(revokeObjectURL).toHaveBeenCalledWith('blob:report')
    } finally {
      vi.unstubAllGlobals()
    }
  })
})
