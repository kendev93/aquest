import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import * as api from '../api/strategyPool'
import * as marketDataApi from '../api/marketData'

import type {
  StrategyPoolCandidate,
  StrategyPoolEntry,
  StrategyPoolJobSummary,
  StrategyPoolMetrics,
  StrategyPoolSignal,
} from '../types'
import StrategyPoolPage from './StrategyPoolPage.vue'
import { ApiError } from '../api/client'

vi.mock('../api/strategyPool', () => ({
  addStrategyPoolFocus: vi.fn(),
  cancelStrategyPoolJob: vi.fn(),
  clearStrategyPoolOverride: vi.fn(),
  createStrategyPoolJob: vi.fn(),
  fetchResearchSettings: vi.fn(),
  fetchPoolSignalDays: vi.fn(),
  fetchStrategyPoolEntries: vi.fn(),
  fetchStrategyPoolFocus: vi.fn(),
  fetchStrategyPoolJobs: vi.fn(),
  fetchStrategyPoolResearchEstimate: vi.fn(),
  fetchStrategyPoolJobStats: vi.fn(),
  getStrategyPoolJob: vi.fn(),
  removeStrategyPoolFocus: vi.fn(),
  saveResearchSettings: vi.fn(),
  saveStrategyPoolOverride: vi.fn(),
}))

vi.mock('../api/marketData', () => ({ fetchStoreStatus: vi.fn() }))

const baseCandidate: StrategyPoolCandidate = {
  candidate_id: 'candidate-1',
  buy_rule: { signals: ['technical.macd'] },
  sell_rule: { signals: ['technical.rsi14'] },
  config: { stop_loss_pct: 0.08 },
  metrics: {
    validation: {
      annual_return: 0.24,
      total_return: 0.24,
      sharpe: 1.35,
      win_rate: 0.62,
      payoff_ratio: 1.8,
      max_drawdown: -0.18,
      total_trades: 12,
    },
    holdout: {
      total_return: 0.08,
      max_drawdown: -0.1,
      closed_trades: 4,
    },
    full_period: {
      total_return: 0.71,
      annual_return: 0.19,
      sharpe: 1.25,
      win_rate: 0.55,
      payoff_ratio: 1.6,
      max_drawdown: -0.22,
      avg_holding_days: 12.5,
    },
  },
  quality: { badges: ['样本较充分'] },
}

function entry(code: string, score: number): StrategyPoolEntry {
  const candidate: StrategyPoolCandidate = {
    ...baseCandidate,
    candidate_id: `candidate-${code}`,
    metrics: {
      full_period: {
        ...baseCandidate.metrics?.full_period,
        total_return: score * 3,
      },
      validation: {
        ...baseCandidate.metrics?.validation,
        annual_return: score,
        sharpe: score * 2,
      },
      holdout: baseCandidate.metrics?.holdout,
    },
  }
  return {
    market: 'SH',
    code,
    name: `测试${code}`,
    manual_overrides: { annual_return: null, sharpe: null },
    active_candidate: candidate,
    active_source: 'automatic',
    status: 'ready',
    benchmark: {
      label: '个股买入持有',
      full_period: { total_return: 0.5, annual_return: 0.2, max_drawdown: -0.2, bars: 300 },
      validation: { total_return: 0.1, annual_return: 0.08, max_drawdown: -0.12, bars: 60 },
      holdout: { total_return: -0.05, annual_return: -0.18, max_drawdown: -0.2, bars: 60 },
    },
  }
}

const buySignal: StrategyPoolSignal = {
  market: 'SH',
  code: '600000',
  name: '测试600000',
  symbol: 'SH600000',
  status: 'buy',
  signal_type: 'buy',
  candidate_id: 'candidate-600000',
  total_return: 0.24,
  annual_return: 0.24,
  sharpe: 1.35,
  win_rate: 0.62,
  payoff_ratio: 1.8,
  max_drawdown: -0.18,
  full_period_return: 0.71,
  holdout_return: 0.08,
  validation_trades: 12,
  holdout_trades: 4,
}

function signalFor(code: string): StrategyPoolSignal {
  return { ...buySignal, code, name: `测试${code}`, symbol: `SH${code}`, candidate_id: `candidate-${code}` }
}

function entryWithQuality(code: string, fullPeriod: Partial<StrategyPoolMetrics>): StrategyPoolEntry {
  const base = entry(code, 0.2)
  const active = base.active_candidate as StrategyPoolCandidate
  const candidate: StrategyPoolCandidate = {
    ...active,
    metrics: { ...active.metrics, full_period: { ...active.metrics?.full_period, ...fullPeriod } },
  }
  return { ...base, active_candidate: candidate }
}

