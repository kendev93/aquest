import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import * as api from '../api/strategyPool'
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
      closed_trades: 12,
    },
    holdout: {
      total_return: 0.08,
      max_drawdown: -0.1,
      closed_trades: 4,
    },
  },
  quality: { badges: ['样本较充分'] },
}

function entry(code: string, score: number): StrategyPoolEntry {
  const candidate: StrategyPoolCandidate = {
    ...baseCandidate,
    candidate_id: `candidate-${code}`,
    metrics: {
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
  annual_return: 0.24,
  sharpe: 1.35,
  win_rate: 0.62,
  payoff_ratio: 1.8,
}

describe('StrategyPoolPage', () => {
  beforeEach(() => {
    window.localStorage.clear()
    vi.mocked(api.fetchStrategyPoolEntries).mockResolvedValue([entry('600000', 0.24), entry('600001', 0.12)])
    vi.mocked(api.fetchStrategyPoolJobs).mockResolvedValue([])
    vi.mocked(api.createStrategyPoolJob).mockResolvedValue({ job_id: 'signal-1', status: 'queued' })
    vi.mocked(api.getStrategyPoolJob).mockResolvedValue({
      job_id: 'signal-1',
      status: 'completed',
      result: {
        mode: 'signals',
        objective: 'annual_return',
        as_of_date: '2026-10-01',
        buy_signals: [buySignal],
        sell_signals: [],
        states: [buySignal],
        model_portfolio: { available: true, holdings: [], risk_exit_signals: [] },
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
      expect(wrapper.get('thead').text()).toContain('最大回撤')
      expect(row.text()).toContain('-18.00%')
      expect(row.text()).toContain('验证 12 · 留出 4 笔')
    },
  )

  it('shows CPU pressure and its dynamic dispatch limit during a full scan', async () => {
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
        current_parallelism: 4,
        maximum_parallelism: 6,
        memory_worker_limit: 6,
        available_memory_mb: 9_000,
        cpu_worker_limit: 5,
        cpu_load_average_1m: 6.2,
        visible_cpus: 8,
        active_symbols: [
          { symbol: 'SH:600000', name: '浦发银行', progress: 0.37, phase_label: '规则 1/6 · 风控搜索' },
          { symbol: 'SH:600004', name: '白云机场', progress: 0.62, phase_label: '规则 2/6 · 验证' },
          { symbol: 'SZ:000001', progress: 0.18, phase_label: '发现买卖条件' },
          { symbol: 'SZ:000002', name: '万科A', progress: 0.9, phase_label: '留出评估' },
        ],
      },
    })

    const wrapper = mount(StrategyPoolPage)
    await flushPromises()

    expect(wrapper.get('[data-testid="pool-parallelism"]').text()).toContain('标的并行 4 / 最大 6')
    expect(wrapper.get('[data-testid="pool-parallelism"]').text()).toContain(
      'CPU允许 5 个标的并发（8核，系统1分钟负载 6.2）',
    )
    expect(wrapper.get('[data-testid="pool-progress-count"]').text()).toContain(
      '已处理 85 / 5223 只（完成 79 · 进行中 4 · 跳过 1 · 失败 5）',
    )
    expect(wrapper.findAll('[data-testid^="pool-active-symbol-"]')).toHaveLength(4)
    expect(
      wrapper.get('[data-testid="pool-active-symbol-SH:600000"]').text(),
    ).toContain('SH:600000 · 浦发银行')
    expect(wrapper.get('[data-testid="pool-active-symbol-SZ:000001"]').text()).toContain('SZ:000001')
    expect(wrapper.get('[data-testid="pool-active-symbol-SH:600000"]').text()).toContain('37%')
    expect(wrapper.get('[data-testid="pool-active-symbol-SH:600000"] [role="progressbar"]').attributes('aria-valuenow')).toBe('37')
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

  it('shows a single rerun candidate as pending and applies it only after an explicit click', async () => {
    window.localStorage.setItem('indicator-lab.strategy-pool.objective', 'annual_return')
    vi.mocked(api.createStrategyPoolJob)
      .mockResolvedValueOnce({ job_id: 'signal-1', status: 'queued' })
      .mockResolvedValueOnce({ job_id: 'single-1', status: 'queued' })
    vi.mocked(api.getStrategyPoolJob)
      .mockResolvedValueOnce({
        job_id: 'signal-1',
        status: 'completed',
        result: { mode: 'signals', objective: 'annual_return', buy_signals: [], sell_signals: [], states: [] },
      })
      .mockResolvedValueOnce({
        job_id: 'single-1',
        status: 'completed',
        result: {
          mode: 'single',
          single_candidate: {
            market: 'SH',
            code: '600000',
            benchmark: { full_period: { total_return: 0.2 } },
            annual_candidate: baseCandidate,
            sharpe_candidate: null,
          },
        },
      })
    const wrapper = mount(StrategyPoolPage)
    await flushPromises()
    await wrapper.get('[data-testid="pool-single-code"]').setValue('600000')
    await wrapper.get('[data-testid="pool-rerun-single"]').trigger('click')
    await flushPromises()

    expect(wrapper.get('[data-testid="pool-single-candidates"]').text()).toContain('待应用候选')
    await wrapper.get('[data-testid="pool-single-candidates"] button').trigger('click')
    await flushPromises()

    expect(api.saveStrategyPoolOverride).toHaveBeenCalledWith(
      'SH',
      '600000',
      'annual_return',
      baseCandidate,
      { full_period: { total_return: 0.2 } },
    )
  })
})
