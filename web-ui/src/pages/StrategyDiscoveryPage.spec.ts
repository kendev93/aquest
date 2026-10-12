import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

import * as api from '../api/strategyDiscovery'
import * as jobsApi from '../api/jobs'
import StrategyDiscoveryPage from './StrategyDiscoveryPage.vue'

vi.mock('../api/strategyDiscovery', async () => {
  const actual = await vi.importActual<typeof import('../api/strategyDiscovery')>('../api/strategyDiscovery')
  return {
    ...actual,
    fetchStrategyDiscoveryCapabilities: vi.fn(),
    createStrategyDiscovery: vi.fn(),
    createStrategyDiscoveryRefinement: vi.fn(),
    cancelStrategyDiscovery: vi.fn(),
    getStrategyDiscovery: vi.fn(),
    getStrategyDiscoveryResults: vi.fn(),
    createStrategyReplay: vi.fn(),
    getStrategyReplay: vi.fn(),
    getStrategyReplayResults: vi.fn(),
    cancelStrategyReplay: vi.fn(),
    getStrategyReplayHistory: vi.fn(),
    fetchStrategyDiscoveryHistory: vi.fn(),
    getStrategyDiscoveryHistory: vi.fn(),
    deleteStrategyDiscoveryHistory: vi.fn(),
  }
})

