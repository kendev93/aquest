import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import * as api from '../api/strategyPool'
import * as marketDataApi from '../api/marketData'
import { FormulaScreenApiError } from '../api/formulaScreen'
import type { StrategyPoolCandidate, StrategyPoolEntry, StrategyPoolSignal } from '../types'
import StrategyPoolPage from './StrategyPoolPage.vue'

vi.mock('../api/strategyPool', () => ({
  cancelStrategyPoolJob: vi.fn(),
  clearStrategyPoolOverride: vi.fn(),
  createStrategyPoolJob: vi.fn(),
  fetchStrategyPoolEntries: vi.fn(),
  fetchStrategyPoolJobs: vi.fn(),
  getStrategyPoolJob: vi.fn(),
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
    automatic_candidates: { annual_return: candidate, sharpe: candidate },
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
  annual_return: 0.24,
  sharpe: 1.35,
  win_rate: 0.62,
  payoff_ratio: 1.8,
}

describe('StrategyPoolPage', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    window.localStorage.clear()
    vi.mocked(api.fetchStrategyPoolEntries).mockResolvedValue([entry('600000', 0.24), entry('600001', 0.12)])
    vi.mocked(api.fetchStrategyPoolJobs).mockResolvedValue([])
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

    expect(window.localStorage.getItem('indicator-lab.strategy-pool.objective')).toBe('annual_return')
    expect(api.createStrategyPoolJob).toHaveBeenCalledWith({ mode: 'signals', objective: 'annual_return' })
    expect(wrapper.get('[data-testid="pool-buy-signals"]').text()).toContain('SH600000')
    expect(wrapper.get('[data-testid="pool-sell-signals"]').text()).toContain('当前没有卖出条件')
  })

  it('switches the saved objective and lets metric sorting change only the table view', async () => {
    window.localStorage.setItem('indicator-lab.strategy-pool.objective', 'annual_return')
    const wrapper = mount(StrategyPoolPage)
    await flushPromises()

    await wrapper.get('[data-testid="pool-objective"]').setValue('sharpe')
    await flushPromises()
    expect(window.localStorage.getItem('indicator-lab.strategy-pool.objective')).toBe('sharpe')
    expect(api.fetchStrategyPoolEntries).toHaveBeenCalledWith('sharpe')

    await wrapper.get('thead button').trigger('click')
    expect(wrapper.findAll('tbody tr')[0]?.text()).toContain('600000')
  })

  it('searches the status table by code and Chinese stock name', async () => {
    window.localStorage.setItem('indicator-lab.strategy-pool.objective', 'annual_return')
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

  it('shows simulated current holdings in a separate panel from daily signal status', async () => {
    window.localStorage.setItem('indicator-lab.strategy-pool.objective', 'annual_return')
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
          }],
          risk_exit_signals: [],
        },
      },
    })

    const wrapper = mount(StrategyPoolPage)
    await flushPromises()

    expect(wrapper.get('[data-testid="pool-current-holdings"]').text()).toContain('今日持仓中 1')
    expect(wrapper.get('[data-testid="pool-current-holdings"]').text()).toContain('SH600000 · 浦发银行')
    expect(wrapper.get('[data-testid="pool-current-holdings"]').text()).toContain('今日触发卖出 · 下一交易日卖出')
    expect(wrapper.get('[data-testid="pool-current-holdings"]').text()).not.toContain('成本')
    expect(wrapper.get('[data-testid="pool-current-holdings"]').text()).not.toContain('浮盈')
    expect(wrapper.get('[data-testid="pool-row-600000"]').text()).toContain('持仓中')
    expect(wrapper.get('[data-testid="pool-row-600000"]').text()).toContain('触发卖出')
    expect(wrapper.get('[data-testid="pool-row-600000"]').text()).not.toContain('空仓')
  })

  it('replaces a legacy five-slot signal snapshot before showing per-stock holdings', async () => {
    window.localStorage.setItem('indicator-lab.strategy-pool.objective', 'annual_return')
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

    expect(api.createStrategyPoolJob).toHaveBeenCalledWith({ mode: 'signals', objective: 'annual_return' })
    expect(wrapper.get('[data-testid="pool-current-holdings"]').text()).not.toContain('旧版组合持仓')
    expect(wrapper.get('[data-testid="pool-current-holdings"]').text()).toContain('当前没有仍处于持仓状态的标的')
  })

  it('restores the latest matching signal result while a portfolio backtest is active', async () => {
    vi.useFakeTimers()
    window.localStorage.setItem('indicator-lab.strategy-pool.objective', 'annual_return')
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
      expect(wrapper.get('[data-testid="pool-job-progress"]').text()).toContain('正在回测组合留出段')
    } finally {
      wrapper.unmount()
      vi.useRealTimers()
    }
  })

  it('does not rescan on page entry when data and selected strategies match the saved signal scan', async () => {
    window.localStorage.setItem('indicator-lab.strategy-pool.objective', 'annual_return')
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
        created_at: 200,
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
    window.localStorage.setItem('indicator-lab.strategy-pool.objective', 'annual_return')
    vi.mocked(api.fetchStrategyPoolJobs).mockResolvedValueOnce([
      {
        job_id: 'signals-failed',
        status: 'failed',
        mode: 'signals',
        objective: 'annual_return',
        created_at: 1_790_984_425,
        finished_at: 1_790_984_500,
      },
      {
        job_id: 'full-cancelled',
        status: 'cancelled',
        mode: 'full',
        created_at: 1_790_957_169,
        finished_at: 1_790_984_378,
      },
      {
        job_id: 'signals-before-research',
        status: 'completed',
        mode: 'signals',
        objective: 'annual_return',
        created_at: 1_790_955_030,
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
    expect(wrapper.get('[role="alert"]').text()).toContain('已暂停自动重试')
    expect(wrapper.get('[data-testid="pool-row-600000"]').text()).toContain('策略可用 · 信号待刷新')
  })

  it('runs a full signal scan when the stored market snapshot is older than local行情', async () => {
    window.localStorage.setItem('indicator-lab.strategy-pool.objective', 'annual_return')
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
        created_at: 200,
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

    expect(api.createStrategyPoolJob).toHaveBeenCalledWith({ mode: 'signals', objective: 'annual_return' })
  })

  it('runs a full signal scan when a full reselection finished after the saved scan', async () => {
    window.localStorage.setItem('indicator-lab.strategy-pool.objective', 'annual_return')
    vi.mocked(api.fetchStrategyPoolJobs).mockResolvedValueOnce([
      { job_id: 'full-newer', status: 'completed', mode: 'full', created_at: 300 },
      {
        job_id: 'signals-older',
        status: 'completed',
        mode: 'signals',
        objective: 'annual_return',
        created_at: 200,
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

    expect(api.createStrategyPoolJob).toHaveBeenCalledWith({ mode: 'signals', objective: 'annual_return' })
  })

  it('rescans one changed strategy only for its symbol', async () => {
    window.localStorage.setItem('indicator-lab.strategy-pool.objective', 'annual_return')
    vi.mocked(api.fetchStrategyPoolJobs).mockResolvedValueOnce([
      {
        job_id: 'signals-before-edit',
        status: 'completed',
        mode: 'signals',
        objective: 'annual_return',
        created_at: 200,
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

    expect(api.createStrategyPoolJob).toHaveBeenCalledWith({
      mode: 'signals',
      objective: 'annual_return',
      market: 'SH',
      code: '600001',
    })
  })

  it('paginates the market status table instead of mounting every symbol at once', async () => {
    window.localStorage.setItem('indicator-lab.strategy-pool.objective', 'annual_return')
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
      window.localStorage.setItem('indicator-lab.strategy-pool.objective', 'annual_return')
      const wrapper = mount(StrategyPoolPage)
      await flushPromises()

      const row = wrapper.get('[data-testid="pool-row-600000"]')
      expect(row.get('[data-testid="pool-entry-benchmark-600000"]').text()).toContain(
        '全段基准 +50.00%',
      )
      expect(row.get('[data-testid="pool-entry-benchmark-600000"]').text()).toContain(
        '验证基准 +10.00% · 策略超额 +14.00%',
      )
      expect(row.get('[data-testid="pool-entry-benchmark-600000"]').text()).toContain(
        '留出基准 -5.00% · 策略超额 +13.00%',
      )
      const headers = wrapper.findAll('thead th').map((header) => header.text())
      const benchmarkColumn = headers.findIndex((header) => header.includes('基准涨跌'))
      const strategyReturnColumn = headers.findIndex((header) => header.includes('策略收益（全区间）'))
      expect(benchmarkColumn).toBeLessThan(strategyReturnColumn)
      expect(wrapper.get('thead').text()).toContain('最大回撤')
      expect(row.text()).toContain('72.00%')
      expect(row.text()).toContain('-18.00%')
      expect(row.text()).toContain('验证 12 · 留出 4 笔')
    },
  )

  it('shows the two-worker cap and active ticker progress during a full scan', async () => {
    window.localStorage.setItem('indicator-lab.strategy-pool.objective', 'annual_return')
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
      '已处理 85 / 5223 只（完成 79 · 进行中 2 · 跳过 1 · 失败 5）',
    )
    expect(wrapper.findAll('[data-testid^="pool-active-symbol-"]')).toHaveLength(2)
    expect(
      wrapper.get('[data-testid="pool-active-symbol-SH:600000"]').text(),
    ).toContain('SH:600000 · 浦发银行')
    expect(wrapper.get('[data-testid="pool-active-symbol-SH:600000"]').text()).toContain('37%')
    expect(wrapper.get('[data-testid="pool-active-symbol-SH:600000"] [role="progressbar"]').attributes('aria-valuenow')).toBe('37')
  })

  it('queues a row rerun behind the active signal scan without replacing its progress', async () => {
    vi.useFakeTimers()
    window.localStorage.setItem('indicator-lab.strategy-pool.objective', 'annual_return')
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
    vi.mocked(api.getStrategyPoolJob)
      .mockRejectedValueOnce(new FormulaScreenApiError('DuckDB 暂时被占用', 503))
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
      expect(wrapper.get('[data-testid="pool-poll-warning"]').text()).toContain('自动重试')
      expect(wrapper.find('[role="alert"]').exists()).toBe(false)

      await vi.advanceTimersByTimeAsync(1_500)
      await flushPromises()
      expect(wrapper.find('[data-testid="pool-poll-warning"]').exists()).toBe(false)
    } finally {
      vi.useRealTimers()
    }
  })

  it('shows single rerun candidates after automatic update and refreshes that symbol signal', async () => {
    window.localStorage.setItem('indicator-lab.strategy-pool.objective', 'annual_return')
    vi.mocked(api.createStrategyPoolJob)
      .mockResolvedValueOnce({ job_id: 'signal-1', status: 'queued' })
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
    expect(api.createStrategyPoolJob).toHaveBeenNthCalledWith(3, {
      mode: 'signals',
      objective: 'annual_return',
      market: 'SH',
      code: '600000',
    })
    expect(wrapper.get('[data-testid="pool-row-600000"]').text()).toContain('触发卖出')
    expect(wrapper.get('[data-testid="pool-row-600001"]').text()).toContain('今日无买卖信号')
  })
})
