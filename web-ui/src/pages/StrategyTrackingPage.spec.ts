import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { FormulaScreenMetadata, StrategyTrackingSnapshot, TrackingTradePage } from '../types'
import StrategyTrackingPage from './StrategyTrackingPage.vue'

import * as strategiesApi from '../api/savedStrategies'
import * as formulaApi from '../api/formulaScreen'

vi.mock('../api/formulaScreen', async () => {
  const actual = await vi.importActual<typeof import('../api/formulaScreen')>('../api/formulaScreen')
  return { ...actual, fetchMetadata: vi.fn() }
})

vi.mock('../api/savedStrategies', async () => {
  const actual =
    await vi.importActual<typeof import('../api/savedStrategies')>('../api/savedStrategies')
  return {
    ...actual,
    fetchStrategyTracking: vi.fn(),
    getStrategyTracking: vi.fn(),
    getStrategyTrackingTrades: vi.fn(),
    getTrackingJob: vi.fn(),
    launchStrategyTracking: vi.fn(),
    rerunStrategyTracking: vi.fn(),
    stopStrategyTracking: vi.fn(),
  }
})

function point(tradeDate: number, equity: number, benchmark: number | null = null) {
  return {
    trade_date: tradeDate,
    equity,
    cash: 1000,
    positions_count: 2,
    benchmark_value: benchmark,
    drawdown: 0,
  }
}

const online: StrategyTrackingSnapshot = {
  strategy_id: 'strategy-1',
  name: '主力进场轮动',
  tracking_status: 'active',
  launched_at: 20260901,
  last_tracked_date: 20260910,
  last_error: '',
  observations: [
    point(20260901, 100000, 100000),
    point(20260902, 102000, 101000),
    point(20260903, 104000, 103000),
  ],
  research: {
    start_date: '2024-01-02',
    end_date: '2026-09-01',
    bars: 640,
    performance: { annual_return: 0.24, sharpe: 1.5, total_return: 1.8, max_drawdown: -0.2 },
  },
  observation: {
    start_date: '2026-09-01',
    end_date: '2026-09-03',
    bars: 3,
    performance: { annual_return: 0.12, sharpe: 0.8, total_return: 0.04, max_drawdown: -0.02 },
  },
  decay: { annual_return: 0.5, sharpe: 0.53 },
  benchmark_performance: { annual_return: 0.09 },
  excess_annual_return: 0.03,
  observation_is_short: true,
  min_track_record_length: null,
  trades: null,
  next_plan: null,
  current_state: null,
  today_candidates: null,
}

const offline: StrategyTrackingSnapshot = {
  ...online,
  strategy_id: 'strategy-2',
  name: '仅保存没上线',
  tracking_status: 'off',
  launched_at: null,
  last_tracked_date: null,
  observations: [],
  research: null,
  observation: null,
  decay: {},
  benchmark_performance: null,
  excess_annual_return: null,
  observation_is_short: true,
}

const indicatorMetadata: FormulaScreenMetadata = {
  indicators: [
    {
      id: 'technical',
      display_name: '技术指标',
      minimum_bars: 1,
      recommended_bars: 1,
      signals: [],
      values: [{ id: 'technical.rsi14', display_name: 'RSI 14', description: '相对强弱指标' }],
    },
    {
      id: 'indicator_one',
      display_name: '主力清洗',
      minimum_bars: 1,
      recommended_bars: 1,
      signals: [
        { id: 'indicator_one.distribution', display_name: '出货', description: 'VAR51 上升' },
        { id: 'indicator_one.main_force_entry', display_name: '主力进场', description: 'VAR5 上升' },
      ],
      values: [],
    },
  ],
  combine_modes: [],
  supported_markets: [],
  supported_universe: [],
  periods: [],
  data_directory_help: '',
}