vi.mock('../api/jobs', async () => {
  const actual = await vi.importActual<typeof import('../api/jobs')>('../api/jobs')
  return { ...actual, fetchActiveJobs: vi.fn() }
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
    vi.mocked(api.getStrategyReplayHistory).mockRejectedValue(new Error('no saved replay'))
    vi.mocked(api.createStrategyDiscovery).mockResolvedValue({ job_id: 'discovery-1', status: 'queued' })
    vi.mocked(api.createStrategyDiscoveryRefinement).mockResolvedValue({ job_id: 'refine-1', status: 'queued' })
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
    vi.mocked(jobsApi.fetchActiveJobs).mockResolvedValue([])
  })

  it('recovers and resumes a running discovery task after the page is refreshed', async () => {
    vi.mocked(jobsApi.fetchActiveJobs).mockResolvedValueOnce([{
      job_id: 'discovery-refresh', description: '策略实验室', status: 'running', progress: 0.42,
      total_candidates: 1000, total_scanned: 420, errors: 0, skipped: 0, error: null, result: null,
    }])
    vi.mocked(api.getStrategyDiscovery).mockResolvedValueOnce({
      job_id: 'discovery-refresh', status: 'completed', progress: 1, total_candidates: 1000, total_scanned: 1000,
      errors: 0, skipped: 0, error: null, phase: 'completed', result: null,
    })

    const wrapper = mount(StrategyDiscoveryPage)
    await flushPromises()

    expect(jobsApi.fetchActiveJobs).toHaveBeenCalled()
    expect(api.getStrategyDiscovery).toHaveBeenCalledWith('discovery-refresh')
    expect(wrapper.get('[data-testid="strategy-discovery-results"]').text()).toContain('MA5 > MA20')
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
    expect(vi.mocked(api.createStrategyDiscovery).mock.calls[0]?.[0]).not.toHaveProperty('start_date')
    expect(vi.mocked(api.createStrategyDiscovery).mock.calls[0]?.[0]).not.toHaveProperty('end_date')
    expect(wrapper.get('[data-testid="strategy-discovery-results"]').text()).toContain('MA5 > MA20')
    expect(wrapper.get('[data-testid="strategy-discovery-sharpe-board"]').text()).toContain('夏普榜')
  })

  it('labels unqualified timeout previews without adding them to result boards', async () => {
    vi.mocked(api.getStrategyDiscoveryResults).mockResolvedValueOnce({
      request: {},
      results_by_sharpe: [],
      results_by_return: [],
      partial: true,
      partial_candidate_count: 3,
      partial_by_sharpe: [{
        candidate_hash: 'partial-1', complexity: 2, human_readable_rules: 'preview',
        config: {}, metrics: {}, achieved_fidelity: 'medium', qualified: false,
      }],
      search_metadata: {},
      validation_metadata: { status: 'not_run' },
    })
    const wrapper = mount(StrategyDiscoveryPage)
    await flushPromises()
    await wrapper.get('[data-testid="strategy-discovery-config"]').trigger('submit')
    await flushPromises()

    expect(wrapper.get('[data-testid="strategy-discovery-message"]').text()).toContain('3 组候选')
    expect(wrapper.get('[data-testid="strategy-discovery-message"]').text()).toContain('中等精度')
    expect(wrapper.get('[data-testid="strategy-discovery-results"]').text()).not.toContain('preview')
  })

  it('stores a candidate configuration before applying it to portfolio backtest', async () => {
    const wrapper = mount(StrategyDiscoveryPage)
    await flushPromises()
    await wrapper.get('[data-testid="strategy-discovery-config"]').trigger('submit')
    await flushPromises()
    await wrapper.get('[data-testid="strategy-discovery-apply-s1"]').trigger('click')

    expect(JSON.parse(sessionStorage.getItem('aquest.strategy-discovery.apply') ?? '{}')).toMatchObject({
      ranking_value: 'technical.ma5',
      max_positions: 3,
    })
  })

  it('starts a refinement job from the selected parent candidate', async () => {
    const wrapper = mount(StrategyDiscoveryPage)
    await flushPromises()
    await wrapper.get('[data-testid="strategy-discovery-config"]').trigger('submit')
    await flushPromises()
    await wrapper.get('[data-testid="strategy-discovery-refine-s1"]').trigger('click')
    await flushPromises()

    expect(api.createStrategyDiscoveryRefinement).toHaveBeenCalledWith({
      parent_history_id: 'discovery-1',
      parent_candidate_hash: 's1',
      refinement_radius: 'medium',
      max_trials: 1000,
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

  it('labels refinement experiments with their parent candidate', async () => {
    vi.mocked(api.fetchStrategyDiscoveryHistory).mockResolvedValueOnce([{
      history_id: 'refinement-1',
      mode: 'refinement',
      parent_candidate_hash: 'parent-candidate-hash',
      created_at: '2026-09-10T00:00:00',
      total_trials: 20,
      duration_seconds: 12,
      best_sharpe: null,
      best_return: null,
    }])
    const wrapper = mount(StrategyDiscoveryPage)
    await flushPromises()

    expect(wrapper.get('[data-testid="strategy-discovery-history"]').text()).toContain(
      '二次优化 · parent-ca',
    )
  })

  it('passes board scope and surfaces a refinement request error', async () => {
    vi.mocked(api.createStrategyDiscoveryRefinement).mockRejectedValueOnce(new Error('refinement rejected'))
    const wrapper = mount(StrategyDiscoveryPage)
    await flushPromises()
    await wrapper.get('[data-testid="strategy-discovery-scope-board-main"]').setValue(true)
    await wrapper.get('[data-testid="strategy-discovery-config"]').trigger('submit')
    await flushPromises()
    await wrapper.get('[data-testid="strategy-discovery-refine-s1"]').trigger('click')
    await flushPromises()

    expect(api.createStrategyDiscovery).toHaveBeenCalledWith(expect.objectContaining({ boards: ['main'] }))
    expect(wrapper.get('[data-testid="strategy-discovery-message"]').text()).toContain('refinement rejected')
  })

  it('shows the refinement progress with phase and remaining estimate', async () => {
    const wrapper = mount(StrategyDiscoveryPage)
    await flushPromises()
    await wrapper.get('[data-testid="strategy-discovery-config"]').trigger('submit')
    await flushPromises()
    vi.mocked(api.getStrategyDiscovery)
      .mockResolvedValueOnce({
      job_id: 'refine-1', status: 'running', progress: 0.4, total_candidates: 1000,
      total_scanned: 50, errors: 0, skipped: 0, error: null, phase: 'validation', result: null,
      metadata: {
        overall_progress: 0.4,
        phase_label: '验证区间',
        phase_current: 50,
        phase_total: 50,
        estimated_total_evaluations: 1200,
        estimated_seconds_remaining: 30,
        requested_trials: 1000,
        evaluated_trials: 500,
      },
    })
      .mockResolvedValueOnce({
        job_id: 'refine-1', status: 'completed', progress: 1, total_candidates: 1000,
        total_scanned: 1000, errors: 0, skipped: 0, error: null, phase: 'completed', result: null,
        metadata: {},
      })
    vi.useFakeTimers()
    const refinementPromise = wrapper.get('[data-testid="strategy-discovery-refine-s1"]').trigger('click')
    await flushPromises()

    const progress = wrapper.get('[data-testid="strategy-refinement-progress"]')
    expect(progress.text()).toContain('验证区间')
    expect(progress.get('[data-testid="strategy-refinement-overall-progress"]').text()).toContain('总体 40%')
    expect(progress.text()).toContain('当前阶段 50 / 50')
    expect(progress.text()).toContain('预计剩余 30 秒')
    await vi.advanceTimersByTimeAsync(220)
    await refinementPromise
    vi.useRealTimers()
  })

  it('passes optional local instrument scope to the discovery request', async () => {
    const wrapper = mount(StrategyDiscoveryPage)
    await flushPromises()
    await wrapper.get('[data-testid="strategy-discovery-scope-type-stock"]').setValue(true)
    await wrapper.get('[data-testid="strategy-discovery-config"]').trigger('submit')
    await flushPromises()

    expect(api.createStrategyDiscovery).toHaveBeenCalledWith(expect.objectContaining({ instrument_types: ['stock'] }))
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

  it('submits recent-five-year mode by default', async () => {
    const wrapper = mount(StrategyDiscoveryPage)
    await flushPromises()
    await wrapper.get('[data-testid="strategy-discovery-positions"]').setValue('3')
    await wrapper.get('[data-testid="strategy-discovery-config"]').trigger('submit')
    await flushPromises()

    expect(api.createStrategyDiscovery).toHaveBeenCalledWith(expect.objectContaining({
      max_positions: 3,
      date_mode: 'recent5',
    }))
    expect(vi.mocked(api.createStrategyDiscovery).mock.calls[0]?.[0]).not.toHaveProperty('start_date')
    expect(vi.mocked(api.createStrategyDiscovery).mock.calls[0]?.[0]).not.toHaveProperty('end_date')
  })

  it('allows selecting full local history explicitly', async () => {
    const wrapper = mount(StrategyDiscoveryPage)
    await flushPromises()
    await wrapper.get('[data-testid="strategy-discovery-date-mode"]').setValue('full')
    await wrapper.get('[data-testid="strategy-discovery-config"]').trigger('submit')
    await flushPromises()

    expect(api.createStrategyDiscovery).toHaveBeenCalledWith(expect.objectContaining({
      date_mode: 'full',
    }))
    expect(vi.mocked(api.createStrategyDiscovery).mock.calls[0]?.[0]).not.toHaveProperty('start_date')
    expect(vi.mocked(api.createStrategyDiscovery).mock.calls[0]?.[0]).not.toHaveProperty('end_date')
  })

  it('submits an explicit date range when custom history is selected', async () => {
    const wrapper = mount(StrategyDiscoveryPage)
    await flushPromises()
    await wrapper.get('[data-testid="strategy-discovery-date-mode"]').setValue('custom')
    await wrapper.get('[data-testid="strategy-discovery-start-date"]').setValue('2024-01-01')
    await wrapper.get('[data-testid="strategy-discovery-end-date"]').setValue('2024-12-31')
    await wrapper.get('[data-testid="strategy-discovery-config"]').trigger('submit')
    await flushPromises()

    expect(api.createStrategyDiscovery).toHaveBeenCalledWith(expect.objectContaining({
      date_mode: 'custom',
      start_date: '2024-01-01',
      end_date: '2024-12-31',
    }))
  })

  it('runs the current leaderboard top ten and renders full-range period metrics', async () => {
    const replayResult = {
      experiment_id: 'discovery-1',
      leaderboard: 'sharpe' as const,
      data_start: '2024-01-01',
      data_end: '2026-09-08',
      data_fingerprint: 'replay-fingerprint',
      source_data_fingerprint: 'source-fingerprint',
      data_changed: true,
      candidate_count: 1,
      diagnostics: {},
      candidates: [{
        rank: 1,
        candidate_hash: 's1',
        human_readable_rules: '买入[MA5 > MA20]；卖出[RSI14 > 70]',
        config: { ranking_value: 'technical.ma5', start_date: '2024-01-01', end_date: '2026-09-08' },
        leaderboard_metrics: { annual_return: 0.2, sharpe: 1.4 },
        overall_metrics: { total_return: 0.55, annual_return: 0.22, max_drawdown: 0.12, sharpe: 1.1, total_trades: 8, win_rate: 0.5, trade_fingerprint: 'trade-fingerprint' },
        periods: {
          ytd: { key: 'ytd', label: '今年以来', start_date: '2026-01-01', end_date: '2026-09-08', bars: 3, total_return: 0.18, annual_return: 0.2, max_drawdown: 0.06, sharpe: 1.2, total_trades: 2, win_rate: 0.5, diagnostic: null },
          one_year: { key: 'one_year', label: '最近一年', start_date: '2025-09-08', end_date: '2026-09-08', bars: 4, total_return: 0.3, annual_return: 0.3, max_drawdown: 0.1, sharpe: 1.3, total_trades: 4, win_rate: 0.5, diagnostic: null },
          two_year: { key: 'two_year', label: '最近两年', start_date: '2024-09-08', end_date: '2026-09-08', bars: 6, total_return: 0.44, annual_return: 0.2, max_drawdown: 0.12, sharpe: 1.1, total_trades: 6, win_rate: 0.5, diagnostic: null },
        },
        annual_metrics: [
          { key: 'year_2025', label: '2025', start_date: '2025-01-01', end_date: '2025-12-31', bars: 3, total_return: 0.2, annual_return: 0.2, max_drawdown: 0.08, sharpe: 1.0, total_trades: 3, win_rate: 0.5, diagnostic: null },
          { key: 'year_2026', label: '2026', start_date: '2026-01-01', end_date: '2026-09-08', bars: 3, total_return: 0.18, annual_return: 0.2, max_drawdown: 0.06, sharpe: 1.2, total_trades: 2, win_rate: 0.5, diagnostic: null },
        ],
        backtest: {
          universe: 'all' as const, total_candidates: 1, processed: 1, skipped: 0, errors: 0, bars: 6,
          start_date: '2024-01-01', end_date: '2026-09-08', max_positions: 3,
          ranking_value: 'technical.ma5', rank_order: 'desc' as const, fitness_rank_weight: 0,
          fitness_min_trades: 5, fitness_max_drawdown: 0.3,
          performance: { total_return: 0.55, annual_return: 0.22, max_drawdown: 0.12, sharpe: 1.1, total_trades: 8, win_rate: 0.5, end_value: 155000 },
          equity_curve: [{ date: '2024-01-01', cash: 100000, position_value: 0, total: 100000, positions_count: 0, drawdown: 0, drawdown_pct: 0 }, { date: '2026-09-08', cash: 0, position_value: 155000, total: 155000, positions_count: 3, drawdown: 0, drawdown_pct: 0 }],
          trades: [], states: [], ranking_events: [], failure_reasons: {}, diagnostic: null,
          data_fingerprint: 'replay-fingerprint', risk_controls: {},
        },
        data_fingerprint: 'replay-fingerprint', diagnostic: null,
      }],
    }
    vi.mocked(api.createStrategyReplay).mockResolvedValue({ job_id: 'replay-1', status: 'queued' })
    vi.mocked(api.getStrategyReplay).mockResolvedValue({
      job_id: 'replay-1', status: 'completed', progress: 1, total_candidates: 1, total_scanned: 1,
      errors: 0, skipped: 0, error: null, result: null, phase: 'completed', metadata: {},
    })
    vi.mocked(api.getStrategyReplayResults).mockResolvedValue(replayResult)

    const wrapper = mount(StrategyDiscoveryPage)
    await flushPromises()
    await wrapper.get('[data-testid="strategy-discovery-config"]').trigger('submit')
    await flushPromises()
    await wrapper.get('[data-testid="strategy-discovery-replay"]').trigger('click')
    await flushPromises()

    expect(api.createStrategyReplay).toHaveBeenCalledWith('discovery-1', 'sharpe')
    expect(wrapper.get('[data-testid="strategy-replay-results"]').text()).toContain('55.00%')
    expect(wrapper.get('[data-testid="strategy-replay-results"]').text()).toContain('行情已更新')
    await wrapper.get('[data-testid="strategy-replay-period-ytd"]').trigger('click')
    expect(wrapper.get('[data-testid="strategy-replay-results"]').text()).toContain('18.00%')
    await wrapper.get('[data-testid="strategy-replay-period-annual"]').trigger('click')
    expect(wrapper.get('[data-testid="strategy-replay-results"]').text()).toContain('2026')
    await wrapper.get('[data-testid="strategy-replay-candidate-s1"]').trigger('click')
    expect(wrapper.get('[data-testid="strategy-replay-detail-s1"]').text()).toContain('technical.ma5')
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
        errors: 0, skipped: 0, error: null, phase: 'completed', result: null,
        metadata: { requested_trials: 1000, evaluated_trials: 1000, unique_evaluated_trials: 1000, duplicate_trials: 12 },
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
    expect(wrapper.get('[data-testid="strategy-discovery-budget-stats"]').text()).toContain('请求候选 1000')
    expect(wrapper.get('[data-testid="strategy-discovery-budget-stats"]').text()).toContain('重复 12')
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

  it('surfaces a capabilities loading error', async () => {
    vi.mocked(api.fetchStrategyDiscoveryCapabilities).mockRejectedValueOnce(new Error('capabilities down'))

    const wrapper = mount(StrategyDiscoveryPage)
    await flushPromises()

    expect(wrapper.get('[data-testid="strategy-discovery-message"]').text()).toContain('capabilities down')
  })

  it('surfaces a history deletion error', async () => {
    vi.mocked(api.fetchStrategyDiscoveryHistory).mockResolvedValueOnce([{
      history_id: 'history-delete',
      created_at: '2026-09-10T00:00:00',
      total_trials: 1,
      best_sharpe: null,
      best_return: null,
    }])
    vi.mocked(api.deleteStrategyDiscoveryHistory).mockRejectedValueOnce(new Error('delete failed'))

    const wrapper = mount(StrategyDiscoveryPage)
    await flushPromises()
    await wrapper.get('[data-testid="strategy-discovery-history"] .danger-link').trigger('click')
    await flushPromises()

    expect(wrapper.get('[data-testid="strategy-discovery-message"]').text()).toContain('delete failed')
  })

  it('cancels a running discovery job', async () => {
    vi.mocked(api.getStrategyDiscovery).mockImplementationOnce(
      () => new Promise(() => undefined),
    )
    vi.mocked(api.cancelStrategyDiscovery).mockResolvedValue({
      job_id: 'discovery-1', status: 'cancelling', cancel_requested: true,
    })

    const wrapper = mount(StrategyDiscoveryPage)
    await flushPromises()
    await wrapper.get('[data-testid="strategy-discovery-config"]').trigger('submit')
    await flushPromises()
    await wrapper.get('[data-testid="strategy-discovery-cancel"]').trigger('click')
    await flushPromises()

    expect(api.cancelStrategyDiscovery).toHaveBeenCalledWith('discovery-1')
    expect(wrapper.get('[data-testid="strategy-discovery-message"]').text()).toContain('已请求取消策略实验')
  })

  it('reports a replay failure and cancels a running replay', async () => {
    vi.mocked(api.createStrategyReplay).mockRejectedValueOnce(new Error('候选为空'))
    const wrapper = mount(StrategyDiscoveryPage)
    await flushPromises()
    await wrapper.get('[data-testid="strategy-discovery-config"]').trigger('submit')
    await flushPromises()

    await wrapper.get('[data-testid="strategy-discovery-replay"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-testid="strategy-discovery-message"]').text()).toContain('策略复核失败：候选为空')

    vi.mocked(api.getStrategyReplay).mockImplementationOnce(
      () => new Promise(() => undefined),
    )
    vi.mocked(api.cancelStrategyReplay).mockResolvedValue({
      job_id: 'replay-1', status: 'cancelling', cancel_requested: true,
    })
    await wrapper.get('[data-testid="strategy-discovery-replay"]').trigger('click')
    await flushPromises()
    await wrapper.get('[data-testid="strategy-discovery-replay-cancel"]').trigger('click')
    await flushPromises()

    expect(api.cancelStrategyReplay).toHaveBeenCalledWith('replay-1')
    expect(wrapper.get('[data-testid="strategy-discovery-message"]').text()).toContain('已请求取消全量复核')
  })

  it('resumes an in-flight replay after a page refresh', async () => {
    vi.mocked(jobsApi.fetchActiveJobs).mockResolvedValueOnce([{
      job_id: 'replay-refresh', description: '策略前十全量复核', status: 'running', progress: 0.3,
      total_candidates: 1, total_scanned: 0, errors: 0, error: null,
      metadata: { experiment_id: 'discovery-1' },
    }])
    vi.mocked(api.getStrategyDiscoveryHistory).mockResolvedValueOnce({
      history_id: 'discovery-1',
      request: {}, results_by_sharpe: [], results_by_return: [], search_metadata: {}, validation_metadata: {},
    })
    vi.mocked(api.getStrategyReplay).mockResolvedValueOnce({
      job_id: 'replay-refresh', status: 'completed', progress: 1, total_candidates: 1, total_scanned: 1,
      errors: 0, skipped: 0, error: null, result: null, phase: 'completed', metadata: {},
    })
    vi.mocked(api.getStrategyReplayResults).mockResolvedValueOnce({
      experiment_id: 'discovery-1', leaderboard: 'sharpe', data_start: '2024-01-01', data_end: '2026-09-08',
      data_fingerprint: 'replay-fingerprint', source_data_fingerprint: 'source-fingerprint', data_changed: false,
      candidate_count: 1, diagnostics: {}, candidates: [],
    })

    const wrapper = mount(StrategyDiscoveryPage)
    await flushPromises()

    expect(api.getStrategyDiscoveryHistory).toHaveBeenCalledWith('discovery-1')
    expect(api.getStrategyReplay).toHaveBeenCalledWith('replay-refresh')
  })

  it('selects a leaderboard candidate, walks the replay periods and applies one', async () => {
    vi.mocked(api.getStrategyDiscovery).mockResolvedValue({
      job_id: 'discovery-1', status: 'completed', progress: 1, total_candidates: 4, total_scanned: 4,
      errors: 0, skipped: 0, error: null, phase: 'completed', result: null,
      metadata: {
        stage_durations_seconds: { snapshot: 12, coarse: 640, medium: 3600, full: 7200, validation: 65, holdout: 30 },
      },
    })
    const trade = {
      date: '2024-03-01', signal_date: '2024-02-29', market: 'SH', code: '600000', name: '浦发银行',
      direction: 'SELL', size: 100, price: 11, commission: 0.5, stamp_tax: 1, slippage: 0, pnl: 120,
      cost_basis: 1000, reason: '止盈', risk_trigger: 'take_profit', rejected: false,
    }
    vi.mocked(api.createStrategyReplay).mockResolvedValue({ job_id: 'replay-1', status: 'queued' })
    vi.mocked(api.getStrategyReplay).mockResolvedValue({
      job_id: 'replay-1', status: 'completed', progress: 1, total_candidates: 1, total_scanned: 1,
      errors: 0, skipped: 0, error: null, result: null, phase: 'completed', metadata: {},
    })
    vi.mocked(api.getStrategyReplayResults).mockResolvedValue({
      experiment_id: 'discovery-1', leaderboard: 'sharpe', data_start: '2024-01-01', data_end: '2026-09-08',
      data_fingerprint: 'replay-fingerprint', source_data_fingerprint: 'source-fingerprint', data_changed: false,
      candidate_count: 1, diagnostics: {},
      candidates: [{
        rank: 1, candidate_hash: 's1', human_readable_rules: '买入[MA5 > MA20]',
        config: { ranking_value: 'technical.ma5' },
        leaderboard_metrics: { annual_return: 0.2, sharpe: 1.4 },
        overall_metrics: { total_return: 0.5, annual_return: 0.2, max_drawdown: 0.1, sharpe: 1.1, total_trades: 6, win_rate: 0.5 },
        periods: {
          ytd: { key: 'ytd', label: '今年以来', start_date: '2026-01-01', end_date: '2026-09-08', bars: 3, total_return: 0.1, annual_return: 0.1, max_drawdown: 0.05, sharpe: 1.0, total_trades: 2, win_rate: 0.5, diagnostic: null },
          one_year: { key: 'one_year', label: '最近一年', start_date: '2025-09-08', end_date: '2026-09-08', bars: 4, total_return: 0.3, annual_return: 0.3, max_drawdown: 0.1, sharpe: 1.3, total_trades: 4, win_rate: 0.5, diagnostic: null },
          two_year: { key: 'two_year', label: '最近两年', start_date: '2024-09-08', end_date: '2026-09-08', bars: 6, total_return: 0.44, annual_return: 0.2, max_drawdown: 0.12, sharpe: 1.1, total_trades: 6, win_rate: 0.5, diagnostic: null },
        },
        annual_metrics: [{ key: 'year_2025', label: '2025', start_date: '2025-01-01', end_date: '2025-12-31', bars: 3, total_return: 0.2, annual_return: 0.2, max_drawdown: 0.08, sharpe: 1.0, total_trades: 3, win_rate: 0.5, diagnostic: null }],
        backtest: {
          universe: 'all', total_candidates: 1, processed: 1, skipped: 0, errors: 0, bars: 6,
          start_date: '2024-01-01', end_date: '2026-09-08', max_positions: 3,
          ranking_value: 'technical.ma5', rank_order: 'desc', fitness_rank_weight: 0,
          fitness_min_trades: 5, fitness_max_drawdown: 0.3,
          performance: { total_return: 0.5, annual_return: 0.2, max_drawdown: 0.1, sharpe: 1.1, total_trades: 6, win_rate: 0.5, end_value: 150000 },
          equity_curve: [
            { date: '2024-01-01', cash: 100000, position_value: 0, total: 100000, positions_count: 0, drawdown: 0, drawdown_pct: 0 },
            { date: '2026-09-08', cash: 0, position_value: 150000, total: 150000, positions_count: 3, drawdown: 0, drawdown_pct: 0 },
          ],
          trades: [trade as never], states: [], ranking_events: [], failure_reasons: {}, diagnostic: null,
          data_fingerprint: 'replay-fingerprint', risk_controls: {},
        },
        data_fingerprint: 'replay-fingerprint', diagnostic: null,
      }],
    })

    const wrapper = mount(StrategyDiscoveryPage)
    await flushPromises()
    await wrapper.get('[data-testid="strategy-discovery-config"]').trigger('submit')
    await flushPromises()

    // 阶段耗时只在元数据里带 stage_durations_seconds 时才渲染。
    expect(wrapper.get('[data-testid="strategy-discovery-stage-durations"]').text()).toContain('粗筛')

    await wrapper.get('[data-testid="strategy-discovery-row-s1"] button').trigger('click')
    expect(wrapper.get('[data-testid="strategy-discovery-detail"]').text()).toContain('MA5 > MA20')

    await wrapper.get('[data-testid="strategy-discovery-replay"]').trigger('click')
    await flushPromises()

    await wrapper.get('[data-testid="strategy-replay-period-one-year"]').trigger('click')
    expect(wrapper.get('[data-testid="strategy-replay-period-table"]').text()).toContain('30.00%')
    await wrapper.get('[data-testid="strategy-replay-period-two-year"]').trigger('click')
    expect(wrapper.get('[data-testid="strategy-replay-period-table"]').text()).toContain('44.00%')
    await wrapper.get('[data-testid="strategy-replay-period-overall"]').trigger('click')
    await wrapper.get('[data-testid="strategy-replay-candidate-s1"]').trigger('click')
    expect(wrapper.get('[data-testid="strategy-replay-detail-s1"]').text()).toContain('止盈')

    await wrapper.get('[data-testid="strategy-replay-apply-s1"]').trigger('click')
    await flushPromises()
    expect(JSON.parse(sessionStorage.getItem('aquest.strategy-discovery.apply') ?? '{}')).toEqual({
      ranking_value: 'technical.ma5',
    })
    expect(sessionStorage.getItem('aquest.strategy-discovery.apply-meta')).toContain('s1')
  })
})
