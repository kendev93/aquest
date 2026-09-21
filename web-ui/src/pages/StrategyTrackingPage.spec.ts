import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { StrategyTrackingSnapshot } from '../types'
import StrategyTrackingPage from './StrategyTrackingPage.vue'

import * as strategiesApi from '../api/savedStrategies'

vi.mock('../api/savedStrategies', async () => {
  const actual =
    await vi.importActual<typeof import('../api/savedStrategies')>('../api/savedStrategies')
  return {
    ...actual,
    fetchStrategyTracking: vi.fn(),
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

describe('StrategyTrackingPage', () => {
  beforeEach(() => {
    vi.mocked(strategiesApi.fetchStrategyTracking).mockReset()
    vi.mocked(strategiesApi.getTrackingJob).mockReset()
    vi.mocked(strategiesApi.launchStrategyTracking).mockReset()
    vi.mocked(strategiesApi.stopStrategyTracking).mockReset()
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
})
