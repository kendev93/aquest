import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import * as api from '../api/strategyPool'
import * as marketDataApi from '../api/marketData'

import type { StrategyPoolCandidate, StrategyPoolEntry, StrategyPoolMetrics, StrategyPoolSignal } from '../types'
import StrategyPoolPage from './StrategyPoolPage.vue'
import { ApiError } from '../api/client'

vi.mock('../api/strategyPool', () => ({
  addStrategyPoolFocus: vi.fn(),
  cancelStrategyPoolJob: vi.fn(),
  clearStrategyPoolOverride: vi.fn(),
  createStrategyPoolJob: vi.fn(),
  fetchResearchSettings: vi.fn(),
  fetchStrategyPoolEntries: vi.fn(),
  fetchStrategyPoolFocus: vi.fn(),
  fetchStrategyPoolJobs: vi.fn(),
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
  return { ...base, active_candidate: candidate, automatic_candidates: { annual_return: candidate, sharpe: candidate } }
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
      automatic_candidates: { annual_return: null, sharpe: null },
      active_candidate: null,
      status: 'insufficient_history',
      last_error: '历史样本不足：无法切出训练、验证、留出三段',
    }
    vi.mocked(api.fetchStrategyPoolEntries).mockResolvedValue([insufficient])

    const wrapper = mount(StrategyPoolPage)
    await flushPromises()

    expect(wrapper.get('[data-testid="pool-row-600000"]').text()).toContain('历史样本不足')
  })

  it('shows at most ten task history rows without manual result loading or pooled holdout UI', async () => {
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    vi.mocked(api.fetchStrategyPoolJobs).mockResolvedValue(Array.from({ length: 12 }, (_, index) => ({
      job_id: `job-${index}`,
      status: 'completed',
      mode: 'single',
      market: 'SH',
      code: `6000${String(index).padStart(2, '0')}`,
      created_at: index,
    })))

    const wrapper = mount(StrategyPoolPage)
    await flushPromises()

    expect(wrapper.findAll('.history-row')).toHaveLength(10)
    expect(wrapper.text()).not.toContain('组合留出段验证')
    expect(wrapper.text()).not.toContain('载入结果')
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

    expect(wrapper.get('[data-testid="pool-current-holdings"]').text()).toContain('今日持仓中 1')
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
    expect(holdings.text()).toContain('今日持仓中 1')
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

  it('loads saved universe preferences and submits them with full research', async () => {
    window.localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    vi.mocked(api.fetchResearchSettings).mockResolvedValue({
      boards: ['main', 'chinext'], exclude_st: true,
    })
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
      expect(focusRow.get('[data-testid="focus-follow-entry-600000"]').text()).toBe('已关注')
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
      expect(wrapper.get('[data-testid="pool-follow-entry-600000"]').text()).toBe('已关注')

      await wrapper.get('[data-testid="focus-follow-entry-600000"]').trigger('click')
      await flushPromises()

      expect(api.removeStrategyPoolFocus).toHaveBeenCalledWith('SH', '600000')
      expect(wrapper.find('[data-testid="focus-row-600000"]').exists()).toBe(false)
      expect(wrapper.find('[data-testid="pool-focus-empty"]').exists()).toBe(true)
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