describe('StrategyPoolPage', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    window.localStorage.clear()
    vi.mocked(api.fetchStrategyPoolEntries).mockResolvedValue([entry('600000', 0.24), entry('600001', 0.12)])
    vi.mocked(api.fetchStrategyPoolJobs).mockResolvedValue([{ job_id:'signal-1',mode:'signals',status:'completed',objective:'annual_return',dual_objectives:true,created_at:Date.now()/1000 }])
    vi.mocked(marketDataApi.fetchStoreStatus).mockResolvedValue({
      database_path: 'market.duckdb',
      schema_version: 1,
      instrument_count: 2,
      bar_count: 300,
      data_start: '2025-01-01',
      data_end: '2026-10-01',
      last_online_sync_at: '2026-10-01T18:00:00+08:00',
    })
    vi.mocked(api.createStrategyPoolJob).mockResolvedValue({ job_id: 'signal-1', status: 'queued' })
    vi.mocked(api.fetchResearchSettings).mockResolvedValue({
      boards: ['main', 'chinext', 'star'],
      exclude_st: false,
    })
    vi.mocked(api.saveResearchSettings).mockImplementation(async (payload) => payload)
    vi.mocked(api.fetchStrategyPoolFocus).mockResolvedValue([])
    // 关注接口都返回服务端权威名单；默认模拟"只关注了这一只"。
    vi.mocked(api.addStrategyPoolFocus).mockImplementation(async (market, code, name) => [
      { market: market as 'SH' | 'SZ', code, name },
    ])
    vi.mocked(api.removeStrategyPoolFocus).mockResolvedValue([])
    vi.mocked(api.getStrategyPoolJob).mockResolvedValue({
      job_id: 'signal-1',
      status: 'completed',
      result: {
        mode: 'signals',
        objective: 'annual_return',
        holding_scope: 'per_stock_strategy',
        holding_snapshot_complete: true,
        as_of_date: '2026-10-01',
        buy_signals: [buySignal],
        sell_signals: [],
        states: [buySignal],
        model_portfolio: {
          available: true,
          holding_scope: 'per_stock_strategy',
          holdings: [],
          risk_exit_signals: [],
        },
      },
    })
    vi.mocked(api.saveStrategyPoolOverride).mockResolvedValue(entry('600000', 0.24))
  })

  it('requires a first objective choice, persists it, and shows current signal groups', async () => {
    const wrapper = mount(StrategyPoolPage)

    expect(wrapper.get('[data-testid="strategy-pool-first-choice"]').text()).toContain('关注年化收益')
    await wrapper.get('[data-testid="pool-select-annual"]').trigger('click')
    await flushPromises()

    expect(window.localStorage.getItem('aquest.strategy-pool.objective')).toBe('annual_return')
    expect(api.createStrategyPoolJob).not.toHaveBeenCalled()
    expect(wrapper.get('[data-testid="pool-buy-signals"]').text()).toContain('SH600000')
    expect(wrapper.get('[data-testid="pool-sell-signals"]').text()).toContain('当前没有卖出条件')
  })

  it('shows full-period quality metrics on buy, sell and holding rows', async () => {
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    vi.mocked(api.getStrategyPoolJob).mockResolvedValue({
      job_id: 'signal-1',
      status: 'completed',
      result: {
        mode: 'signals',
        objective: 'annual_return',
        holding_scope: 'per_stock_strategy',
        holding_snapshot_complete: true,
        as_of_date: '2026-10-01',
        buy_signals: [buySignal],
        sell_signals: [{ ...buySignal, signal_type: 'sell', status: 'sell' }],
        states: [buySignal],
        model_portfolio: {
          available: true,
          holding_scope: 'per_stock_strategy',
          holdings: [
            {
              market: 'SH',
              code: '600000',
              name: '测试600000',
              status: 'held',
              holding_return_pct: 0.05,
            },
          ],
          risk_exit_signals: [],
        },
      },
    })
    const wrapper = mount(StrategyPoolPage)
    await flushPromises()

    // 质量指标统一取条目的全区间口径（与表格一致），跟踪来源的行也能显示
    expect(wrapper.get('[data-testid="pool-buy-signals"]').text()).toContain('全区间胜率 55.00%')
    expect(wrapper.get('[data-testid="pool-sell-signals"]').text()).toContain('全区间盈亏比 1.60')
    expect(wrapper.get('[data-testid="pool-current-holdings"]').text()).toContain('平均持仓 12.5 天')
  })

  it('sorts buy and sell signal rows by full-period payoff ratio and win rate', async () => {
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    // 跟踪增量路径返回的信号行不带指标，顺序是证券代码升序；排序只能靠条目表补全区间口径。
    // SH600003 没有全区间指标，按指标排序时落到末尾。
    vi.mocked(api.fetchStrategyPoolEntries).mockResolvedValue([
      entryWithQuality('600000', { win_rate: 0.4, payoff_ratio: 3, total_return: 0.1 }),
      entryWithQuality('600001', { win_rate: 0.7, payoff_ratio: 1, total_return: 0.3 }),
      entryWithQuality('600002', { win_rate: 0.55, payoff_ratio: 2, total_return: 0.2 }),
    ])
    const codes = ['600003', '600000', '600001', '600002']
    vi.mocked(api.getStrategyPoolJob).mockResolvedValue({
      job_id: 'signal-1',
      status: 'completed',
      result: {
        mode: 'signals',
        objective: 'annual_return',
        holding_scope: 'per_stock_strategy',
        holding_snapshot_complete: true,
        as_of_date: '2026-10-01',
        buy_signals: codes.map(signalFor),
        sell_signals: codes.map((code) => ({ ...signalFor(code), status: 'sell', signal_type: 'sell' })),
        states: codes.map((code) => ({ ...signalFor(code), status: 'flat' })),
        model_portfolio: {
          available: true,
          holding_scope: 'per_stock_strategy',
          holdings: [],
          risk_exit_signals: [],
        },
      },
    })
    const wrapper = mount(StrategyPoolPage)
    await flushPromises()

    const rowSymbols = (testid: string) => wrapper
      .findAll(`[data-testid="${testid}"] .signal-row`)
      .map((row) => row.text().slice(0, 8))
    const returned = ['SH600003', 'SH600000', 'SH600001', 'SH600002']
    expect(rowSymbols('pool-buy-signals')).toEqual(returned)

    const sort = wrapper.get('[data-testid="pool-signal-sort"]')
    await sort.setValue('payoff_ratio')
    expect(rowSymbols('pool-buy-signals')).toEqual(['SH600000', 'SH600002', 'SH600001', 'SH600003'])
    expect(rowSymbols('pool-sell-signals')).toEqual(['SH600000', 'SH600002', 'SH600001', 'SH600003'])

    await sort.setValue('win_rate')
    expect(rowSymbols('pool-buy-signals')).toEqual(['SH600001', 'SH600002', 'SH600000', 'SH600003'])

    await sort.setValue('total_return')
    expect(rowSymbols('pool-buy-signals')).toEqual(['SH600001', 'SH600002', 'SH600000', 'SH600003'])

    await sort.setValue('default')
    expect(rowSymbols('pool-buy-signals')).toEqual(returned)
  })

  it('switches the saved objective and lets metric sorting change only the table view', async () => {
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    const wrapper = mount(StrategyPoolPage)
    await flushPromises()

    await wrapper.get('[data-testid="pool-objective"]').setValue('sharpe')
    await flushPromises()
    expect(window.localStorage.getItem('aquest.strategy-pool.objective')).toBe('sharpe')
    expect(api.fetchStrategyPoolEntries).toHaveBeenCalledWith('sharpe')

    await wrapper.get('thead button').trigger('click')
    expect(wrapper.findAll('tbody tr')[0]?.text()).toContain('600000')
  })

  it('ranks post-launch returns off the same objective-scoped list the table reads', async () => {
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    const withWindow = (code: string, post: number, benchmark: number) => ({
      ...entry(code, 0.2),
      tracking: { version_id: `v-${code}` },
      tracking_status: 'ready',
      tracking_as_of_date: '2026-10-01',
      created_return: post / 2,
      post_launch_return: post,
      post_launch_benchmark_return: benchmark,
      post_launch_days: 40,
      post_launch_has_data: true,
    })
    vi.mocked(api.fetchStrategyPoolEntries).mockImplementation(async (objective) =>
      objective === 'sharpe'
        ? [withWindow('600009', 0.02, 0.3)]
        : [withWindow('600000', 0.3, 0.1), withWindow('600001', 0.5, -0.2)])

    const wrapper = mount(StrategyPoolPage)
    await flushPromises()

    const board = wrapper.get('[data-testid="pool-post-launch-board"]')
    expect(board.findAll('tbody tr')[0].text()).toContain('600001')
    expect(board.text()).toContain('50.00%')
    expect(board.text()).toContain('+70.00%')
    // 位置：特别关注之后、全市场状态表之前——它是概览，不是另一张全量清单。
    expect(
      wrapper
        .findAll('[data-testid="pool-focus-section"], [data-testid="pool-post-launch-board"], [data-testid="pool-entry-table-section"]')
        .map((node) => node.attributes('data-testid')),
    ).toEqual(['pool-focus-section', 'pool-post-launch-board', 'pool-entry-table-section'])

    // 换目标要跟着换：上线后窗口是「当前生效版本」的，两个目标各自一条榜。
    await wrapper.get('[data-testid="pool-objective"]').setValue('sharpe')
    await flushPromises()
    expect(wrapper.get('[data-testid="pool-post-launch-board"]').findAll('tbody tr')[0].text()).toContain('600009')
  })

  it('cycles a metric column through 高→低, 低→高 and back to the status grouping', async () => {
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    const wrapper = mount(StrategyPoolPage)
    await flushPromises()

    // 全区间收益：600000 = 0.72，600001 = 0.36；默认档按状态分组，600000 触发买入在前。
    const rowCodes = () => wrapper
      .findAll('[data-testid^="pool-row-"]')
      .map((row) => row.attributes('data-testid')?.replace('pool-row-', ''))
    const sortButton = () => wrapper.get('[data-testid="pool-sort-strategy_return"]')

    expect(sortButton().text()).toBe('策略收益')
    expect(rowCodes()).toEqual(['600000', '600001'])

    await sortButton().trigger('click')
    expect(sortButton().text()).toBe('策略收益 ↓')
    expect(rowCodes()).toEqual(['600000', '600001'])

    await sortButton().trigger('click')
    expect(sortButton().text()).toBe('策略收益 ↑')
    expect(rowCodes()).toEqual(['600001', '600000'])

    await sortButton().trigger('click')
    expect(sortButton().text()).toBe('策略收益')
    expect(rowCodes()).toEqual(['600000', '600001'])
  })

  it('restores the ascending direction from the list query so 返回 keeps the column order', async () => {
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    window.history.replaceState({}, '', '/strategy-pool?objective=annual_return&sort=strategy_return&dir=asc')
    const wrapper = mount(StrategyPoolPage)
    await flushPromises()

    expect(wrapper.get('[data-testid="pool-sort-strategy_return"]').text()).toBe('策略收益 ↑')
    expect(wrapper.findAll('[data-testid^="pool-row-"]')[0]?.attributes('data-testid'))
      .toBe('pool-row-600001')
    // 进详情时把方向一起带过去，返回列表不会掉回高→低。
    expect(wrapper.get('[data-testid="pool-detail-600000"]').attributes('href')).toContain('dir%3Dasc')

    window.history.replaceState({}, '', '/strategy-pool')
  })

  it('searches the status table by code and Chinese stock name', async () => {
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    const wrapper = mount(StrategyPoolPage)
    await flushPromises()

    const search = wrapper.get('[data-testid="pool-entry-search"]')
    await search.setValue('600001')
    expect(wrapper.findAll('tbody tr[data-testid^="pool-row-"]')).toHaveLength(1)
    expect(wrapper.find('[data-testid="pool-row-600001"]')?.exists()).toBe(true)

    await search.setValue('测试600000')
    expect(wrapper.findAll('tbody tr[data-testid^="pool-row-"]')).toHaveLength(1)
    expect(wrapper.find('[data-testid="pool-row-600000"]')?.exists()).toBe(true)

    await search.setValue('不存在的股票')
    expect(wrapper.findAll('tbody tr[data-testid^="pool-row-"]')).toHaveLength(0)
    expect(wrapper.get('[data-testid="pool-entry-empty"]').text()).toContain('没有符合代码或名称的标的')
  })

  it('labels cached stocks with insufficient history instead of no strategy', async () => {
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    const insufficient: StrategyPoolEntry = {
      ...entry('600000', 0.24),
      active_candidate: null,
      status: 'insufficient_history',
      last_error: '历史样本不足：无法切出训练、验证、留出三段',
    }
    vi.mocked(api.fetchStrategyPoolEntries).mockResolvedValue([insufficient])

    const wrapper = mount(StrategyPoolPage)
    await flushPromises()

    expect(wrapper.get('[data-testid="pool-row-600000"]').text()).toContain('历史样本不足')
  })

  it('splits task history into research and signal-scan groups so long runs stay visible', async () => {
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    const rows = (mode: 'single' | 'signals', count: number): StrategyPoolJobSummary[] =>
      Array.from({ length: count }, (_, index) => ({
        job_id: `${mode}-${index}`,
        status: 'completed',
        mode,
        market: 'SH',
        code: `6000${String(index).padStart(2, '0')}`,
        created_at: index,
      }))
    vi.mocked(api.fetchStrategyPoolJobs).mockResolvedValue([
      ...rows('signals', 8),
      ...rows('single', 8),
    ])

    const wrapper = mount(StrategyPoolPage)
    await flushPromises()

    // 后台跟踪每十分钟写一条扫描记录：不分组的话十条里十条是同一句话的扫描，
    // 跑了几小时的全市场重选一条都看不见。
    const groups = wrapper.findAll('.history-group')
    expect(groups).toHaveLength(2)
    expect(groups[0]?.text()).toContain('研究与重跑任务')
    expect(groups[0]?.findAll('.history-row')).toHaveLength(4)
    expect(groups[1]?.text()).toContain('当天信号扫描')
    expect(groups[1]?.findAll('.history-row')).toHaveLength(5)
    expect(wrapper.findAll('.history-row')).toHaveLength(9)
    expect(wrapper.text()).not.toContain('组合留出段验证')
    expect(wrapper.text()).not.toContain('载入结果')
  })

  it('shows duration, research split and parallelism on every task history row', async () => {
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    vi.mocked(api.fetchStrategyPoolJobs).mockResolvedValue([{
      job_id: 'full-1',
      status: 'completed',
      mode: 'full',
      created_at: 1_700_000_000,
      duration_seconds: 18_720,
      duration_includes_queue: false,
      completed_symbols: 3_498,
      skipped_symbols: 859,
      newly_researched_symbols: 2_639,
      failed_symbols: 10,
      total_symbols: 4_357,
      maximum_parallelism: 4,
      automatic: false,
    }])

    const wrapper = mount(StrategyPoolPage)
    await flushPromises()

    const row = wrapper.get('[data-testid="pool-history-full-1"]').text()
    expect(row).toContain('全市场重选')
    expect(row).toContain('已完成 · 耗时 5 小时 12 分钟')
    // completed_symbols 把复用也算进去了，只说「3498 只完成」看不出真研究了几只。
    expect(row).toContain('已完成 3498/4357（新研究 2639 · 复用 859）')
    expect(row).toContain('失败 10')
    expect(row).toContain('并行 4')
    expect(row).toContain('手动触发')
    expect(row).not.toContain('含排队')
  })

  it('marks durations that had to include queue time', async () => {
    // started_at 是 worker 认领时才写的；在那之前入库的任务只能从创建时间起算。
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    vi.mocked(api.fetchStrategyPoolJobs).mockResolvedValue([{
      job_id: 'full-old',
      status: 'cancelled',
      mode: 'full',
      created_at: 1_700_000_000,
      duration_seconds: 3_600,
      duration_includes_queue: true,
      newly_researched_symbols: 40,
      automatic: true,
    }])

    const wrapper = mount(StrategyPoolPage)
    await flushPromises()

    const row = wrapper.get('[data-testid="pool-history-full-old"]').text()
    expect(row).toContain('已取消')
    expect(row).toContain('耗时 1 小时（含排队）')
    expect(row).toContain('自动触发')
  })

  it('summarizes recent task durations by mode above the history list', async () => {
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    vi.mocked(api.fetchStrategyPoolJobStats).mockResolvedValue({
      sample_limit: 60,
      modes: {
        full: {
          samples: 5,
          avg_duration_seconds: 3_000,
          median_duration_seconds: 2_400,
          started_at_samples: 3,
          throughput_samples: 6,
          avg_seconds_per_symbol: 40,
          median_seconds_per_symbol: 36,
          avg_parallelism: 3.5,
        },
        single: {
          samples: 0,
          avg_duration_seconds: null,
          median_duration_seconds: null,
          started_at_samples: 0,
          throughput_samples: 0,
          avg_seconds_per_symbol: null,
          median_seconds_per_symbol: null,
          avg_parallelism: null,
        },
        signals: {
          samples: 60,
          avg_duration_seconds: 30,
          median_duration_seconds: 26,
          started_at_samples: 60,
          throughput_samples: 60,
          avg_seconds_per_symbol: 13,
          median_seconds_per_symbol: 12,
          avg_parallelism: 1,
        },
        portfolio: {
          samples: 0,
          avg_duration_seconds: null,
          median_duration_seconds: null,
          started_at_samples: 0,
          throughput_samples: 0,
          avg_seconds_per_symbol: null,
          median_seconds_per_symbol: null,
          avg_parallelism: null,
        },
      },
    })

    const wrapper = mount(StrategyPoolPage)
    await flushPromises()

    const stats = wrapper.get('[data-testid="pool-history-stats"]').text()
    expect(stats).toContain('全市场重选近 5 次完成耗时：中位 40 分钟 · 平均 50 分钟')
    expect(stats).toContain('每只新研究标的中位 36.0 秒（6 轮样本，平均并行 3.5）')
    expect(stats).toContain('信号扫描近 60 次中位 26.0 秒')
    expect(stats).not.toContain('单股重跑')
    const note = wrapper.get('[data-testid="pool-history-stats-note"]').text()
    expect(note).toContain('每种任务各取最近 60 次终态记录')
    expect(note).toContain('其中 2 次全市场重选没有记录开始时间')
  })

  it('says why there is no average completion time when no full run finished', async () => {
    // 2026-10-18 线上形状：最近 60 次全市场终态里没有一次 completed（全是失败/取消），
    // 「平均完成耗时」那一行整条消失，用户读到的是「统计坏了」而不是「没有可平均的样本」。
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    vi.mocked(api.fetchStrategyPoolJobStats).mockResolvedValue({
      sample_limit: 60,
      modes: {
        full: {
          samples: 0, avg_duration_seconds: null, median_duration_seconds: null,
          started_at_samples: 0, throughput_samples: 36,
          avg_seconds_per_symbol: 674.3, median_seconds_per_symbol: 659.6, avg_parallelism: 2.1,
        },
        single: {
          samples: 0, avg_duration_seconds: null, median_duration_seconds: null,
          started_at_samples: 0, throughput_samples: 0,
          avg_seconds_per_symbol: null, median_seconds_per_symbol: null, avg_parallelism: null,
        },
        signals: {
          samples: 60, avg_duration_seconds: 24.1, median_duration_seconds: 22.3,
          started_at_samples: 60, throughput_samples: 60,
          avg_seconds_per_symbol: 12.4, median_seconds_per_symbol: 10.5, avg_parallelism: 4,
        },
        portfolio: {
          samples: 0, avg_duration_seconds: null, median_duration_seconds: null,
          started_at_samples: 0, throughput_samples: 0,
          avg_seconds_per_symbol: null, median_seconds_per_symbol: null, avg_parallelism: null,
        },
      },
    })

    const wrapper = mount(StrategyPoolPage)
    await flushPromises()

    const stats = wrapper.get('[data-testid="pool-history-stats"]').text()
    expect(stats).toContain('最近 60 次终态里没有一次正常完成')
    expect(stats).not.toContain('完成耗时：中位')
    // 每只新研究标的的耗时仍然给得出：被取消的半截轮次只进这一项。
    expect(stats).toContain('每只新研究标的中位 11 分钟（36 轮样本，平均并行 2.1）')
  })

  it('shows task history timestamps in China time, not the browser zone', async () => {
    // created_at 是后端 `_now()` 落库时去掉 tzinfo 的裸 UTC 串。按浏览器时区解释会整体
    // 差 8 小时，和站内其它走 formatChinaTimestamp 的位置也对不上。
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    vi.mocked(api.fetchStrategyPoolJobs).mockResolvedValue([{
      job_id: 'job-utc',
      status: 'completed',
      mode: 'signals',
      created_at: '2026-10-01T01:00:00',
    }])

    const wrapper = mount(StrategyPoolPage)
    await flushPromises()

    const row = wrapper.get('.history-row')
    expect(row.text()).toContain('2026-10-01 09:00:00')
    expect(row.text()).not.toContain('01:00:00')
  })

  it('shows simulated current holdings in a separate panel from daily signal status', async () => {
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    vi.mocked(api.getStrategyPoolJob).mockResolvedValueOnce({
      job_id: 'signal-1',
      status: 'completed',
      result: {
        mode: 'signals',
        objective: 'annual_return',
        holding_scope: 'per_stock_strategy',
        holding_snapshot_complete: true,
        as_of_date: '2026-10-01',
        buy_signals: [],
        sell_signals: [{ ...buySignal, status: 'sell', signal_type: 'sell' }],
        states: [{ ...buySignal, status: 'sell' }],
        model_portfolio: {
          available: true,
          holding_scope: 'per_stock_strategy',
          holdings: [{
            market: 'SH',
            code: '600000',
            name: '浦发银行',
            holding_return_pct: 0.12,
          }],
          risk_exit_signals: [],
        },
      },
    })

    const wrapper = mount(StrategyPoolPage)
    await flushPromises()

    expect(wrapper.get('[data-testid="pool-current-holdings"]').text()).toContain('持仓中 1')
    expect(wrapper.get('[data-testid="pool-current-holdings"]').text()).toContain('SH600000 · 浦发银行')
    expect(wrapper.get('[data-testid="pool-current-holdings"]').text()).toContain('今日触发卖出 · 下一交易日卖出')
    expect(wrapper.get('[data-testid="pool-current-holdings"]').text()).toContain('持仓阶段收益 +12.00%')
    expect(wrapper.get('[data-testid="pool-sell-signals"]').text()).toContain('验证收益 +24.00%')
    expect(wrapper.get('[data-testid="pool-sell-signals"]').text()).toContain('全区间收益 +71.00%')
    expect(wrapper.get('[data-testid="pool-sell-signals"]').text()).toContain('留出收益 +8.00%')
    expect(wrapper.get('[data-testid="pool-sell-signals"]').text()).toContain('交易数 验证 12 · 留出 4')
    expect(wrapper.get('[data-testid="pool-current-holdings"]').text()).not.toContain('成本')
    expect(wrapper.get('[data-testid="pool-current-holdings"]').text()).not.toContain('浮盈')
    expect(wrapper.get('[data-testid="pool-row-600000"]').text()).toContain('持仓中')
    expect(wrapper.get('[data-testid="pool-row-600000"]').text()).toContain('触发卖出')
    expect(wrapper.get('[data-testid="pool-row-600000"]').text()).not.toContain('空仓')
  })

  it('labels a risk exit by the day it triggered and the day it filled', async () => {
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    vi.mocked(api.getStrategyPoolJob).mockResolvedValueOnce({
      job_id: 'signal-risk',
      status: 'completed',
      result: {
        mode: 'signals',
        objective: 'annual_return',
        holding_scope: 'per_stock_strategy',
        holding_snapshot_complete: true,
        as_of_date: '2026-10-01',
        buy_signals: [],
        sell_signals: [],
        states: [],
        model_portfolio: {
          available: true,
          holding_scope: 'per_stock_strategy',
          holdings: [{
            market: 'SH',
            code: '600001',
            name: '在途止损',
            status: 'held',
            holding_return_pct: -0.06,
            pending_exit_reason: '止损',
            pending_exit_signal_date: '2026-10-01',
            pending_exit_risk_trigger: 'stop_loss',
            stop_loss_price: 9,
            trailing_stop_price: 11.4,
            take_profit_price: null,
            risk_level_as_of: '2026-10-01',
            max_hold_days: 20,
            max_hold_days_left: 3,
            max_hold_expiry_date: '2026-10-28',
          }],
          risk_exit_signals: [{
            market: 'SH',
            code: '600000',
            name: '浦发银行',
            reason: '止损',
            risk_trigger: 'stop_loss',
            signal_date: '2026-09-30',
            date: '2026-10-01',
            price: 10,
            trade_return_pct: -0.08,
          }],
        },
      },
    })

    const wrapper = mount(StrategyPoolPage)
    await flushPromises()

    // 昨收触发、今开成交：挂单在途那天它仍是持仓，标签要说清是哪条线、按哪天的数据算。
    const holdings = wrapper.get('[data-testid="pool-current-holdings"]')
    expect(holdings.text()).toContain('今日触发止损 · 下一交易日开盘卖出')
    expect(holdings.get('[data-testid="pool-holding-risk-600001"]').text()).toBe(
      '止损 9.00 · 移动止盈 11.40（截至 2026-10-01 收盘） · 2026-10-28 到期 · 还剩 3 个交易日',
    )

    const sells = wrapper.get('[data-testid="pool-sell-signals"]')
    expect(sells.text()).toContain('2026-09-30 收盘触发止损 · 2026-10-01 开盘已成交')
    expect(sells.text()).toContain('本次持仓收益 -8.00%')
    expect(sells.text()).not.toContain('按风控成交规则处理')
    // 面板标题不能再说「当天触发卖出」：风控退出是昨天收盘触发、今天开盘成交。
    expect(sells.text()).toContain('卖出规则与风控退出 1')
    expect(sells.text()).toContain('2026-10-01 收盘判定卖出规则 · 风控退出在下一交易日开盘成交')
    expect(wrapper.text()).toContain('止损、止盈、移动止盈走同一条口径')
  })

  it('shows confirmed holdings and their signal badge when the per-stock snapshot is incomplete', async () => {
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    vi.mocked(api.getStrategyPoolJob).mockResolvedValueOnce({
      job_id: 'signal-partial',
      status: 'completed',
      result: {
        mode: 'signals',
        objective: 'annual_return',
        holding_scope: 'per_stock_strategy',
        holding_snapshot_complete: false,
        as_of_date: '2026-10-01',
        buy_signals: [buySignal],
        sell_signals: [],
        states: [buySignal],
        model_portfolio: {
          available: true,
          holding_scope: 'per_stock_strategy',
          failed_symbols: 7,
          holdings: [{
            market: 'SH',
            code: '600000',
            name: '浦发银行',
            status: 'held',
            holding_return_pct: 0.05,
          }],
          risk_exit_signals: [],
        },
      },
    })

    const wrapper = mount(StrategyPoolPage)
    await flushPromises()

    const holdings = wrapper.get('[data-testid="pool-current-holdings"]')
    expect(holdings.text()).toContain('持仓中 1')
    expect(holdings.text()).toContain('SH600000 · 浦发银行')
    expect(holdings.text()).toContain('持仓阶段收益 +5.00%')
    expect(holdings.text()).toContain('有 7 个标的未能确认')
    const buySignals = wrapper.get('[data-testid="pool-buy-signals"]')
    expect(buySignals.text()).toContain('当前持仓收益 +5.00%')
    expect(buySignals.text()).toContain('买入信号触发')
  })

  it('keeps an incomplete empty snapshot distinct from a confirmed no-holdings result', async () => {
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    vi.mocked(api.getStrategyPoolJob).mockResolvedValueOnce({
      job_id: 'signal-empty-partial',
      status: 'completed',
      result: {
        mode: 'signals',
        objective: 'annual_return',
        holding_scope: 'per_stock_strategy',
        holding_snapshot_complete: false,
        as_of_date: '2026-10-01',
        buy_signals: [],
        sell_signals: [],
        states: [],
        model_portfolio: {
          available: true,
          holding_scope: 'per_stock_strategy',
          holdings: [],
          risk_exit_signals: [],
        },
      },
    })

    const wrapper = mount(StrategyPoolPage)
    await flushPromises()

    const holdings = wrapper.get('[data-testid="pool-current-holdings"]')
    expect(holdings.text()).toContain('结果不完整')
    expect(holdings.text()).not.toContain('当前没有仍处于持仓状态的标的')
  })

  it('does not show holdings from an unavailable portfolio even when they are present in the payload', async () => {
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    vi.mocked(api.getStrategyPoolJob).mockResolvedValueOnce({
      job_id: 'signal-unavailable',
      status: 'completed',
      result: {
        mode: 'signals',
        objective: 'annual_return',
        holding_scope: 'per_stock_strategy',
        holding_snapshot_complete: false,
        as_of_date: '2026-10-01',
        buy_signals: [],
        sell_signals: [],
        states: [],
        model_portfolio: {
          available: false,
          reason: '策略持仓计算失败',
          holdings: [{ market: 'SH', code: '600000', name: '不应显示' }],
          risk_exit_signals: [],
        },
      },
    })

    const wrapper = mount(StrategyPoolPage)
    await flushPromises()

    const holdings = wrapper.get('[data-testid="pool-current-holdings"]')
    expect(holdings.text()).toContain('策略持仓计算失败')
    expect(holdings.text()).not.toContain('不应显示')
  })

  it('paginates each daily signal and holding panel by ten rows', async () => {
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    const signals = Array.from({ length: 12 }, (_value, index) => {
      const code = String(600000 + index)
      return {
        ...buySignal,
        code,
        name: `测试${code}`,
        symbol: `SH${code}`,
        candidate_id: `candidate-${code}`,
      }
    })
    const riskExits = Array.from({ length: 2 }, (_value, index) => ({
      market: 'SH',
      code: `61000${index}`,
      name: `风控标的${index}`,
      date: '2026-10-01',
      risk_trigger: 'stop_loss',
      reason: '止损',
      trade_return_pct: -0.08,
    }))
    const holdings = Array.from({ length: 11 }, (_value, index) => {
      const code = String(600000 + index)
      return {
        market: 'SH' as const,
        code,
        name: `持仓标的${index}`,
        status: 'held',
        holding_return_pct: index / 100,
      }
    })
    vi.mocked(api.fetchStrategyPoolJobs).mockResolvedValueOnce([{
      job_id: 'signals-paged',
      status: 'completed',
      mode: 'signals',
      objective: 'annual_return',
      created_at: Date.now() / 1000,
    }])
    vi.mocked(api.getStrategyPoolJob).mockResolvedValueOnce({
      job_id: 'signals-paged',
      status: 'completed',
      result: {
        mode: 'signals',
        objective: 'annual_return',
        holding_scope: 'per_stock_strategy',
        holding_snapshot_complete: true,
        as_of_date: '2026-10-01',
        buy_signals: signals,
        sell_signals: signals,
        states: [
          { ...buySignal, status: 'flat' },
          { ...buySignal, code: '600001', symbol: 'SH600001', candidate_id: 'candidate-600001', status: 'flat' },
        ],
        market_snapshot: {
          data_end: '2026-10-01',
          bar_count: 300,
          last_online_sync_at: '2026-10-01T18:00:00+08:00',
        },
        strategy_snapshot: [
          { market: 'SH', code: '600000', candidate_id: 'candidate-600000' },
          { market: 'SH', code: '600001', candidate_id: 'candidate-600001' },
        ],
        model_portfolio: {
          available: true,
          holding_scope: 'per_stock_strategy',
          holdings,
          risk_exit_signals: riskExits,
        },
      },
    })

    const wrapper = mount(StrategyPoolPage)
    await flushPromises()

    expect(wrapper.findAll('[data-testid="pool-buy-signals"] .signal-row')).toHaveLength(10)
    expect(wrapper.findAll('[data-testid="pool-sell-signals"] .signal-row')).toHaveLength(10)
    expect(wrapper.findAll('[data-testid="pool-current-holdings"] .signal-row')).toHaveLength(10)
    expect(wrapper.get('[data-testid="pool-sell-signals"]').text()).toContain('全区间收益')

    await wrapper.get('[data-testid="pool-buy-pagination"] button:last-child').trigger('click')
    await wrapper.get('[data-testid="pool-sell-pagination"] button:last-child').trigger('click')
    await wrapper.get('[data-testid="pool-holdings-pagination"] button:last-child').trigger('click')

    expect(wrapper.findAll('[data-testid="pool-buy-signals"] .signal-row')).toHaveLength(2)
    expect(wrapper.findAll('[data-testid="pool-sell-signals"] .signal-row')).toHaveLength(4)
    expect(wrapper.findAll('[data-testid="pool-current-holdings"] .signal-row')).toHaveLength(1)
    expect(wrapper.get('[data-testid="pool-current-holdings"]').text()).toContain('持仓阶段收益 +10.00%')
  })

  it('replaces a legacy five-slot signal snapshot before showing per-stock holdings', async () => {
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    vi.mocked(api.fetchStrategyPoolJobs).mockResolvedValue([{
      job_id: 'legacy-signals',
      mode: 'signals',
      status: 'completed',
      objective: 'annual_return',
      created_at: '2026-10-01T10:00:00+08:00',
    }])
    vi.mocked(api.getStrategyPoolJob).mockResolvedValueOnce({
      job_id: 'legacy-signals',
      status: 'completed',
      result: {
        mode: 'signals',
        objective: 'annual_return',
        as_of_date: '2026-10-01',
        buy_signals: [],
        sell_signals: [],
        states: [],
        model_portfolio: {
          available: true,
          holdings: [{ market: 'SH', code: '600000', name: '旧版组合持仓' }],
          risk_exit_signals: [],
        },
      },
    })

    const wrapper = mount(StrategyPoolPage)
    await flushPromises()

    expect(api.createStrategyPoolJob).not.toHaveBeenCalled()
    expect(wrapper.get('[data-testid="pool-current-holdings"]').text()).not.toContain('旧版组合持仓')
    expect(wrapper.get('[data-testid="pool-current-holdings"]').text()).toContain('当前历史结果没有逐股持仓快照')
  })

  it('restores the latest matching signal result while a portfolio backtest is active', async () => {
    vi.useFakeTimers()
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    vi.mocked(api.fetchStrategyPoolJobs).mockResolvedValueOnce([
      { job_id: 'portfolio-running', status: 'running', mode: 'portfolio' },
      {
        job_id: 'signals-complete',
        status: 'completed',
        mode: 'signals',
        objective: 'annual_return',
      },
    ])
    vi.mocked(api.getStrategyPoolJob)
      .mockResolvedValueOnce({
        job_id: 'signals-complete',
        status: 'completed',
        result: {
          mode: 'signals',
          objective: 'annual_return',
          holding_scope: 'per_stock_strategy',
          holding_snapshot_complete: true,
          as_of_date: '2026-10-01',
          buy_signals: [],
          sell_signals: [],
          states: [{ ...buySignal, status: 'no_strategy', candidate_id: undefined }],
          model_portfolio: {
            available: true,
            holding_scope: 'per_stock_strategy',
            holdings: [{ market: 'SH', code: '600000', name: '浦发银行' }],
            risk_exit_signals: [],
          },
        },
      })
      .mockResolvedValueOnce({
        job_id: 'portfolio-running',
        status: 'running',
        progress: 0.4,
      })

    const wrapper = mount(StrategyPoolPage)
    try {
      await flushPromises()

      expect(api.getStrategyPoolJob).toHaveBeenNthCalledWith(1, 'signals-complete')
      expect(wrapper.get('[data-testid="pool-row-600000"]').text()).toContain('持仓中')
      expect(wrapper.get('[data-testid="pool-row-600000"]').text()).toContain('策略可用 · 信号待刷新')
      expect(wrapper.get('[data-testid="pool-current-holdings"]').text()).toContain('浦发银行')
      expect(wrapper.get('[data-testid="pool-job-progress"]').text()).toContain('组合回测任务进行中')
    } finally {
      wrapper.unmount()
      vi.useRealTimers()
    }
  })

  it('does not rescan on page entry when data and selected strategies match the saved signal scan', async () => {
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    vi.mocked(marketDataApi.fetchStoreStatus).mockResolvedValueOnce({
      database_path: 'market.duckdb',
      schema_version: 1,
      instrument_count: 2,
      bar_count: 300,
      data_start: '2025-01-01',
      data_end: '2026-10-01',
      last_online_sync_at: '2026-10-03T07:30:00+08:00',
    })
    vi.mocked(api.fetchStrategyPoolJobs).mockResolvedValueOnce([
      {
        job_id: 'signals-fresh',
        status: 'completed',
        mode: 'signals',
        objective: 'annual_return',
        created_at: Date.now() / 1000,
      },
    ])
    vi.mocked(api.getStrategyPoolJob).mockResolvedValueOnce({
      job_id: 'signals-fresh',
      status: 'completed',
      result: {
        mode: 'signals',
        objective: 'annual_return',
        holding_scope: 'per_stock_strategy',
        holding_snapshot_complete: true,
        as_of_date: '2026-10-01',
        buy_signals: [buySignal],
        sell_signals: [],
        states: [buySignal],
        market_snapshot: {
          data_end: '2026-10-01',
          bar_count: 300,
          last_online_sync_at: '2026-10-01T18:00:00+08:00',
        },
        strategy_snapshot: [
          { market: 'SH', code: '600000', candidate_id: 'candidate-600000' },
          { market: 'SH', code: '600001', candidate_id: 'candidate-600001' },
        ],
        model_portfolio: {
          available: true,
          holding_scope: 'per_stock_strategy',
          holdings: [],
          risk_exit_signals: [],
        },
      },
    })

    const wrapper = mount(StrategyPoolPage)
    await flushPromises()

    expect(api.createStrategyPoolJob).not.toHaveBeenCalled()
    expect(wrapper.get('[data-testid="pool-row-600000"]').text()).toContain('触发买入')
  })

  it('does not automatically repeat a failed scan for unchanged data and strategies', async () => {
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    vi.mocked(api.fetchStrategyPoolJobs).mockResolvedValueOnce([
      {
        job_id: 'signals-failed',
        status: 'failed',
        mode: 'signals',
        objective: 'annual_return',
        created_at: Date.now() / 1000,
        finished_at: 1_790_984_500,
      },
      {
        job_id: 'full-cancelled',
        status: 'cancelled',
        mode: 'full',
        created_at: Date.now() / 1000,
        finished_at: 1_790_984_378,
      },
      {
        job_id: 'signals-before-research',
        status: 'completed',
        mode: 'signals',
        objective: 'annual_return',
        created_at: Date.now() / 1000,
      },
    ])
    vi.mocked(api.getStrategyPoolJob).mockResolvedValueOnce({
      job_id: 'signals-before-research',
      status: 'completed',
      result: {
        mode: 'signals',
        objective: 'annual_return',
        holding_scope: 'per_stock_strategy',
        holding_snapshot_complete: true,
        as_of_date: '2026-09-30',
        buy_signals: [],
        sell_signals: [],
        states: [],
        market_snapshot: {
          data_end: '2026-10-01',
          bar_count: 300,
          last_online_sync_at: '2026-10-01T18:00:00+08:00',
        },
        strategy_snapshot: [],
        model_portfolio: {
          available: true,
          holding_scope: 'per_stock_strategy',
          holdings: [],
          risk_exit_signals: [],
        },
      },
    })

    const wrapper = mount(StrategyPoolPage)
    await flushPromises()

    expect(api.createStrategyPoolJob).not.toHaveBeenCalled()
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="pool-row-600000"]').text()).toContain('策略可用 · 信号待刷新')
  })

  it('does not repeat the same-day scan when local market data changes', async () => {
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    vi.mocked(marketDataApi.fetchStoreStatus).mockResolvedValueOnce({
      database_path: 'market.duckdb',
      schema_version: 1,
      instrument_count: 2,
      bar_count: 301,
      data_start: '2025-01-01',
      data_end: '2026-10-02',
      last_online_sync_at: '2026-10-02T18:00:00+08:00',
    })
    vi.mocked(api.fetchStrategyPoolJobs).mockResolvedValueOnce([
      {
        job_id: 'signals-old',
        status: 'completed',
        mode: 'signals',
        objective: 'annual_return',
        created_at: Date.now() / 1000,
      },
    ])
    vi.mocked(api.getStrategyPoolJob).mockResolvedValueOnce({
      job_id: 'signals-old',
      status: 'completed',
      result: {
        mode: 'signals',
        objective: 'annual_return',
        holding_scope: 'per_stock_strategy',
        holding_snapshot_complete: true,
        as_of_date: '2026-10-01',
        buy_signals: [],
        sell_signals: [],
        states: [],
        market_snapshot: {
          data_end: '2026-10-01',
          bar_count: 300,
          last_online_sync_at: '2026-10-01T18:00:00+08:00',
        },
        strategy_snapshot: [
          { market: 'SH', code: '600000', candidate_id: 'candidate-600000' },
          { market: 'SH', code: '600001', candidate_id: 'candidate-600001' },
        ],
        model_portfolio: {
          available: true,
          holding_scope: 'per_stock_strategy',
          holdings: [],
          risk_exit_signals: [],
        },
      },
    })

    mount(StrategyPoolPage)
    await flushPromises()

    expect(api.createStrategyPoolJob).not.toHaveBeenCalled()
  })

  it('does not repeat the same-day scan after full reselection', async () => {
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    vi.mocked(api.fetchStrategyPoolJobs).mockResolvedValueOnce([
      { job_id: 'full-newer', status: 'completed', mode: 'full', created_at: Date.now() / 1000 },
      {
        job_id: 'signals-older',
        status: 'completed',
        mode: 'signals',
        objective: 'annual_return',
        created_at: Date.now() / 1000,
      },
    ])
    vi.mocked(api.getStrategyPoolJob).mockResolvedValueOnce({
      job_id: 'signals-older',
      status: 'completed',
      result: {
        mode: 'signals',
        objective: 'annual_return',
        holding_scope: 'per_stock_strategy',
        holding_snapshot_complete: true,
        as_of_date: '2026-10-01',
        buy_signals: [],
        sell_signals: [],
        states: [],
        market_snapshot: {
          data_end: '2026-10-01',
          bar_count: 300,
          last_online_sync_at: '2026-10-01T18:00:00+08:00',
        },
        strategy_snapshot: [
          { market: 'SH', code: '600000', candidate_id: 'candidate-600000' },
          { market: 'SH', code: '600001', candidate_id: 'candidate-600001' },
        ],
        model_portfolio: {
          available: true,
          holding_scope: 'per_stock_strategy',
          holdings: [],
          risk_exit_signals: [],
        },
      },
    })

    mount(StrategyPoolPage)
    await flushPromises()

    expect(api.createStrategyPoolJob).not.toHaveBeenCalled()
  })

  it('does not repeat the same-day scan when one strategy changes', async () => {
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    vi.mocked(api.fetchStrategyPoolJobs).mockResolvedValueOnce([
      {
        job_id: 'signals-before-edit',
        status: 'completed',
        mode: 'signals',
        objective: 'annual_return',
        created_at: Date.now() / 1000,
      },
    ])
    vi.mocked(api.getStrategyPoolJob).mockResolvedValueOnce({
      job_id: 'signals-before-edit',
      status: 'completed',
      result: {
        mode: 'signals',
        objective: 'annual_return',
        holding_scope: 'per_stock_strategy',
        holding_snapshot_complete: true,
        as_of_date: '2026-10-01',
        buy_signals: [],
        sell_signals: [],
        states: [],
        market_snapshot: {
          data_end: '2026-10-01',
          bar_count: 300,
          last_online_sync_at: '2026-10-01T18:00:00+08:00',
        },
        strategy_snapshot: [
          { market: 'SH', code: '600000', candidate_id: 'candidate-600000' },
          { market: 'SH', code: '600001', candidate_id: 'old-candidate-600001' },
        ],
        model_portfolio: {
          available: true,
          holding_scope: 'per_stock_strategy',
          holdings: [],
          risk_exit_signals: [],
        },
      },
    })

    mount(StrategyPoolPage)
    await flushPromises()

    expect(api.createStrategyPoolJob).not.toHaveBeenCalled()
  })

  it('paginates the market status table instead of mounting every symbol at once', async () => {
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    vi.mocked(api.fetchStrategyPoolEntries).mockResolvedValue(
      Array.from({ length: 60 }, (_item, index) => entry(String(600000 + index), 0.2)),
    )
    const wrapper = mount(StrategyPoolPage)
    await flushPromises()

    expect(wrapper.findAll('tbody tr')).toHaveLength(50)
    expect(wrapper.get('[data-testid="pool-pagination"]').text()).toContain('1–50 / 60')
    await wrapper.get('[data-testid="pool-pagination"] button:last-child').trigger('click')

    expect(wrapper.findAll('tbody tr')).toHaveLength(10)
    expect(wrapper.get('[data-testid="pool-pagination"]').text()).toContain('第 2 / 2 页')
  })

  it(
    'compares each strategy with its stock buy-and-hold baseline and shows drawdown and sample size',
    async () => {
      window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
      const wrapper = mount(StrategyPoolPage)
      await flushPromises()

      const row = wrapper.get('[data-testid="pool-row-600000"]')
      expect(row.get('[data-testid="pool-entry-benchmark-600000"]').text()).toContain(
        '全段基准 +50.00%',
      )
      expect(row.get('[data-testid="pool-entry-benchmark-600000"]').text()).not.toContain('验证基准')
      expect(row.get('[data-testid="pool-entry-benchmark-600000"]').text()).not.toContain('留出基准')
      expect(row.get('[data-testid="pool-entry-benchmark-600000"]').text()).not.toContain('策略超额')
      const headers = wrapper.findAll('thead th').map((header) => header.text())
      const benchmarkColumn = headers.findIndex((header) => header.includes('基准涨跌'))
      const strategyReturnColumn = headers.findIndex((header) => header.includes('策略收益'))
      expect(benchmarkColumn).toBeLessThan(strategyReturnColumn)
      expect(wrapper.get('thead').text()).toContain('最大回撤')
      expect(wrapper.get('thead').text()).not.toContain('策略收益（年化）')
      expect(row.text()).toContain('72.00%')
      expect(row.text()).toContain('-22.00%')
      expect(row.text()).not.toContain('-18.00%')
      expect(row.text()).toContain('12.5 天')
      expect(row.text()).toContain('验证 12 · 留出 4 笔')
    },
  )

  it('says plainly that a full run is still queued instead of faking a zero plan', async () => {
    // 任务在被 worker 领取之前一直是 queued。标题写「进行中」、计数写「需重算 0 只（全 0）」
    // 时，排队两分钟和真的在跑完全分不出来——2026-10-16 一次认领响应丢失就是这样空转到被判死。
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    vi.mocked(api.fetchStrategyPoolJobs).mockResolvedValueOnce([
      { job_id: 'full-queued', status: 'queued', mode: 'full' },
    ])
    vi.mocked(api.getStrategyPoolJob).mockResolvedValueOnce({
      job_id: 'full-queued',
      status: 'queued',
      progress: 0,
      total_candidates: 0,
      metadata: { mode: 'full', phase_label: '排队等待策略 worker' },
    })

    const wrapper = mount(StrategyPoolPage)
    await flushPromises()

    expect(wrapper.get('[data-testid="pool-progress-heading"]').text()).toContain('全市场研究排队中')
    expect(wrapper.find('[data-testid="pool-progress-count"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="pool-job-progress"]').text()).toContain('排队等待策略 worker')
  })

  it('shows the two-worker cap and active ticker progress during a full scan', async () => {
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    vi.mocked(api.fetchStrategyPoolJobs).mockResolvedValueOnce([
      { job_id: 'full-1', status: 'running', mode: 'full' },
    ])
    vi.mocked(api.getStrategyPoolJob).mockResolvedValueOnce({
      job_id: 'full-1',
      status: 'running',
      progress: 0.01,
      metadata: {
        total_symbols: 5_223,
        finished_symbols: 85,
        completed_symbols: 80,
        skipped_symbols: 1,
        failed_symbols: 5,
        current_parallelism: 2,
        maximum_parallelism: 2,
        memory_worker_limit: 2,
        available_memory_mb: 9_000,
        cpu_worker_limit: 2,
        cpu_load_average_1m: 0.8,
        visible_cpus: 8,
        active_symbols: [
          { symbol: 'SH:600000', name: '浦发银行', progress: 0.37, phase_label: '规则 1/6 · 风控搜索' },
          { symbol: 'SH:600004', name: '白云机场', progress: 0.62, phase_label: '规则 2/6 · 验证' },
        ],
      },
    })

    const wrapper = mount(StrategyPoolPage)
    await flushPromises()

    expect(wrapper.get('[data-testid="pool-parallelism"]').text()).toContain('标的并行 2 / 最大 2')
    expect(wrapper.get('[data-testid="pool-parallelism"]').text()).toContain(
      'CPU允许 2 个标的并发（8核，系统1分钟负载 0.8）',
    )
    expect(wrapper.get('[data-testid="pool-progress-count"]').text()).toContain(
      '需重算 5223 只（已完成 79 · 进行中 2 · 复用 1 · 失败 5）',
    )
    expect(wrapper.findAll('[data-testid^="pool-active-symbol-"]')).toHaveLength(2)
    expect(
      wrapper.get('[data-testid="pool-active-symbol-SH:600000"]').text(),
    ).toContain('SH:600000 · 浦发银行')
    expect(wrapper.get('[data-testid="pool-active-symbol-SH:600000"]').text()).toContain('37%')
    expect(wrapper.get('[data-testid="pool-active-symbol-SH:600000"] [role="progressbar"]').attributes('aria-valuenow')).toBe('37')
  })

  it('breaks out the symbols skipped for insufficient history', async () => {
    // 2026-10-19 用户报告：进度条已经 100%，「已完成」却只有 20，看着像任务没干活。
    // 队列里那 59 只是上市未满一年，每轮都在几毫秒内以跳过结束——它们本来就在 skipped
    // 里，所以要摊开成单独一项，但不能从「已完成」里再减一次。
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    vi.mocked(api.fetchStrategyPoolJobs).mockResolvedValueOnce([
      { job_id: 'full-1', status: 'running', mode: 'full' },
    ])
    vi.mocked(api.getStrategyPoolJob).mockResolvedValueOnce({
      job_id: 'full-1',
      status: 'running',
      progress: 0.9996,
      metadata: {
        total_symbols: 80,
        completed_symbols: 4_421,
        skipped_symbols: 4_401,
        insufficient_symbols: 59,
        failed_symbols: 0,
        current_parallelism: 1,
        maximum_parallelism: 4,
      },
    })

    const wrapper = mount(StrategyPoolPage)
    await flushPromises()

    const count = wrapper.get('[data-testid="pool-progress-count"]')
    expect(count.text()).toBe(
      '需重算 80 只（已完成 20 · 历史样本不足 59 · 进行中 1 · 复用 4342 · 失败 0）',
    )
    expect(count.attributes('title')).toContain('不产出策略结果')
  })

  it('reports scheduled symbols separately from the queue', async () => {
    // 排期出标的在预扫描就不进队列了：括号里的几项加起来仍然是队列长度，
    // 「另排期 N 只」单独报它们哪天自动回来，免得「需重算 0 只」看着像整池跑完了。
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    vi.mocked(api.fetchStrategyPoolJobs).mockResolvedValueOnce([
      { job_id: 'full-1', status: 'running', mode: 'full' },
    ])
    vi.mocked(api.getStrategyPoolJob).mockResolvedValueOnce({
      job_id: 'full-1',
      status: 'running',
      progress: 0.5,
      metadata: {
        total_symbols: 21,
        completed_symbols: 4_381,
        skipped_symbols: 4_360,
        insufficient_symbols: 0,
        deferred_insufficient_symbols: 59,
        next_eligible_date: '2026-11-30',
        failed_symbols: 0,
        current_parallelism: 1,
        maximum_parallelism: 4,
      },
    })

    const wrapper = mount(StrategyPoolPage)
    await flushPromises()

    const count = wrapper.get('[data-testid="pool-progress-count"]')
    expect(count.text()).toBe('需重算 21 只（已完成 21 · 进行中 1 · 复用 4360 · 失败 0） · 另排期 59 只')
    expect(count.attributes('title')).toContain('最早 2026-11-30 自动回到增量队列')
    expect(count.attributes('title')).toContain('不需要手动重跑')
  })

  it('does not bill insufficient-history symbols to the remaining time', async () => {
    // 那 59 只几乎不花时间；按「每只平均耗时」把它们算进剩余，会凭空多出几十分钟假预计。
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    vi.mocked(api.fetchStrategyPoolJobs).mockResolvedValueOnce([
      { job_id: 'full-1', status: 'running', mode: 'full' },
    ])
    const startedAt = Date.now() / 1000 - 3_600
    vi.mocked(api.getStrategyPoolJob).mockResolvedValueOnce({
      job_id: 'full-1',
      status: 'running',
      progress: 0.25,
      created_at: startedAt,
      started_at: startedAt,
      metadata: {
        total_symbols: 80,
        completed_symbols: 4_421,
        skipped_symbols: 4_401,
        insufficient_symbols: 59,
        failed_symbols: 0,
        current_parallelism: 1,
        maximum_parallelism: 4,
      },
    })

    const wrapper = mount(StrategyPoolPage)
    await flushPromises()

    const pace = wrapper.get('[data-testid="pool-research-pace"]').text()
    expect(pace).toContain('平均每只新研究标的 3 分钟')
    expect(pace).toContain('剩余 1 只预计还需 3 分钟')
    expect(pace).not.toContain('剩余 60 只')
  })

  it('extrapolates the remaining full-market research time from symbols actually researched', async () => {
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    vi.mocked(api.fetchStrategyPoolJobs).mockResolvedValueOnce([
      { job_id: 'full-1', status: 'running', mode: 'full' },
    ])
    // 一小时前被 worker 认领，创建时间还要早半小时：耗时从 started_at 起算，
    // 分母只算真研究了的那 100 只（120 完成里有 20 只是复用，几乎不花时间）。
    // total_symbols 是预扫描剔除纯复用之后的队列长度，所以它比 completed 小是正常的。
    const startedAt = Date.now() / 1000 - 3_600
    vi.mocked(api.getStrategyPoolJob).mockResolvedValueOnce({
      job_id: 'full-1',
      status: 'running',
      progress: 0.03,
      created_at: startedAt - 1_800,
      started_at: startedAt,
      metadata: {
        total_symbols: 1_000,
        completed_symbols: 120,
        skipped_symbols: 20,
        failed_symbols: 0,
        current_parallelism: 4,
        maximum_parallelism: 4,
      },
    })

    const wrapper = mount(StrategyPoolPage)
    await flushPromises()

    const pace = wrapper.get('[data-testid="pool-research-pace"]').text()
    expect(pace).toContain('已运行 1 小时')
    expect(pace).toContain('平均每只新研究标的 36.0 秒')
    expect(pace).toContain('剩余 900 只预计还需 9 小时')
    expect(pace).not.toContain('含排队')
  })

  it('keeps the estimate when most of the queue is reused results', async () => {
    // 2026-10-18 线上形状：1402 只待重算、3020 只复用直接计入 completed。
    // 拿 total − completed 会把剩余算成负数并夹到 0，「剩余预计」那一行就凭空消失了。
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    vi.mocked(api.fetchStrategyPoolJobs).mockResolvedValueOnce([
      { job_id: 'full-2', status: 'running', mode: 'full' },
    ])
    const startedAt = Date.now() / 1000 - 3_302
    vi.mocked(api.getStrategyPoolJob).mockResolvedValueOnce({
      job_id: 'full-2',
      status: 'running',
      progress: 0.05,
      created_at: startedAt,
      started_at: startedAt,
      metadata: {
        total_symbols: 1_402,
        completed_symbols: 3_085,
        skipped_symbols: 3_020,
        failed_symbols: 2,
        current_parallelism: 4,
        maximum_parallelism: 4,
      },
    })

    const wrapper = mount(StrategyPoolPage)
    await flushPromises()

    const pace = wrapper.get('[data-testid="pool-research-pace"]').text()
    expect(pace).toContain('平均每只新研究标的 50.8 秒')
    expect(pace).toContain('剩余 1335 只预计还需')
  })

  it('queues a row rerun behind the active signal scan without replacing its progress', async () => {
    vi.useFakeTimers()
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    vi.mocked(api.fetchStrategyPoolJobs)
      .mockResolvedValueOnce([{ job_id: 'signal-running', status: 'running', mode: 'signals' }])
      .mockResolvedValueOnce([
        { job_id: 'single-queued', status: 'queued', mode: 'single', market: 'SH', code: '600000' },
        { job_id: 'signal-running', status: 'running', mode: 'signals' },
      ])
      .mockResolvedValueOnce([
        { job_id: 'single-queued', status: 'running', mode: 'single', market: 'SH', code: '600000' },
      ])
    vi.mocked(api.getStrategyPoolJob)
      .mockResolvedValueOnce({
        job_id: 'signal-running',
        status: 'running',
        progress: 0.25,
        phase: '扫描信号',
        metadata: { phase_label: '已扫描 1319 / 5224 只' },
      })
      .mockResolvedValueOnce({
        job_id: 'signal-running',
        status: 'completed',
        result: {
          mode: 'signals',
          objective: 'annual_return',
          buy_signals: [],
          sell_signals: [],
          states: [],
        },
      })
      .mockResolvedValueOnce({ job_id: 'single-queued', status: 'running' })
    vi.mocked(api.createStrategyPoolJob).mockResolvedValueOnce({
      job_id: 'single-queued',
      status: 'queued',
    })

    const wrapper = mount(StrategyPoolPage)
    try {
      await flushPromises()
      const rerun = wrapper.get('[data-testid="pool-rerun-entry-600000"]')
      expect((rerun.element as HTMLButtonElement).disabled).toBe(false)
      await rerun.trigger('click')
      await flushPromises()

      expect(api.createStrategyPoolJob).toHaveBeenCalledWith({
        mode: 'single',
        market: 'SH',
        code: '600000',
        risk_trials: 3_000,
        max_hold_days: 20,
      })
      expect(wrapper.get('[data-testid="pool-job-progress"]').text()).toContain('正在扫描当天信号')
      expect(wrapper.get('[data-testid="pool-rerun-entry-600000"]').text()).toContain('排队中')

      await vi.advanceTimersByTimeAsync(1_500)
      await flushPromises()
      expect(wrapper.get('[data-testid="pool-job-progress"]').text()).toContain('正在重跑单只股票')
    } finally {
      wrapper.unmount()
      vi.useRealTimers()
    }
  })

  it('shows a transient polling warning without marking the running task failed', async () => {
    vi.useFakeTimers()
    vi.mocked(api.fetchStrategyPoolJobs).mockResolvedValue([])
    vi.mocked(api.getStrategyPoolJob)
      .mockRejectedValueOnce(new ApiError('DuckDB 暂时被占用', 503))
      .mockResolvedValueOnce({
        job_id: 'signal-1',
        status: 'completed',
        result: {
          mode: 'signals',
          objective: 'annual_return',
          buy_signals: [],
          sell_signals: [],
          states: [],
        },
      })
    const wrapper = mount(StrategyPoolPage)
    try {
      await wrapper.get('[data-testid="pool-select-annual"]').trigger('click')
      await flushPromises()
      await wrapper.get('[data-testid="pool-refresh-signals"]').trigger('click')
      await flushPromises()
      expect(wrapper.get('[data-testid="pool-poll-warning"]').text()).toContain('自动重试')
      expect(wrapper.find('[role="alert"]').exists()).toBe(false)

      await vi.advanceTimersByTimeAsync(1_500)
      await flushPromises()
      expect(wrapper.find('[data-testid="pool-poll-warning"]').exists()).toBe(false)
    } finally {
      vi.useRealTimers()
    }
  })

  it('shows saved single rerun candidates without launching another signal scan', async () => {
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    vi.mocked(api.createStrategyPoolJob)
      .mockResolvedValueOnce({ job_id: 'single-1', status: 'queued' })
    vi.mocked(api.getStrategyPoolJob)
      .mockResolvedValueOnce({
        job_id: 'signal-1',
        status: 'completed',
        result: {
          mode: 'signals',
          objective: 'annual_return',
          buy_signals: [buySignal],
          sell_signals: [],
          states: [
            { ...buySignal, status: 'buy' },
            {
              ...buySignal,
              market: 'SH',
              code: '600001',
              name: '测试600001',
              symbol: 'SH600001',
              candidate_id: 'candidate-600001',
              status: 'flat',
              signal_type: '',
            },
          ],
          model_portfolio: { available: true, holdings: [], risk_exit_signals: [] },
        },
      })
      .mockResolvedValueOnce({
        job_id: 'single-1',
        status: 'completed',
        result: {
          mode: 'single',
          single_candidate: {
            market: 'SH',
            code: '600000',
            saved_to_pool: true,
            benchmark: { full_period: { total_return: 0.2 } },
            annual_candidate: baseCandidate,
            sharpe_candidate: null,
          },
        },
      })
      .mockResolvedValueOnce({
        job_id: 'signals-targeted',
        status: 'completed',
        result: {
          mode: 'signals',
          objective: 'annual_return',
          signal_target: { market: 'SH', code: '600000' },
          buy_signals: [],
          sell_signals: [{ ...buySignal, status: 'sell', signal_type: 'sell' }],
          states: [{ ...buySignal, status: 'sell', signal_type: 'sell' }],
          model_portfolio: { available: true, holdings: [], risk_exit_signals: [] },
        },
      })
    const wrapper = mount(StrategyPoolPage)
    await flushPromises()
    await wrapper.get('[data-testid="pool-single-code"]').setValue('600000')
    await wrapper.get('[data-testid="pool-rerun-single"]').trigger('click')
    await flushPromises()

    expect(wrapper.get('[data-testid="pool-single-candidates"]').text()).toContain('年化与夏普自动候选已保存')
    expect(wrapper.find('[data-testid="pool-single-candidates"] button').exists()).toBe(false)
    await flushPromises()

    expect(api.saveStrategyPoolOverride).not.toHaveBeenCalled()
    expect(api.createStrategyPoolJob).toHaveBeenCalledTimes(1)
    expect(wrapper.get('[data-testid="pool-row-600000"]').text()).toContain('触发卖出')
    expect(wrapper.get('[data-testid="pool-row-600001"]').text()).toContain('策略可用 · 信号待刷新')
  })

  it('submits an explicit rebuild and clears the displayed old pool', async () => {
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    // 清空重建是不可逆操作，页面上有确认框；这里按「确定」走下去。
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    const wrapper = mount(StrategyPoolPage)
    try {
      await flushPromises()
      vi.mocked(api.getStrategyPoolJob).mockResolvedValue({
        job_id: 'rebuild', status: 'running', progress: 0,
      })
      await wrapper.get('[data-testid="pool-start-full"]').trigger('click')
      await flushPromises()
      expect(api.createStrategyPoolJob).toHaveBeenLastCalledWith({
        mode: 'full', rebuild: true, risk_trials: 3000, max_hold_days: 20,
        boards: ['main', 'chinext', 'star'], exclude_st: false,
      })
      expect(wrapper.find('[data-testid="pool-row-600000"]').exists()).toBe(false)
    } finally {
      wrapper.unmount()
      confirmSpy.mockRestore()
    }
  })

  it('submits nothing and keeps the pool on screen when the rebuild is dismissed', async () => {
    // 误点一次的代价是整张策略池被删空 + 几小时重跑，所以取消必须是什么都不做。
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    const wrapper = mount(StrategyPoolPage)
    try {
      await flushPromises()
      await wrapper.get('[data-testid="pool-start-full"]').trigger('click')
      await flushPromises()
      expect(confirmSpy).toHaveBeenCalledTimes(1)
      expect(api.createStrategyPoolJob).not.toHaveBeenCalled()
      expect(wrapper.find('[data-testid="pool-row-600000"]').exists()).toBe(true)

      // 增量更新不删东西，不该跟着弹确认。
      confirmSpy.mockClear()
      await wrapper.get('[data-testid="pool-start-incremental"]').trigger('click')
      await flushPromises()
      expect(confirmSpy).not.toHaveBeenCalled()
      expect(api.createStrategyPoolJob).toHaveBeenLastCalledWith(expect.objectContaining({
        mode: 'full', rebuild: false,
      }))
    } finally {
      wrapper.unmount()
      confirmSpy.mockRestore()
    }
  })

  it('renders default universe checkboxes and persists toggles immediately', async () => {
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    const wrapper = mount(StrategyPoolPage)
    try {
      await flushPromises()
      // 默认：三板块全选、不过滤 ST——与未保存偏好时的旧行为一致。
      expect((wrapper.get('[data-testid="pool-board-main"]').element as HTMLInputElement).checked).toBe(true)
      expect((wrapper.get('[data-testid="pool-board-chinext"]').element as HTMLInputElement).checked).toBe(true)
      expect((wrapper.get('[data-testid="pool-board-star"]').element as HTMLInputElement).checked).toBe(true)
      expect((wrapper.get('[data-testid="pool-exclude-st"]').element as HTMLInputElement).checked).toBe(false)

      await wrapper.get('[data-testid="pool-board-star"]').setValue(false)
      await flushPromises()
      expect(api.saveResearchSettings).toHaveBeenLastCalledWith({
        boards: ['main', 'chinext'], exclude_st: false,
      })

      await wrapper.get('[data-testid="pool-exclude-st"]').setValue(true)
      await flushPromises()
      expect(api.saveResearchSettings).toHaveBeenLastCalledWith({
        boards: ['main', 'chinext'], exclude_st: true,
      })

      // 取消 main 后还剩创业板，是合法范围，照常保存；再取消创业板就会被拦下。
      await wrapper.get('[data-testid="pool-board-main"]').setValue(false)
      await flushPromises()
      expect(api.saveResearchSettings).toHaveBeenCalledTimes(3)
      expect(api.saveResearchSettings).toHaveBeenLastCalledWith({
        boards: ['chinext'], exclude_st: true,
      })

      await wrapper.get('[data-testid="pool-board-chinext"]').setValue(false)
      await flushPromises()
      expect(api.saveResearchSettings).toHaveBeenCalledTimes(3)
      expect(wrapper.text()).toContain('至少要保留一个板块')
    } finally {
      wrapper.unmount()
    }
  })

  it('shows a submitting state while the signal scan request is in flight', async () => {
    // 提交要写一次任务行，研究正在跑时库被它占着，可能要等十几秒；这段时间既没有进度条
    // 也没有任务 id，按钮必须自己说明「正在提交」，否则看起来就是点了没反应。
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    let resolveCreate: (value: unknown) => void = () => {}
    vi.mocked(api.createStrategyPoolJob).mockImplementation(
      () => new Promise((resolve) => { resolveCreate = resolve }) as never,
    )
    const wrapper = mount(StrategyPoolPage)
    await flushPromises()

    const button = wrapper.get('[data-testid="pool-refresh-signals"]')
    await button.trigger('click')
    expect(button.text()).toBe('正在提交…')
    expect(button.attributes('disabled')).toBeDefined()
    expect(wrapper.get('[data-testid="pool-submitting"]').text()).toContain('正在把任务提交给后端')

    resolveCreate({ job_id: 'scan-1', status: 'queued' })
    await flushPromises()
    expect(wrapper.find('[data-testid="pool-submitting"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="pool-refresh-signals"]').text()).toBe('刷新当天信号')
    wrapper.unmount()
  })

  it('says so when the signal trading days cannot be read', async () => {
    // 一个空的禁用下拉框看不出是「读不到」还是「还没有历史」，所以读失败时换成一句说明。
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    vi.mocked(api.fetchStrategyPoolJobs).mockResolvedValue([])
    vi.mocked(api.fetchPoolSignalDays).mockRejectedValue(new Error('temporary unavailable'))

    const wrapper = mount(StrategyPoolPage)
    await flushPromises()

    expect(wrapper.find('[data-testid="pool-signal-date"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="pool-signal-day-unavailable"]').text())
      .toContain('暂时读不到交易日')
    wrapper.unmount()
  })

  it('lets the user review an earlier trading day through the signal date picker', async () => {
    // 「信号日期」下拉给的是后端每个交易日最后一次全市场扫描；默认最新那天，选更早的就按
    // 那天的名单显示，标题不能再宣称是「当天」。
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    vi.mocked(api.fetchStrategyPoolJobs).mockResolvedValue([
      {
        job_id: 'scan-2', mode: 'signals', status: 'completed',
        objective: 'annual_return', dual_objectives: true, created_at: Date.now() / 1000,
      },
    ])
    vi.mocked(api.fetchPoolSignalDays).mockResolvedValue([
      { as_of_date: '2026-10-02', job_id: 'scan-2' },
      { as_of_date: '2026-10-01', job_id: 'scan-1' },
    ])
    const resultFor = (asOf: string, withBuy: boolean): Record<string, unknown> => ({
      mode: 'signals',
      objective: 'annual_return',
      as_of_date: asOf,
      holding_scope: 'per_stock_strategy',
      holding_snapshot_complete: true,
      buy_signals: withBuy ? [{ ...buySignal }] : [],
      sell_signals: [],
      states: withBuy ? [{ ...buySignal }] : [],
      model_portfolio: {
        available: true,
        holding_scope: 'per_stock_strategy',
        holdings: [],
        risk_exit_signals: [],
      },
    })
    vi.mocked(api.getStrategyPoolJob).mockImplementation(async (jobId: string) => ({
      job_id: jobId,
      status: 'completed',
      result: jobId === 'scan-1'
        ? resultFor('2026-10-01', true)
        : resultFor('2026-10-02', false),
    } as never))

    const wrapper = mount(StrategyPoolPage)
    await flushPromises()

    const picker = wrapper.get('[data-testid="pool-signal-date"]')
    expect(picker.findAll('option').map((option) => option.text())).toEqual([
      '2026-10-02（最新）', '2026-10-01',
    ])
    expect((picker.element as HTMLSelectElement).value).toBe('2026-10-02')
    const buys = wrapper.get('[data-testid="pool-buy-signals"]')
    expect(buys.text()).toContain('当前没有股票触发买入规则')

    await picker.setValue('2026-10-01')
    await flushPromises()
    const historical = wrapper.get('[data-testid="pool-buy-signals"]')
    expect(historical.text()).toContain('触发买入 1')
    expect(historical.text()).toContain('SH600000')
    expect(historical.text()).not.toContain('当天')
    expect(wrapper.get('[data-testid="pool-signal-day-note"]').text())
      .toContain('2026-10-01 那天最后一次全市场扫描')

    // 切回最新那天：重新走「全市场扫描 + 逐股增量合并」那条路，不是留着历史那份。
    await picker.setValue('2026-10-02')
    await flushPromises()
    expect(wrapper.get('[data-testid="pool-buy-signals"]').text()).toContain('当前没有股票触发买入规则')
    expect(wrapper.find('[data-testid="pool-signal-day-note"]').exists()).toBe(true)
  })

  it('loads saved universe preferences and submits them with full research', async () => {
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    vi.mocked(api.fetchResearchSettings).mockResolvedValue({
      boards: ['main', 'chinext'], exclude_st: true,
    })
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    const wrapper = mount(StrategyPoolPage)
    try {
      await flushPromises()
      expect((wrapper.get('[data-testid="pool-board-main"]').element as HTMLInputElement).checked).toBe(true)
      expect((wrapper.get('[data-testid="pool-board-chinext"]').element as HTMLInputElement).checked).toBe(true)
      expect((wrapper.get('[data-testid="pool-board-star"]').element as HTMLInputElement).checked).toBe(false)
      expect((wrapper.get('[data-testid="pool-exclude-st"]').element as HTMLInputElement).checked).toBe(true)

      await wrapper.get('[data-testid="pool-start-full"]').trigger('click')
      await flushPromises()
      expect(api.createStrategyPoolJob).toHaveBeenLastCalledWith(expect.objectContaining({
        mode: 'full', boards: ['main', 'chinext'], exclude_st: true,
      }))
    } finally {
      wrapper.unmount()
      confirmSpy.mockRestore()
    }
  })

  it('does not restore signal snapshots from before the latest rebuild', async () => {
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    vi.mocked(api.fetchStrategyPoolJobs).mockResolvedValue([
      { job_id: 'rebuild', mode: 'full', rebuild: true, status: 'running', created_at: 200 },
      { job_id: 'old-signal', mode: 'signals', objective: 'annual_return', status: 'completed', created_at: 100 },
    ])
    vi.mocked(api.getStrategyPoolJob).mockResolvedValue({ job_id: 'rebuild', status: 'running' })
    const wrapper = mount(StrategyPoolPage)
    try {
      await flushPromises()
      expect(api.getStrategyPoolJob).not.toHaveBeenCalledWith('old-signal')
      expect(api.createStrategyPoolJob).not.toHaveBeenCalled()
    } finally {
      wrapper.unmount()
    }
  })
  it('restores a same-day scan on reentry and permits an explicit manual scan after switching objectives', async () => {
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    vi.mocked(api.fetchStrategyPoolJobs).mockResolvedValue([{
      job_id: 'today', mode: 'signals', objective: 'annual_return',
      status: 'completed', created_at: Date.now() / 1000,
    }])
    let wrapper = mount(StrategyPoolPage)
    await flushPromises()
    expect(api.createStrategyPoolJob).not.toHaveBeenCalled()
    wrapper.unmount()
    wrapper = mount(StrategyPoolPage)
    try {
      await flushPromises()
      await wrapper.get('[data-testid="pool-objective"]').setValue('sharpe')
      await flushPromises()
      expect(api.createStrategyPoolJob).not.toHaveBeenCalled()
      await wrapper.get('[data-testid="pool-refresh-signals"]').trigger('click')
      await flushPromises()
      expect(api.createStrategyPoolJob).toHaveBeenCalledExactlyOnceWith({
        mode: 'signals', objective: 'sharpe',
      })
    } finally {
      wrapper.unmount()
    }
  })

  it('does not append a signal scan when full research completes', async () => {
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    vi.mocked(api.fetchStrategyPoolJobs).mockResolvedValue([{
      job_id: 'research', mode: 'full', status: 'running',
    }])
    vi.mocked(api.getStrategyPoolJob).mockResolvedValue({
      job_id: 'research', status: 'completed', result: { mode: 'full' },
    })
    const wrapper = mount(StrategyPoolPage)
    try {
      await flushPromises()
      expect(api.createStrategyPoolJob).not.toHaveBeenCalled()
    } finally {
      wrapper.unmount()
    }
  })

  it('does not poll or show scanning when the server says today was already attempted', async () => {
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    vi.mocked(api.createStrategyPoolJob).mockResolvedValue({
      job_id: 'already-scanned', status: 'completed', skipped: true,
    })
    const wrapper = mount(StrategyPoolPage)
    try {
      await flushPromises()
      expect(api.createStrategyPoolJob).not.toHaveBeenCalled()
      expect(api.getStrategyPoolJob).toHaveBeenCalledWith('signal-1')
      expect(wrapper.find('[data-testid="pool-job-progress"]').exists()).toBe(false)
    } finally {
      wrapper.unmount()
    }
  })

  it('refreshes saved stock rows during full research without rescanning signals or resetting search', async () => {
    vi.useFakeTimers()
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    let saved = 0
    vi.mocked(api.fetchStrategyPoolJobs).mockResolvedValue([
      { job_id: 'full-live', status: 'running', mode: 'full' },
    ])
    vi.mocked(api.getStrategyPoolJob).mockImplementation(async () => ({
      job_id: 'full-live', status: 'running', progress: saved / 3,
      metadata: { finished_symbols: saved },
    }))
    vi.mocked(api.fetchStrategyPoolEntries).mockImplementation(async () =>
      saved ? [entry('600004', 0.3)] : [],
    )
    const wrapper = mount(StrategyPoolPage)
    try {
      await flushPromises()
      expect(api.fetchStrategyPoolEntries).toHaveBeenCalledTimes(1)
      await wrapper.get('[data-testid="pool-entry-search"]').setValue('600004')
      saved = 1
      await vi.advanceTimersByTimeAsync(1500)
      await flushPromises()
      expect(wrapper.find('[data-testid="pool-row-600004"]').exists()).toBe(true)
      expect(api.fetchStrategyPoolEntries).toHaveBeenCalledTimes(2)
      await vi.advanceTimersByTimeAsync(1500)
      await flushPromises()
      expect(api.fetchStrategyPoolEntries).toHaveBeenCalledTimes(2)
      saved = 2
      await vi.advanceTimersByTimeAsync(1500)
      await flushPromises()
      // 节流：上一次重取才过 3 秒，完成数变了也不重取整张表（4439 行 / 7 MB）。
      expect(api.fetchStrategyPoolEntries).toHaveBeenCalledTimes(2)
      await vi.advanceTimersByTimeAsync(20_000)
      await flushPromises()
      expect(api.fetchStrategyPoolEntries).toHaveBeenCalledTimes(3)
      expect((wrapper.get('[data-testid="pool-entry-search"]').element as HTMLInputElement).value).toBe('600004')
      expect(wrapper.get('[data-testid="pool-job-progress"]').text()).toContain('全市场研究进行中')
      expect(api.createStrategyPoolJob).not.toHaveBeenCalled()
    } finally {
      wrapper.unmount()
      vi.useRealTimers()
    }
  })

  it('retries a transient live stock-list refresh failure while keeping full research running', async () => {
    vi.useFakeTimers()
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    vi.mocked(api.fetchStrategyPoolJobs).mockResolvedValue([
      { job_id: 'full-live', status: 'running', mode: 'full' },
    ])
    vi.mocked(api.getStrategyPoolJob).mockResolvedValue({
      job_id: 'full-live', status: 'running', metadata: { finished_symbols: 1 },
    })
    vi.mocked(api.fetchStrategyPoolEntries)
      .mockResolvedValueOnce([])
      .mockRejectedValueOnce(new Error('temporary database contention'))
      .mockResolvedValue([entry('600004', 0.3)])
    const wrapper = mount(StrategyPoolPage)
    try {
      await flushPromises()
      expect(wrapper.get('[data-testid="pool-job-progress"]').text()).toContain('全市场研究进行中')
      expect(wrapper.get('[data-testid="pool-poll-warning"]').text()).toContain('标的状态刷新暂时失败')
      await vi.advanceTimersByTimeAsync(1500)
      await flushPromises()
      expect(wrapper.find('[data-testid="pool-row-600004"]').exists()).toBe(true)
      expect(wrapper.find('[data-testid="pool-poll-warning"]').exists()).toBe(false)
      expect(api.createStrategyPoolJob).not.toHaveBeenCalled()
    } finally {
      wrapper.unmount()
      vi.useRealTimers()
    }
  })

  it('refreshes the latest saved stock rows when full research is cancelled', async () => {
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    vi.mocked(api.fetchStrategyPoolJobs).mockResolvedValue([
      { job_id: 'full-live', status: 'running', mode: 'full' },
    ])
    vi.mocked(api.getStrategyPoolJob).mockResolvedValue({ job_id: 'full-live', status: 'cancelled' })
    vi.mocked(api.fetchStrategyPoolEntries).mockResolvedValueOnce([])
      .mockResolvedValue([entry('600004', 0.3)])
    const wrapper = mount(StrategyPoolPage)
    try {
      await flushPromises()
      expect(wrapper.find('[data-testid="pool-row-600004"]').exists()).toBe(true)
      expect(api.createStrategyPoolJob).not.toHaveBeenCalled()
    } finally {
      wrapper.unmount()
    }
  })

  it('shows independent stock timers that keep ticking across progress updates and page reload', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-03T05:00:00Z'))
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    const now = Date.now() / 1000
    vi.mocked(api.fetchStrategyPoolJobs).mockResolvedValue([
      { job_id: 'full-timed', status: 'running', mode: 'full' },
    ])
    vi.mocked(api.getStrategyPoolJob).mockResolvedValue({
      job_id: 'full-timed', status: 'running', metadata: {
        active_symbols: [
          { symbol: 'SH:600000', name: '浦发银行', progress: 0.1, started_at: now - 65 },
          { symbol: 'SH:600004', name: '白云机场', progress: 0.2, started_at: now - 5 },
        ],
      },
    })
    let wrapper = mount(StrategyPoolPage)
    try {
      await flushPromises()
      expect(wrapper.get('[data-testid="pool-elapsed-SH:600000"]').text()).toContain('00:01:05')
      expect(wrapper.get('[data-testid="pool-elapsed-SH:600004"]').text()).toContain('00:00:05')
      await vi.advanceTimersByTimeAsync(3000)
      await flushPromises()
      expect(wrapper.get('[data-testid="pool-elapsed-SH:600000"]').text()).toContain('00:01:08')
      wrapper.unmount()
      wrapper = mount(StrategyPoolPage)
      await flushPromises()
      expect(wrapper.get('[data-testid="pool-elapsed-SH:600000"]').text()).toContain('00:01:08')
      expect(api.createStrategyPoolJob).not.toHaveBeenCalled()
    } finally {
      wrapper.unmount()
      vi.useRealTimers()
    }
  })

  it('uses the server start time for a single-stock rerun timer', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-03T05:00:00Z'))
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    vi.mocked(api.fetchStrategyPoolJobs).mockResolvedValue([
      { job_id: 'single-timed', mode: 'single', status: 'running' },
    ])
    vi.mocked(api.getStrategyPoolJob).mockResolvedValue({
      job_id: 'single-timed', status: 'running', metadata: {
        symbol: 'SH:600000', ticker_name: '浦发银行', ticker_progress: 0.4,
        ticker_started_at: Date.now() / 1000 - 3605,
      },
    })
    const wrapper = mount(StrategyPoolPage)
    try {
      await flushPromises()
      expect(wrapper.get('[data-testid="pool-elapsed-SH:600000"]').text()).toContain('01:00:05')
      await vi.advanceTimersByTimeAsync(1000)
      expect(wrapper.get('[data-testid="pool-elapsed-SH:600000"]').text()).toContain('01:00:06')
    } finally {
      wrapper.unmount()
      vi.useRealTimers()
    }
  })

  describe('增量更新的工作量预估', () => {
    const estimate = {
      universe_symbols: 4417,
      pending_symbols: 112,
      reusable_symbols: 4305,
      rebuild: false,
      risk_trials: 3_000 as const,
      data_end: '2026-10-19',
    }

    it('explains the workload before the click, with the payload the click would send', async () => {
      window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
      vi.mocked(api.fetchStrategyPoolResearchEstimate).mockResolvedValue(estimate)
      const wrapper = mount(StrategyPoolPage)
      await flushPromises()

      const hint = wrapper.get('[data-testid="pool-research-estimate"]')
      expect(hint.text()).toBe('待重算 112 只 · 可复用 4305 只')
      expect(hint.attributes('title')).toContain('弱策略到期与上线后衰减的仍会重跑')
      expect(api.fetchStrategyPoolResearchEstimate).toHaveBeenCalledWith(
        expect.objectContaining({ mode: 'full', rebuild: false, risk_trials: 3_000 }),
      )
    })

    it('re-estimates when a research parameter changes', async () => {
      window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
      vi.mocked(api.fetchStrategyPoolResearchEstimate).mockResolvedValue(estimate)
      const wrapper = mount(StrategyPoolPage)
      await flushPromises()

      await wrapper.get('[data-testid="pool-max-hold-days"]').setValue('30')
      await flushPromises()

      expect(api.fetchStrategyPoolResearchEstimate).toHaveBeenLastCalledWith(
        expect.objectContaining({ max_hold_days: 30 }),
      )
    })

    it('names the day the deferred symbols come back on its own', async () => {
      window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
      vi.mocked(api.fetchStrategyPoolResearchEstimate).mockResolvedValue({
        ...estimate,
        pending_symbols: 0,
        reusable_symbols: 4_305,
        deferred_symbols: 112,
        next_eligible_date: '2026-11-30',
      })
      const wrapper = mount(StrategyPoolPage)
      await flushPromises()

      const hint = wrapper.get('[data-testid="pool-research-estimate"]')
      expect(hint.text())
        .toBe('没有待更新：4305 只可直接复用 · 112 只样本不足已排期（最早 2026-11-30）')
      expect(hint.attributes('title')).toContain('未满一年的排到「上市首日 + 365 天」')
    })

    it('says so when there is nothing to recompute', async () => {
      window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
      vi.mocked(api.fetchStrategyPoolResearchEstimate).mockResolvedValue({
        ...estimate,
        pending_symbols: 0,
        reusable_symbols: 4417,
      })
      const wrapper = mount(StrategyPoolPage)
      await flushPromises()

      expect(wrapper.get('[data-testid="pool-research-estimate"]').text())
        .toBe('没有待更新：4417 只可直接复用')
    })

    it('stays quiet while a pool job is running', async () => {
      window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
      vi.mocked(api.fetchStrategyPoolJobs).mockResolvedValue([
        { job_id: 'full-1', mode: 'full', status: 'running', created_at: Date.now() / 1000 },
      ])
      vi.mocked(api.getStrategyPoolJob).mockResolvedValue({
        job_id: 'full-1',
        status: 'running',
        progress: 0.01,
        metadata: { total_symbols: 151, finished_symbols: 12 },
      })
      vi.mocked(api.fetchStrategyPoolResearchEstimate).mockResolvedValue(estimate)
      const wrapper = mount(StrategyPoolPage)
      await flushPromises()

      expect(api.fetchStrategyPoolResearchEstimate).not.toHaveBeenCalled()
      expect(wrapper.find('[data-testid="pool-research-estimate"]').exists()).toBe(false)
    })

    it('leaves the hint out when the estimate fails instead of showing a stale number', async () => {
      window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
      vi.mocked(api.fetchStrategyPoolResearchEstimate).mockRejectedValue(new Error('统计失败'))
      const wrapper = mount(StrategyPoolPage)
      await flushPromises()

      expect(wrapper.find('[data-testid="pool-research-estimate"]').exists()).toBe(false)
    })
  })

  describe('特别关注', () => {
    it('follows a symbol from the maintenance column into the focus panel', async () => {
      window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
      const wrapper = mount(StrategyPoolPage)
      await flushPromises()

      expect(wrapper.get('[data-testid="pool-focus-empty"]').text()).toContain('维护列点击关注')
      expect(wrapper.find('[data-testid="focus-row-600000"]').exists()).toBe(false)

      await wrapper.get('[data-testid="pool-follow-entry-600000"]').trigger('click')
      await flushPromises()

      expect(api.addStrategyPoolFocus).toHaveBeenCalledWith('SH', '600000', '测试600000')
      const focusRow = wrapper.get('[data-testid="focus-row-600000"]')
      expect(focusRow.text()).toContain('SH 600000')
      expect(focusRow.get('[data-testid="focus-follow-entry-600000"]').text()).toBe('取消关注')
      expect(wrapper.find('[data-testid="pool-focus-empty"]').exists()).toBe(false)
    })

    it('unfollows from the focus panel and falls back to the hint', async () => {
      window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
      vi.mocked(api.fetchStrategyPoolFocus).mockResolvedValue([
        { market: 'SH', code: '600000', name: '测试600000' },
      ])
      const wrapper = mount(StrategyPoolPage)
      await flushPromises()

      expect(wrapper.find('[data-testid="focus-row-600000"]').exists()).toBe(true)
      expect(wrapper.get('[data-testid="pool-follow-entry-600000"]').text()).toBe('取消关注')

      await wrapper.get('[data-testid="focus-follow-entry-600000"]').trigger('click')
      await flushPromises()

      expect(api.removeStrategyPoolFocus).toHaveBeenCalledWith('SH', '600000')
      expect(wrapper.find('[data-testid="focus-row-600000"]').exists()).toBe(false)
      expect(wrapper.find('[data-testid="pool-focus-empty"]').exists()).toBe(true)
    })

    it('follows a symbol straight from a buy signal row', async () => {
      window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
      const wrapper = mount(StrategyPoolPage)
      await flushPromises()

      const button = wrapper.get('[data-testid="pool-buy-follow-600000"]')
      expect(button.text()).toBe('关注')
      expect(button.attributes('title')).toBe('关注 SH 600000，加入特别关注面板')

      await button.trigger('click')
      await flushPromises()

      expect(api.addStrategyPoolFocus).toHaveBeenCalledWith('SH', '600000', '测试600000')
      expect(wrapper.get('[data-testid="pool-buy-follow-600000"]').text()).toBe('取消关注')
      expect(wrapper.find('[data-testid="focus-row-600000"]').exists()).toBe(true)
    })

    it('gives sell-rule, risk-exit and holding rows the same switch and one name list', async () => {
      window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
      vi.mocked(api.getStrategyPoolJob).mockResolvedValue({
        job_id: 'signal-1',
        status: 'completed',
        result: {
          mode: 'signals',
          objective: 'annual_return',
          holding_scope: 'per_stock_strategy',
          holding_snapshot_complete: true,
          as_of_date: '2026-10-01',
          buy_signals: [buySignal],
          sell_signals: [{ ...buySignal, signal_type: 'sell', status: 'sell' }],
          states: [buySignal],
          model_portfolio: {
            available: true,
            holding_scope: 'per_stock_strategy',
            holdings: [
              { market: 'SH', code: '600000', name: '测试600000', status: 'held' },
              // 持仓行没给名字时也要能关注：名称留空，市场与代码照样传过去。
              { market: 'SZ', code: '000002', status: 'held' },
            ],
            risk_exit_signals: [
              { market: 'SZ', code: '000002', name: '测试000002', date: '2026-10-01' },
            ],
          },
        },
      })
      vi.mocked(api.fetchStrategyPoolFocus).mockResolvedValue([
        { market: 'SH', code: '600000', name: '测试600000' },
      ])
      vi.mocked(api.addStrategyPoolFocus).mockImplementation(async (market, code, name) => [
        { market: 'SH', code: '600000', name: '测试600000' },
        { market: market as 'SH' | 'SZ', code, name },
      ])
      const wrapper = mount(StrategyPoolPage)
      await flushPromises()

      // 同一份名单：已关注的行不管出现在哪个框里，都是「取消关注」。
      expect(wrapper.get('[data-testid="pool-buy-follow-600000"]').text()).toBe('取消关注')
      expect(wrapper.get('[data-testid="pool-sell-rule-follow-600000"]').text()).toBe('取消关注')
      expect(wrapper.get('[data-testid="pool-holding-follow-600000"]').text()).toBe('取消关注')
      expect(wrapper.get('[data-testid="pool-holding-follow-000002"]').text()).toBe('关注')
      expect(wrapper.get('[data-testid="pool-sell-risk-follow-000002"]').text()).toBe('关注')

      await wrapper.get('[data-testid="pool-sell-risk-follow-000002"]').trigger('click')
      await flushPromises()

      expect(api.addStrategyPoolFocus).toHaveBeenCalledWith('SZ', '000002', '测试000002')
      expect(wrapper.get('[data-testid="pool-sell-risk-follow-000002"]').text()).toBe('取消关注')
      expect(wrapper.get('[data-testid="pool-holding-follow-000002"]').text()).toBe('取消关注')
      expect(wrapper.get('[data-testid="pool-buy-follow-600000"]').text()).toBe('取消关注')

      // 再从持仓行取消：同一个标的在四个位置一起回到「关注」，别的行不受影响。
      vi.mocked(api.removeStrategyPoolFocus).mockResolvedValue([
        { market: 'SH', code: '600000', name: '测试600000' },
      ])
      await wrapper.get('[data-testid="pool-holding-follow-000002"]').trigger('click')
      await flushPromises()

      expect(api.removeStrategyPoolFocus).toHaveBeenCalledWith('SZ', '000002')
      expect(wrapper.get('[data-testid="pool-holding-follow-000002"]').text()).toBe('关注')
      expect(wrapper.get('[data-testid="pool-sell-risk-follow-000002"]').text()).toBe('关注')
      expect(wrapper.get('[data-testid="pool-buy-follow-600000"]').text()).toBe('取消关注')
    })

    it('orders followed symbols by status priority, not by follow time', async () => {
      window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
      // 600001 没有信号行 → 待刷新；600000 触发买入，必须排在前面。
      vi.mocked(api.fetchStrategyPoolFocus).mockResolvedValue([
        { market: 'SH', code: '600001', name: '测试600001' },
        { market: 'SH', code: '600000', name: '测试600000' },
      ])
      const wrapper = mount(StrategyPoolPage)
      await flushPromises()

      expect(
        wrapper.findAll('tbody tr[data-testid^="focus-row-"]').map((row) => row.attributes('data-testid')),
      ).toEqual(['focus-row-600000', 'focus-row-600001'])
    })

    it('keeps the focus panel out of search, sorting and pagination', async () => {
      window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
      vi.mocked(api.fetchStrategyPoolFocus).mockResolvedValue([
        { market: 'SH', code: '600001', name: '测试600001' },
      ])
      const wrapper = mount(StrategyPoolPage)
      await flushPromises()

      await wrapper.get('[data-testid="pool-entry-search"]').setValue('600000')
      await flushPromises()

      expect(wrapper.findAll('tbody tr[data-testid^="pool-row-"]')).toHaveLength(1)
      expect(wrapper.find('[data-testid="pool-row-600001"]').exists()).toBe(false)
      expect(wrapper.find('[data-testid="focus-row-600001"]').exists()).toBe(true)

      await wrapper.get('thead button').trigger('click')
      expect(wrapper.findAll('tbody tr[data-testid^="focus-row-"]')).toHaveLength(1)
    })

    it('hides followed symbols that are missing from the pool and counts them', async () => {
      window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
      vi.mocked(api.fetchStrategyPoolFocus).mockResolvedValue([
        { market: 'SH', code: '600000', name: '测试600000' },
        { market: 'SZ', code: '000001', name: '平安银行' },
      ])
      vi.mocked(api.addStrategyPoolFocus).mockImplementation(async (market, code, name) => [
        { market: 'SH', code: '600000', name: '测试600000' },
        { market: 'SZ', code: '000001', name: '平安银行' },
        { market: market as 'SH' | 'SZ', code, name },
      ])
      const wrapper = mount(StrategyPoolPage)
      await flushPromises()

      expect(wrapper.findAll('tbody tr[data-testid^="focus-row-"]')).toHaveLength(1)
      expect(wrapper.get('[data-testid="pool-focus-missing"]').text()).toContain('1 只关注标的暂时不在策略池')

      await wrapper.get('[data-testid="pool-follow-entry-600001"]').trigger('click')
      await flushPromises()

      // 600001 在策略池里，进面板；只有 SZ000001 仍然缺席，计数不变。
      expect(wrapper.findAll('tbody tr[data-testid^="focus-row-"]')).toHaveLength(2)
      expect(wrapper.get('[data-testid="pool-focus-missing"]').text()).toContain('1 只')
    })

    it('rolls the panel back and warns when the backend rejects a follow', async () => {
      window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
      vi.mocked(api.addStrategyPoolFocus).mockRejectedValueOnce(
        new ApiError('特别关注最多 100 只。', 422),
      )
      const wrapper = mount(StrategyPoolPage)
      await flushPromises()

      await wrapper.get('[data-testid="pool-follow-entry-600000"]').trigger('click')
      await flushPromises()

      expect(wrapper.get('[role="alert"]').text()).toContain('特别关注最多 100 只')
      expect(wrapper.find('[data-testid="focus-row-600000"]').exists()).toBe(false)
      expect(wrapper.get('[data-testid="pool-follow-entry-600000"]').text()).toBe('关注')
    })

    it('reads the focus list once on load without re-fetching it on objective switch', async () => {
      window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
      const wrapper = mount(StrategyPoolPage)
      await flushPromises()

      await wrapper.get('[data-testid="pool-objective"]').setValue('sharpe')
      await flushPromises()

      expect(api.fetchStrategyPoolFocus).toHaveBeenCalledTimes(1)
    })
    it('prunes followed symbols that are not in the pool', async () => {
      window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
      vi.mocked(api.fetchStrategyPoolFocus).mockResolvedValue([
        { market: 'SH', code: '600000', name: '测试600000' },
        { market: 'SZ', code: '000001', name: '平安银行' },
        { market: 'SZ', code: '000002', name: '万科A' },
      ])
      vi.mocked(api.removeStrategyPoolFocus)
        .mockResolvedValueOnce([
          { market: 'SH', code: '600000', name: '测试600000' },
          { market: 'SZ', code: '000002', name: '万科A' },
        ])
        .mockResolvedValueOnce([{ market: 'SH', code: '600000', name: '测试600000' }])
      const wrapper = mount(StrategyPoolPage)
      await flushPromises()

      expect(wrapper.get('[data-testid="pool-focus-missing"]').text()).toContain('2 只')
      expect(wrapper.findAll('tbody tr[data-testid^="focus-row-"]')).toHaveLength(1)

      await wrapper.get('[data-testid="pool-focus-prune"]').trigger('click')
      await flushPromises()

      // 只删池外的那两只，在池里的关注保持不动。
      expect(api.removeStrategyPoolFocus).toHaveBeenNthCalledWith(1, 'SZ', '000001')
      expect(api.removeStrategyPoolFocus).toHaveBeenNthCalledWith(2, 'SZ', '000002')
      expect(wrapper.find('[data-testid="pool-focus-missing"]').exists()).toBe(false)
      expect(wrapper.findAll('tbody tr[data-testid^="focus-row-"]')).toHaveLength(1)
      expect(wrapper.find('[role="alert"]').exists()).toBe(false)
    })

    it('re-reads the focus list when a prune request fails halfway', async () => {
      window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
      vi.mocked(api.fetchStrategyPoolFocus)
        .mockResolvedValueOnce([{ market: 'SZ', code: '000001', name: '平安银行' }])
        .mockResolvedValueOnce([])
      vi.mocked(api.removeStrategyPoolFocus).mockRejectedValue(
        new ApiError('特别关注暂时无法保存', 503),
      )
      const wrapper = mount(StrategyPoolPage)
      await flushPromises()

      await wrapper.get('[data-testid="pool-focus-prune"]').trigger('click')
      await flushPromises()

      expect(wrapper.get('[role="alert"]').text()).toContain('特别关注暂时无法保存')
      // 后端已经删掉了第一只，重读让界面跟库一致，而不是回滚成旧名单。
      expect(wrapper.find('[data-testid="pool-focus-missing"]').exists()).toBe(false)
      expect(wrapper.find('[data-testid="pool-focus-empty"]').exists()).toBe(true)
    })
  })

})
