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
      sharpe: 1.35,
      win_rate: 0.62,
      payoff_ratio: 1.8,
      max_drawdown: -0.18,
      closed_trades: 12,
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

    expect(api.saveStrategyPoolOverride).toHaveBeenCalledWith('SH', '600000', 'annual_return', baseCandidate)
  })
})