describe('StrategyTrackingPage', () => {
  beforeEach(() => {
    vi.mocked(formulaApi.fetchMetadata).mockReset().mockResolvedValue(indicatorMetadata)
    vi.mocked(strategiesApi.fetchStrategyTracking).mockReset()
    vi.mocked(strategiesApi.getStrategyTracking).mockReset()
    vi.mocked(strategiesApi.getStrategyTrackingTrades).mockReset()
    vi.mocked(strategiesApi.getStrategyTrackingTrades).mockResolvedValue({
      rows: [], total: 0, limit: 50, offset: 0,
    })
    vi.mocked(strategiesApi.getTrackingJob).mockReset()
    vi.mocked(strategiesApi.launchStrategyTracking).mockReset()
    vi.mocked(strategiesApi.stopStrategyTracking).mockReset()
    vi.mocked(strategiesApi.rerunStrategyTracking).mockReset()
  })

  it('renders one row per strategy with its decay ratio', async () => {
    vi.mocked(strategiesApi.fetchStrategyTracking).mockResolvedValue([online, offline])

    const wrapper = mount(StrategyTrackingPage)
    await flushPromises()

    expect(wrapper.get('[data-testid="tracking-row-strategy-1"]').text()).toContain('0.50×')
    expect(wrapper.get('[data-testid="tracking-status-strategy-1"]').text()).toBe('已上线')
    expect(wrapper.get('[data-testid="tracking-status-strategy-2"]').text()).toBe('未上线')
    expect(wrapper.get('[data-testid="tracking-row-strategy-1"]').text()).toContain('2026-09-01')
  })

  it('warns when the observation window is too short to mean anything', async () => {
    vi.mocked(strategiesApi.fetchStrategyTracking).mockResolvedValue([online])

    const wrapper = mount(StrategyTrackingPage)
    await flushPromises()
    await wrapper.get('[data-testid="tracking-toggle-strategy-1"]').trigger('click')
    await flushPromises()

    // 刚上线三天就谈"衰减"是噪声，这一条必须拦住。
    expect(wrapper.get('[data-testid="tracking-short-warning"]').text()).toContain('基本是噪声')
  })

  it('says how many more trading days are needed before the result is significant', async () => {
    // 观察段已经不算短，但离"能谈显著"还差得远——这正是 MinTRL 要传达的。
    const longRun: StrategyTrackingSnapshot = {
      ...online,
      observation_is_short: false,
      min_track_record_length: 900,
    }
    vi.mocked(strategiesApi.fetchStrategyTracking).mockResolvedValue([longRun])

    const wrapper = mount(StrategyTrackingPage)
    await flushPromises()
    await wrapper.get('[data-testid="tracking-toggle-strategy-1"]').trigger('click')
    await flushPromises()

    const note = wrapper.get('[data-testid="tracking-mintrl-note"]').text()
    expect(note).toContain('900 个交易日')
    // 上线日那一行是接力点不算观察日，3 行 = 已观察 2 个，还差 898。
    expect(note).toContain('已观察 2 个')
    expect(note).toContain('还差 898 个')
    expect(note).toContain('3.6 年')
  })

  it('stays quiet when the minimum length cannot be computed', async () => {
    // 观察段够长但算不出（平均收益不为正、零波动等）：null 不是"不需要样本"，
    // 显示一条空提示或"还差 0 个"都会误导。
    const noAnswer: StrategyTrackingSnapshot = {
      ...online,
      observation_is_short: false,
      min_track_record_length: null,
    }
    vi.mocked(strategiesApi.fetchStrategyTracking).mockResolvedValue([noAnswer])

    const wrapper = mount(StrategyTrackingPage)
    await flushPromises()
    await wrapper.get('[data-testid="tracking-toggle-strategy-1"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-testid="tracking-mintrl-note"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="tracking-short-warning"]').exists()).toBe(false)
  })

  it('stays quiet once the observation window is already long enough', async () => {
    const enough: StrategyTrackingSnapshot = {
      ...online,
      observation_is_short: false,
      min_track_record_length: 2,
    }
    vi.mocked(strategiesApi.fetchStrategyTracking).mockResolvedValue([enough])

    const wrapper = mount(StrategyTrackingPage)
    await flushPromises()
    await wrapper.get('[data-testid="tracking-toggle-strategy-1"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-testid="tracking-mintrl-note"]').exists()).toBe(false)
  })

  it('launches a strategy by submitting a job and then reloading', async () => {
    vi.mocked(strategiesApi.fetchStrategyTracking).mockResolvedValue([offline])
    vi.mocked(strategiesApi.launchStrategyTracking).mockResolvedValue({
      job_id: 'job-1',
      status: 'queued',
    })
    vi.mocked(strategiesApi.getTrackingJob).mockResolvedValue({
      job_id: 'job-1',
      status: 'completed',
      progress: 1,
      total_candidates: 1,
      total_scanned: 1,
      errors: 0,
      error: null,
      result: online,
    } as never)

    const wrapper = mount(StrategyTrackingPage)
    await flushPromises()
    await wrapper.get('[data-testid="tracking-launch-strategy-2"]').trigger('click')
    await flushPromises()

    expect(strategiesApi.launchStrategyTracking).toHaveBeenCalledWith('strategy-2')
    expect(wrapper.get('[data-testid="tracking-message"]').text()).toContain('追踪已更新')
  })

  it('surfaces the last error without pretending the day was computed', async () => {
    const failed: StrategyTrackingSnapshot = { ...online, last_error: '行情缺失' }
    vi.mocked(strategiesApi.fetchStrategyTracking).mockResolvedValue([failed])

    const wrapper = mount(StrategyTrackingPage)
    await flushPromises()

    expect(wrapper.get('[data-testid="tracking-error-strategy-1"]').text()).toBe('上次失败')
    await wrapper.get('[data-testid="tracking-toggle-strategy-1"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-testid="tracking-error-detail-strategy-1"]').text()).toBe('行情缺失')
    // 日期停在旧值，而不是被改成"今天"。
    expect(wrapper.get('[data-testid="tracking-updated-strategy-1"]').text()).toContain('2026-09-10')
  })

  it('draws both the strategy curve and the benchmark on one axis', async () => {
    vi.mocked(strategiesApi.fetchStrategyTracking).mockResolvedValue([online])

    const wrapper = mount(StrategyTrackingPage)
    await flushPromises()
    await wrapper.get('[data-testid="tracking-toggle-strategy-1"]').trigger('click')
    await flushPromises()

    const chart = wrapper.get('[data-testid="tracking-chart-strategy-1"]')
    const lines = chart.findAll('polyline')
    // 两条线共用同一个纵轴范围，否则基准会跑出画框、看着像策略完胜。
    expect(lines.length).toBe(2)
    // 基准是虚线，画在策略实线下面。
    expect(lines[0].attributes('stroke-dasharray')).toBe('6 4')
  })

  it('stops tracking and keeps what was already observed', async () => {
    vi.mocked(strategiesApi.fetchStrategyTracking).mockResolvedValue([online])
    vi.mocked(strategiesApi.stopStrategyTracking).mockResolvedValue({
      ...online,
      tracking_status: 'off',
    })

    const wrapper = mount(StrategyTrackingPage)
    await flushPromises()
    await wrapper.get('[data-testid="tracking-stop-strategy-1"]').trigger('click')
    await flushPromises()

    expect(strategiesApi.stopStrategyTracking).toHaveBeenCalledWith('strategy-1')
    expect(wrapper.get('[data-testid="tracking-message"]').text()).toContain('保留')
  })

  it('explains an empty library instead of showing a blank table', async () => {
    vi.mocked(strategiesApi.fetchStrategyTracking).mockResolvedValue([])

    const wrapper = mount(StrategyTrackingPage)
    await flushPromises()

    expect(wrapper.get('[data-testid="tracking-empty"]').text()).toContain('组合回测')
  })

  describe('下一交易日计划与成交明细', () => {
    const plan = {
      as_of: '2026-09-10',
      cash: 5000,
      estimated_cash_after_sells: 20000,
      rebalance_due: true,
      holdings: [
        {
          market: 'SH',
          code: '600001',
          name: '甲',
          size: 1000,
          reference_price: 15,
          market_value: 15000,
          entry_price: 14,
          entry_date: '2026-09-02',
          unrealized_pnl_pct: 0.0714,
        },
      ],
      sells: [
        {
          market: 'SH',
          code: '600001',
          name: '甲',
          size: 1000,
          reference_price: 15,
          estimated_proceeds: 14950,
          reason: '卖出条件',
          signal_date: '2026-09-10',
        },
      ],
      buys: [
        {
          market: 'SZ',
          code: '000002',
          name: '乙',
          estimated_size: 1300,
          reference_price: 11,
          estimated_cost: 14320,
          signal_date: '2026-09-10',
        },
      ],
      notes: ['买入股数是按今日收盘价与可用现金估算的，明天开盘价变了就会变。'],
    }

    const trades: TrackingTradePage = {
      rows: [
        {
          trade_date: 20260905,
          signal_date: 20260904,
          phase: 'post',
          direction: 'SELL',
          market: 'SH',
          code: '600001',
          name: '甲',
          size: 300,
          price: 16,
          commission: 1.6,
          stamp_tax: 4.8,
          slippage: 0.5,
          cost_basis: 4200,
          pnl: 600,
          reason: '卖出条件',
        },
        {
          trade_date: 20260820,
          signal_date: null,
          phase: 'pre',
          direction: 'BUY',
          market: 'SH',
          code: '600009',
          name: '旧',
          size: 500,
          price: 9,
          commission: null,
          stamp_tax: null,
          slippage: null,
          cost_basis: null,
          pnl: 0,
          reason: '买入条件',
        },
      ],
      total: 2,
      limit: 50,
      offset: 0,
    }

    it('shows a dated holdings snapshot and a separate informational candidate scan', async () => {
      const detail: StrategyTrackingSnapshot = {
        ...online,
        current_state: {
          as_of: '2026-09-10',
          available: true,
          equity: 125000,
          total_asset: 125000,
          cash: 25000,
          position_value: 100000,
          positions_count: 1,
          holdings: [
            {
              market: 'SH', code: '600100', name: '持仓甲', size: 1000,
              entry_price: 90, entry_date: '2026-09-02', close: 100, latest_price: 100,
              market_value: 100000, unrealized_pnl: 10000, unrealized_pnl_pct: 0.1111,
              position_weight: 0.8,
              sell_signal: {
                matched: false,
                signals: [{ id: 'indicator_one.distribution', matched: false }],
                conditions: [{
                  left_value: 'technical.rsi14', right_value: null, operator: 'cross_down',
                  current_left_value: 35, current_right_value: 30, threshold: 30,
                  previous_left_value: 40, previous_right_value: 30,
                  required: false, matched: false,
                }],
              },
            },
          ],
        },
        today_candidates: {
          as_of: '2026-09-10', ranking_value: 'indicator_one.wash', total: 21,
          rows: [{
            rank: 1, market: 'SZ', code: '000101', name: '候选乙', score: 88, rank_score: 88,
            ranking_value: 'indicator_one.wash', already_held: false,
            buy_signal: { matched: true, signals: [{ id: 'indicator_one.main_force_entry', matched: true }] },
            buy_conditions: [{
              left_value: 'technical.rsi14', right_value: null, operator: 'cross_up',
              current_left_value: 31, current_right_value: 30, threshold: 30,
              previous_left_value: 28, previous_right_value: 30,
              required: false, matched: true,
            }],
          }],
        },
        next_plan: plan,
      }
      vi.mocked(strategiesApi.fetchStrategyTracking).mockResolvedValue([{ ...online, next_plan: plan }])
      vi.mocked(strategiesApi.getStrategyTracking).mockResolvedValue(detail)
      vi.mocked(strategiesApi.getStrategyTrackingTrades).mockResolvedValue({ rows: [], total: 0, limit: 50, offset: 0 })

      const wrapper = mount(StrategyTrackingPage)
      await flushPromises()
      await wrapper.get('[data-testid="tracking-toggle-strategy-1"]').trigger('click')
      await flushPromises()

      const state = wrapper.get('[data-testid="tracking-current-state-strategy-1"]')
      const candidates = wrapper.get('[data-testid="tracking-candidates-strategy-1"]')
      expect(state.text()).toContain('截至 2026-09-10')
      expect(state.text()).toContain('125,000')
      expect(state.text()).toContain('600100')
      expect(state.text()).toContain('前值 → 当前值')
      expect(state.text()).toContain('40.00 → 35.00')
      expect(state.text()).toContain('RSI 14 下穿')
      expect(state.text()).toContain('出货：未满足')
      expect(candidates.text()).toContain('命中 21 只')
      expect(candidates.text()).toContain('000101')
      expect(candidates.text()).toContain('主力进场：满足')
      expect(candidates.text()).toContain('28.00 → 31.00')
      expect(wrapper.get('[data-testid="tracking-plan-strategy-1"]').text()).toContain('下一交易日待执行计划')
    })

    it('shows a historical-version note when an old stopped strategy has no saved state', async () => {
      const oldStopped: StrategyTrackingSnapshot = {
        ...offline,
        launched_at: 20260901,
        last_tracked_date: 20260910,
      }
      vi.mocked(strategiesApi.fetchStrategyTracking).mockResolvedValue([oldStopped])
      vi.mocked(strategiesApi.getStrategyTracking).mockResolvedValue(oldStopped)

      const wrapper = mount(StrategyTrackingPage)
      await flushPromises()
      await wrapper.get('[data-testid="tracking-toggle-strategy-2"]').trigger('click')
      await flushPromises()

      expect(wrapper.get('[data-testid="tracking-state-not-saved"]').text()).toContain('历史版本未保存')
    })

    it('pages through the full trade ledger', async () => {
      const firstRow = {
        trade_date: 20260910, signal_date: 20260909, phase: 'post' as const,
        direction: 'SELL', market: 'SH', code: '600001', name: '甲', size: 100,
        price: 12, commission: 1, stamp_tax: 2, slippage: 0.5,
        cost_basis: 1000, pnl: 196.5, reason: '卖出指标',
      }
      const firstPage: TrackingTradePage = {
        rows: Array.from({ length: 50 }, (_, index) => ({ ...firstRow, code: `600${String(index).padStart(3, '0')}` })),
        total: 51, limit: 50, offset: 0,
      }
      const lastPage: TrackingTradePage = {
        rows: [{ ...firstRow, code: '600051' }], total: 51, limit: 50, offset: 50,
      }
      vi.mocked(strategiesApi.fetchStrategyTracking).mockResolvedValue([online])
      vi.mocked(strategiesApi.getStrategyTracking).mockResolvedValue(online)
      vi.mocked(strategiesApi.getStrategyTrackingTrades)
        .mockResolvedValueOnce(firstPage)
        .mockResolvedValueOnce(lastPage)

      const wrapper = mount(StrategyTrackingPage)
      await flushPromises()
      await wrapper.get('[data-testid="tracking-toggle-strategy-1"]').trigger('click')
      await flushPromises()
      expect(wrapper.get('[data-testid="tracking-trades-strategy-1"]').text()).toContain('600000')

      await wrapper.get('.trade-pagination button:last-child').trigger('click')
      await flushPromises()

      expect(strategiesApi.getStrategyTrackingTrades).toHaveBeenLastCalledWith('strategy-1', {
        phase: '', date_from: '', date_to: '', limit: 50, offset: 50,
      })
      expect(wrapper.get('[data-testid="tracking-trades-strategy-1"]').text()).toContain('600051')
    })

    it('applies phase and date filters and resets to the first page', async () => {
      vi.mocked(strategiesApi.fetchStrategyTracking).mockResolvedValue([online])
      vi.mocked(strategiesApi.getStrategyTracking).mockResolvedValue(online)
      vi.mocked(strategiesApi.getStrategyTrackingTrades).mockResolvedValue({
        rows: [], total: 0, limit: 50, offset: 0,
      })

      const wrapper = mount(StrategyTrackingPage)
      await flushPromises()
      await wrapper.get('[data-testid="tracking-toggle-strategy-1"]').trigger('click')
      await flushPromises()
      await wrapper.get('[data-testid="tracking-trades-strategy-1"] select').setValue('pre')
      await flushPromises()

      expect(strategiesApi.getStrategyTrackingTrades).toHaveBeenLastCalledWith('strategy-1', {
        phase: 'pre', date_from: '', date_to: '', limit: 50, offset: 0,
      })
      const dateInputs = wrapper.findAll('[data-testid="tracking-trades-strategy-1"] input[type="date"]')
      await dateInputs[0].setValue('2026-09-01')
      await dateInputs[1].setValue('2026-09-10')
      await flushPromises()

      expect(strategiesApi.getStrategyTrackingTrades).toHaveBeenLastCalledWith('strategy-1', {
        phase: 'pre', date_from: '2026-09-01', date_to: '2026-09-10', limit: 50, offset: 0,
      })
    })

    it('fetches trades only after a strategy is expanded', async () => {
      vi.mocked(strategiesApi.fetchStrategyTracking).mockResolvedValue([online])
      vi.mocked(strategiesApi.getStrategyTracking).mockResolvedValue(online)
      vi.mocked(strategiesApi.getStrategyTrackingTrades).mockResolvedValue(trades)

      const wrapper = mount(StrategyTrackingPage)
      await flushPromises()
      // 列表接口不带明细（几百笔 × 策略数会撑大响应），所以展开前不该请求。
      expect(strategiesApi.getStrategyTracking).not.toHaveBeenCalled()

      await wrapper.get('[data-testid="tracking-toggle-strategy-1"]').trigger('click')
      await flushPromises()

      expect(strategiesApi.getStrategyTracking).toHaveBeenCalledWith('strategy-1')
      expect(strategiesApi.getStrategyTrackingTrades).toHaveBeenCalledWith('strategy-1', {
        phase: '', date_from: '', date_to: '', limit: 50, offset: 0,
      })
      const table = wrapper.get('[data-testid="tracking-trades-strategy-1"]')
      expect(table.text()).toContain('600001')
      expect(table.text()).toContain('600009')
      expect(table.text()).toContain('已实现盈亏')
      expect(table.text()).toContain('手续费')
      expect(table.text()).toContain('1.60')
      expect(table.text()).toContain('未记录')
    })

    it('tells the user what the next session is going to trade', async () => {
      vi.mocked(strategiesApi.fetchStrategyTracking).mockResolvedValue([
        { ...online, next_plan: plan },
      ])

      const wrapper = mount(StrategyTrackingPage)
      await flushPromises()

      expect(wrapper.get('[data-testid="tracking-plan-badge-strategy-1"]').text()).toBe(
        '明日：卖 1 / 买 1',
      )
      await wrapper.get('[data-testid="tracking-toggle-strategy-1"]').trigger('click')
      await flushPromises()

      const card = wrapper.get('[data-testid="tracking-plan-strategy-1"]')
      expect(card.text()).toContain('2026-09-10')
      expect(wrapper.get('[data-testid="tracking-plan-sells-strategy-1"]').text()).toContain('600001')
      expect(wrapper.get('[data-testid="tracking-plan-buys-strategy-1"]').text()).toContain('000002')
      // 估算必须写出来：明天的开盘价今天不存在。
      expect(card.text()).toContain('估算')
    })

    it('names the risk line behind a pending exit and the day it was judged', async () => {
      vi.mocked(strategiesApi.fetchStrategyTracking).mockResolvedValue([
        {
          ...online,
          next_plan: {
            ...plan,
            sells: [{
              ...plan.sells[0],
              reason: '止损',
              risk_trigger: 'stop_loss',
              stop_loss_price: 9,
              trailing_stop_price: null,
              take_profit_price: null,
              risk_level_as_of: '2026-09-10',
            }],
          },
        },
      ])

      const wrapper = mount(StrategyTrackingPage)
      await flushPromises()
      await wrapper.get('[data-testid="tracking-toggle-strategy-1"]').trigger('click')
      await flushPromises()

      const sells = wrapper.get('[data-testid="tracking-plan-sells-strategy-1"]')
      expect(sells.text()).toContain('止损 · 线 9.00 · 2026-09-10 收盘判定')
      // 未启用的线不给 0，也不该被读成「跌到 0 才止损」。
      expect(sells.text()).not.toContain('0.00')
    })

    it('says plainly when there is nothing to do tomorrow', async () => {
      vi.mocked(strategiesApi.fetchStrategyTracking).mockResolvedValue([
        {
          ...online,
          next_plan: { ...plan, sells: [], buys: [], notes: ['明日无需操作：持仓不动。'] },
        },
      ])

      const wrapper = mount(StrategyTrackingPage)
      await flushPromises()

      expect(wrapper.get('[data-testid="tracking-plan-badge-strategy-1"]').text()).toBe(
        '明日无需操作',
      )
    })

    it('re-fetches the trades of an expanded row after a rerun finishes', async () => {
      // 重算完成后 load() 会把明细缓存整个作废；若只清空而不把当前展开的那一行补回来，
      // 展开着的行会突然显示「上线后还没有成交」，像是重跑把成交记录弄丢了。
      const refreshedTrades: TrackingTradePage = {
        rows: [
          {
            trade_date: 20260908,
            signal_date: 20260907,
            phase: 'post',
            direction: 'BUY',
            market: 'SH',
            code: '600008',
            name: '新',
            size: 200,
            price: 12,
            commission: 0.24,
            stamp_tax: 0,
            slippage: 0,
            cost_basis: 0,
            pnl: 0,
            reason: '买入条件',
          },
        ],
        total: 1,
        limit: 50,
        offset: 0,
      }
      vi.mocked(strategiesApi.fetchStrategyTracking).mockResolvedValue([online])
      vi.mocked(strategiesApi.getStrategyTracking)
        .mockResolvedValueOnce(online)
        .mockResolvedValueOnce(online)
      vi.mocked(strategiesApi.getStrategyTrackingTrades)
        .mockResolvedValueOnce(trades)
        .mockResolvedValueOnce(refreshedTrades)
      vi.mocked(strategiesApi.rerunStrategyTracking).mockResolvedValue({
        job_id: 'job-2',
        status: 'queued',
      })
      vi.mocked(strategiesApi.getTrackingJob).mockResolvedValue({
        job_id: 'job-2',
        status: 'completed',
        progress: 1,
        total_candidates: 1,
        total_scanned: 1,
        errors: 0,
        error: null,
        result: online,
      } as never)

      const wrapper = mount(StrategyTrackingPage)
      await flushPromises()
      await wrapper.get('[data-testid="tracking-toggle-strategy-1"]').trigger('click')
      await flushPromises()
      expect(wrapper.get('[data-testid="tracking-trades-strategy-1"]').text()).toContain('600001')

      await wrapper.get('[data-testid="tracking-rerun-strategy-1"]').trigger('click')
      await flushPromises()

      expect(strategiesApi.rerunStrategyTracking).toHaveBeenCalledWith('strategy-1')
      // 第一次是展开时拉取，第二次是重算完成后的缓存作废补拉。
      expect(strategiesApi.getStrategyTracking).toHaveBeenCalledTimes(2)
      expect(strategiesApi.getStrategyTrackingTrades).toHaveBeenCalledTimes(2)
      expect(wrapper.get('[data-testid="tracking-trades-strategy-1"]').text()).toContain('600008')
    })
  })
})
