import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

import * as api from '../api/strategyDiscovery'
import StrategyDiscoveryPage from './StrategyDiscoveryPage.vue'

vi.mock('../api/strategyDiscovery', async () => {
  const actual = await vi.importActual<typeof import('../api/strategyDiscovery')>('../api/strategyDiscovery')
  return {
    ...actual,
    fetchStrategyDiscoveryCapabilities: vi.fn(),
    createStrategyDiscovery: vi.fn(),
    getStrategyDiscovery: vi.fn(),
    getStrategyDiscoveryResults: vi.fn(),
    fetchStrategyDiscoveryHistory: vi.fn(),
    getStrategyDiscoveryHistory: vi.fn(),
    deleteStrategyDiscoveryHistory: vi.fn(),
  }
})

describe('StrategyDiscoveryPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    sessionStorage.clear()
    vi.mocked(api.fetchStrategyDiscoveryCapabilities).mockResolvedValue({
      enabled: true,
      indicator_count: 20,
      signal_count: 15,
      optuna_available: true,
      lightgbm_available: true,
      indicators: [
        { id: 'technical.ma5', display_name: 'MA5', rankable: true },
        { id: 'technical.ma20', display_name: 'MA20', rankable: true },
        { id: 'technical.rsi14', display_name: 'RSI14', rankable: true },
      ],
    })
    vi.mocked(api.fetchStrategyDiscoveryHistory).mockResolvedValue([])
    vi.mocked(api.createStrategyDiscovery).mockResolvedValue({ job_id: 'discovery-1', status: 'queued' })
    vi.mocked(api.getStrategyDiscovery).mockResolvedValue({
      job_id: 'discovery-1', status: 'completed', progress: 1, total_candidates: 4, total_scanned: 4,
      errors: 0, skipped: 0, error: null, phase: 'completed', result: null,
    })
    vi.mocked(api.getStrategyDiscoveryResults).mockResolvedValue({
      request: {},
      results_by_sharpe: [{
        candidate_hash: 's1', complexity: 3, human_readable_rules: '买入[MA5 > MA20]；卖出[RSI14 > 70]；持仓3个',
        config: { universe: 'all', selected_signals: [], combine_mode: 'all', minimum_matches: null, ranking_value: 'technical.ma5', max_positions: 3 },
        metrics: {
          validation: { annual_return: 0.2, sharpe: 1.4, max_drawdown: 0.1, total_trades: 12 },
          holdout: { annual_return: 0.1, sharpe: 0.9, max_drawdown: 0.12, total_trades: 5 },
        },
      }],
      results_by_return: [],
      search_metadata: { evaluated_trials: 4 },
      validation_metadata: { leaderboard_period: 'validation' },
    })
  })

  it('submits only the user-facing universe and holding count', async () => {
    const wrapper = mount(StrategyDiscoveryPage)
    await flushPromises()
    await wrapper.get('[data-testid="strategy-discovery-positions"]').setValue('3')
    await wrapper.get('[data-testid="strategy-discovery-config"]').trigger('submit')
    await flushPromises()

    expect(api.createStrategyDiscovery).toHaveBeenCalledWith(expect.objectContaining({
      max_positions: 3, max_trials: 1000,
    }))
    expect(api.createStrategyDiscovery.mock.calls[0]?.[0]).not.toHaveProperty('start_date')
    expect(api.createStrategyDiscovery.mock.calls[0]?.[0]).not.toHaveProperty('end_date')
    expect(wrapper.get('[data-testid="strategy-discovery-results"]').text()).toContain('MA5 > MA20')
    expect(wrapper.get('[data-testid="strategy-discovery-sharpe-board"]').text()).toContain('夏普榜')
  })

  it('stores a candidate configuration before applying it to portfolio backtest', async () => {
    const wrapper = mount(StrategyDiscoveryPage)
    await flushPromises()
    await wrapper.get('[data-testid="strategy-discovery-config"]').trigger('submit')
    await flushPromises()
    await wrapper.get('[data-testid="strategy-discovery-apply-s1"]').trigger('click')

    expect(JSON.parse(sessionStorage.getItem('indicator-lab.strategy-discovery.apply') ?? '{}')).toMatchObject({
      ranking_value: 'technical.ma5',
      max_positions: 3,
    })
  })

  it('restores user-facing settings when loading experiment history', async () => {
    vi.mocked(api.fetchStrategyDiscoveryHistory).mockResolvedValueOnce([{
      history_id: 'history-1',
      created_at: '2026-09-10T00:00:00',
      total_trials: 12,
      best_sharpe: { candidate_hash: 's1' },
      best_return: null,
    }])
    vi.mocked(api.getStrategyDiscoveryHistory).mockResolvedValueOnce({
      history_id: 'history-1',
      request: {
        base_config: { universe: 'sh', max_positions: 4, universe_file: null },
        splits: {
          search: { start_date: '2020-01-01', end_date: '2022-12-31' },
          validation: { start_date: '2023-01-01', end_date: '2023-12-31' },
          holdout: { start_date: '2024-01-01', end_date: '2024-12-31' },
        },
      },
      results_by_sharpe: [],
      results_by_return: [],
      search_metadata: {},
      validation_metadata: {},
    })
    const wrapper = mount(StrategyDiscoveryPage)
    await flushPromises()
    await wrapper.get('[data-testid="strategy-discovery-history"] .table-link').trigger('click')
    await flushPromises()

    expect((wrapper.get('[data-testid="strategy-discovery-universe"]').element as HTMLSelectElement).value).toBe('sh')
    expect((wrapper.get('[data-testid="strategy-discovery-positions"]').element as HTMLInputElement).value).toBe('4')
  })

  it('shows the elapsed time for each historical experiment', async () => {
    vi.mocked(api.fetchStrategyDiscoveryHistory).mockResolvedValueOnce([{
      history_id: 'history-2',
      created_at: '2026-09-10T00:00:00',
      total_trials: 120,
      duration_seconds: 3723.5,
      best_sharpe: null,
      best_return: null,
    }])
    const wrapper = mount(StrategyDiscoveryPage)
    await flushPromises()

    expect(wrapper.get('[data-testid="strategy-discovery-history"]').text()).toContain('1 小时 2 分钟')
  })

  it('passes optional local instrument scope to the discovery request', async () => {
    const wrapper = mount(StrategyDiscoveryPage)
    await flushPromises()
    await wrapper.get('[data-testid="strategy-discovery-scope-type-fund"]').setValue(true)
    await wrapper.get('[data-testid="strategy-discovery-config"]').trigger('submit')
    await flushPromises()

    expect(api.createStrategyDiscovery).toHaveBeenCalledWith(expect.objectContaining({ instrument_types: ['fund'] }))
  })

  it('submits user-required buy and sell conditions as hard constraints', async () => {
    const wrapper = mount(StrategyDiscoveryPage)
    await flushPromises()
    await wrapper.get('[data-testid="strategy-required-buy-rule-add-condition"]').trigger('click')
    await wrapper.get('[data-testid="strategy-required-sell-rule-add-condition"]').trigger('click')
    await wrapper.get('#strategy-required-sell-rule-right-type-0').setValue('constant')
    await wrapper.get('#strategy-required-sell-rule-right-0').setValue(70)
    await wrapper.get('[data-testid="strategy-discovery-config"]').trigger('submit')
    await flushPromises()

    expect(api.createStrategyDiscovery).toHaveBeenCalledWith(expect.objectContaining({
      required_buy_conditions: [{
        left_value: 'technical.ma5',
        operator: 'gt',
        right_value: 'technical.ma20',
        right_constant: null,
      }],
      required_sell_conditions: [{
        left_value: 'technical.ma5',
        operator: 'gt',
        right_value: null,
        right_constant: 70,
      }],
    }))
    expect(wrapper.get('[data-testid="strategy-required-buy-rule"]').text()).toContain('硬约束')
  })

  it('submits without a date range and uses all local history by default', async () => {
    const wrapper = mount(StrategyDiscoveryPage)
    await flushPromises()
    await wrapper.get('[data-testid="strategy-discovery-positions"]').setValue('3')
    await wrapper.get('[data-testid="strategy-discovery-config"]').trigger('submit')
    await flushPromises()

    expect(api.createStrategyDiscovery).toHaveBeenCalledWith(expect.objectContaining({ max_positions: 3 }))
    expect(api.createStrategyDiscovery.mock.calls[0]?.[0]).not.toHaveProperty('start_date')
    expect(api.createStrategyDiscovery.mock.calls[0]?.[0]).not.toHaveProperty('end_date')
  })

  it('shows overall progress, current phase, throughput, and remaining estimate', async () => {
    vi.useFakeTimers()
    vi.mocked(api.getStrategyDiscovery)
      .mockResolvedValueOnce({
        job_id: 'discovery-1', status: 'running', progress: 0.1, total_candidates: 8, total_scanned: 1,
        errors: 0, skipped: 0, error: null, phase: 'searching', result: null,
        metadata: {
          overall_progress: 0.24,
          phase_label: '寻优候选',
          phase_current: 8,
          phase_total: 1000,
          estimated_total_evaluations: 1570,
          throughput_per_second: 2.5,
          estimated_seconds_remaining: 120,
          best_sharpe: 1.2,
          best_return: 0.18,
        },
      })
      .mockResolvedValueOnce({
        job_id: 'discovery-1', status: 'completed', progress: 1, total_candidates: 1, total_scanned: 1,
        errors: 0, skipped: 0, error: null, phase: 'completed', result: null, metadata: {},
      })
    const wrapper = mount(StrategyDiscoveryPage)
    await flushPromises()
    const submitPromise = wrapper.get('[data-testid="strategy-discovery-config"]').trigger('submit')
    await flushPromises()

    expect(wrapper.get('[data-testid="strategy-discovery-overall-progress"]').text()).toContain('总体 24%')
    expect(wrapper.get('[data-testid="strategy-discovery-progress"]').text()).toContain('寻优候选')
    expect(wrapper.get('[data-testid="strategy-discovery-remaining"]').text()).toContain('2 分钟')
    await vi.advanceTimersByTimeAsync(220)
    await flushPromises()
    await submitPromise
    vi.useRealTimers()
  })

  it('shows the backend diagnosis instead of replacing it with a generic failure', async () => {
    vi.mocked(api.getStrategyDiscovery).mockResolvedValueOnce({
      job_id: 'discovery-1', status: 'failed', progress: 0, total_candidates: 0, total_scanned: 0,
      errors: 0, skipped: 0, error: '本地 DuckDB 没有可用行情，请先在“本地行情”页面导入日线数据',
      phase: 'failed', result: null, metadata: {},
    })
    const wrapper = mount(StrategyDiscoveryPage)
    await flushPromises()
    await wrapper.get('[data-testid="strategy-discovery-config"]').trigger('submit')
    await flushPromises()

    expect(wrapper.get('[data-testid="strategy-discovery-message"]').text()).toContain('先在“本地行情”页面导入')
  })
})
