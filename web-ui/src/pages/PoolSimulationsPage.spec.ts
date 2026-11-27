import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h, KeepAlive } from 'vue'
import { expect, it, vi } from 'vitest'

import PoolSimulationsPage from './PoolSimulationsPage.vue'

it('creates from the chosen signal-board settings and opens the account detail', async () => {
  const original = window.location.href
  window.history.replaceState({}, '', '/strategy-pool/simulations?create=1&objective=sharpe&ranking=composite&start_date=2026-11-26')
  const api = vi.fn(async (url: string, init?: RequestInit) => {
    const data = url.includes('/signal-days') ? ['2026-11-26']
      : init?.method === 'POST' ? { id: 'test' }
      : url.includes('/test?') ? {
        id: 'test', name: '超额盘', positions: [], trades: [], trades_total: 0, equity: [],
        initial_cash: 1000000, cash: 1000000, value: 1000000, holding_count: 0,
      } : []
    return { ok: true, text: async () => JSON.stringify({ data }) }
  })
  vi.stubGlobal('fetch', api)
  const wrapper = mount(defineComponent({
    setup: () => () => h(KeepAlive, null, { default: () => h(PoolSimulationsPage) }),
  }))
  try {
    await flushPromises()
    await wrapper.get('form input').setValue('超额盘')
    const slots = wrapper.get<HTMLInputElement>('[data-testid="simulation-max-positions"]')
    expect(slots.element.value).toBe('5')
    await slots.setValue(3)
    const band = wrapper.get<HTMLInputElement>('[data-testid="simulation-rebalance-band"]')
    expect(band.element.value).toBe('5')
    await band.setValue(8)
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    const post = api.mock.calls.find(([, init]) => init?.method === 'POST')
    expect(JSON.parse(String(post?.[1]?.body))).toMatchObject({
      name: '超额盘', objective: 'sharpe', ranking: 'composite', start_date: '2026-11-26', max_positions: 3,
      rebalance_band_pct: 8,
    })
    expect(wrapper.text()).toContain('超额盘 · 持仓与成交')
  } finally {
    wrapper.unmount()
    vi.unstubAllGlobals()
    window.history.replaceState({}, '', original)
  }
})

it('deletes an account after confirmation and clears its detail', async () => {
  const original = window.location.href
  window.history.replaceState({}, '', '/strategy-pool/simulations')
  const account = {
    id: 'gone', name: '待删盘', objective: 'annual_return', ranking: 'default', min_days: 0,
    direction: 'desc', start_date: '2026-11-26', initial_cash: 1000000, paused: false,
    cash: 1000000, value: 1000000, total_return: 0, max_drawdown: 0,
    turnover: 0, holding_count: 0, equity: [], positions: [], trades: [], trades_total: 0,
  }
  let deleted = false
  const api = vi.fn(async (url: string, init?: RequestInit) => {
    const data = init?.method === 'DELETE' ? { id: 'gone' }
      : url.includes('/signal-days') ? []
      : url.includes('/gone?') ? account
      : deleted ? []
      : [account]
    if (init?.method === 'DELETE') deleted = true
    return { ok: true, text: async () => JSON.stringify({ data }) }
  })
  vi.stubGlobal('fetch', api)
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
  const wrapper = mount(defineComponent({
    setup: () => () => h(KeepAlive, null, { default: () => h(PoolSimulationsPage) }),
  }))
  try {
    await flushPromises()
    await wrapper.get('.simulation-name').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('待删盘 · 持仓与成交')
    await wrapper.get('.simulation-delete').trigger('click')
    await flushPromises()
    expect(api.mock.calls.some(([url, init]) => init?.method === 'DELETE' && String(url).endsWith('/gone'))).toBe(true)
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining('待删盘'))
    expect(wrapper.text()).toContain('还没有模拟盘')
    expect(wrapper.text()).not.toContain('持仓与成交')
  } finally {
    wrapper.unmount()
    confirm.mockRestore()
    vi.unstubAllGlobals()
    window.history.replaceState({}, '', original)
  }
})

it('renames an account from the list and keeps the open detail in sync', async () => {
  const original = window.location.href
  window.history.replaceState({}, '', '/strategy-pool/simulations')
  const account = {
    id: 'rename', name: '旧名称', objective: 'annual_return', ranking: 'default', min_days: 0,
    direction: 'desc', start_date: '2026-11-26', initial_cash: 1000000, paused: false,
    cash: 1000000, value: 1000000, total_return: 0, max_drawdown: 0,
    turnover: 0, holding_count: 0, equity: [], positions: [], trades: [], trades_total: 0,
  }
  let renamed = false
  const api = vi.fn(async (url: string, init?: RequestInit) => {
    const data = init?.method === 'PATCH' ? { id: 'rename', name: '新名称', paused: null }
      : url.includes('/signal-days') ? []
      : url.includes('/rename?') ? { ...account, name: renamed ? '新名称' : account.name }
      : [renamed ? { ...account, name: '新名称' } : account]
    if (init?.method === 'PATCH') renamed = true
    return { ok: true, text: async () => JSON.stringify({ data }) }
  })
  vi.stubGlobal('fetch', api)
  const prompt = vi.spyOn(window, 'prompt').mockReturnValue('新名称')
  const wrapper = mount(defineComponent({
    setup: () => () => h(KeepAlive, null, { default: () => h(PoolSimulationsPage) }),
  }))
  try {
    await flushPromises()
    await wrapper.get('.simulation-name').trigger('click')
    await flushPromises()
    await wrapper.get('.simulation-rename').trigger('click')
    await flushPromises()
    const patch = api.mock.calls.find(([, init]) => init?.method === 'PATCH')
    expect(JSON.parse(String(patch?.[1]?.body))).toEqual({ name: '新名称' })
    expect(prompt).toHaveBeenCalledWith('输入新的模拟盘名称', '旧名称')
    expect(wrapper.text()).toContain('新名称 · 持仓与成交')
  } finally {
    wrapper.unmount()
    prompt.mockRestore()
    vi.unstubAllGlobals()
    window.history.replaceState({}, '', original)
  }
})

it('draws single-day comparisons as dots instead of an invisible polyline', async () => {
  const original = window.location.href
  window.history.replaceState({}, '', '/strategy-pool/simulations')
  const account = {
    id: 'one-day', name: '首日盘', objective: 'annual_return', ranking: 'default', min_days: 0,
    direction: 'desc', start_date: '2026-11-26', initial_cash: 1000000, paused: false,
    cash: 200000, value: 1074700, total_return: 0.0747, max_drawdown: 0, turnover: 0.8,
    holding_count: 5, equity: [{ date: '2026-11-27', value: 1074700 }],
    positions: [], trades: [], trades_total: 0,
  }
  const api = vi.fn(async (url: string) => {
    const data = url.includes('/signal-days') ? [] : [account]
    return { ok: true, text: async () => JSON.stringify({ data }) }
  })
  vi.stubGlobal('fetch', api)
  const wrapper = mount(defineComponent({
    setup: () => () => h(KeepAlive, null, { default: () => h(PoolSimulationsPage) }),
  }))
  try {
    await flushPromises()
    const dots = wrapper.findAll('.simulation-chart circle')
    expect(dots).toHaveLength(1)
    expect(dots[0].attributes('cx')).toBe('418')
    const dateLabels = wrapper.findAll('.simulation-chart text')
      .map(node => node.text()).filter(text => /^\d{4}-\d{2}-\d{2}$/.test(text))
    expect(dateLabels).toEqual(['2026-11-25', '2026-11-26', '2026-11-27', '2026-11-28', '2026-11-29'])
  } finally {
    wrapper.unmount()
    vi.unstubAllGlobals()
    window.history.replaceState({}, '', original)
  }
})

it('shows the detail return curve with range shortcuts and buy/sell markers', async () => {
  const original = window.location.href
  window.history.replaceState({}, '', '/strategy-pool/simulations')
  const account = {
    id: 'curve', name: '曲线盘', objective: 'annual_return', ranking: 'default', min_days: 0,
    direction: 'desc', start_date: '2026-06-30', initial_cash: 1000000, paused: false,
    cash: 900000, value: 1020000, total_return: 0.02, max_drawdown: 0,
    turnover: 0.1, holding_count: 1, equity: [
      { date: '2026-06-30', value: 1000000 },
      { date: '2026-11-26', value: 1010000 },
      { date: '2026-11-27', value: 1020000 },
    ],
    positions: [], trades: [], trades_total: 2,
    day_return: 0.01, benchmark_return: 0.02, holdings_return: 0.015,
    excess_benchmark: -0.01, excess_holdings: -0.005,
    metrics: {
      trading: {
        closed: 1, win_rate: 1, payoff_ratio: null, profit_factor: null,
        avg_win: 90, avg_loss: null, best_trade: 90, worst_trade: null, expectancy: 90,
        realized_pnl: 90, unrealized_pnl: 0, avg_rank: 1,
      },
      risk: {
        max_drawdown: 0, max_drawdown_days: 0, avg_drawdown: 0, current_drawdown: 0,
        underwater_ratio: 0, sample_days: 3, annual_return: null, volatility: null,
        downside_volatility: null, sharpe: null, sortino: null, calmar: null, var95: null,
        max_daily_gain: 0.01, max_daily_loss: 0, daily_win_rate: 0.5, avg_daily_return: 0.005,
        max_up_streak: 1, max_down_streak: 0,
      },
      relative: {
        days: 3, beta: null, correlation: null, tracking_error: null, information_ratio: null,
        up_capture: null, down_capture: null, excess_max_drawdown: 0,
      },
      concentration: { max_weight: 0, top3_weight: 0, hhi: 0, cash_ratio: 0.9 },
      ranges: [{ key: '1m', label: '近 1 月', start: '2026-06-30', strategy: 0.02, benchmark: 0.05, holdings: 0.03, excess_benchmark: -0.03, excess_holdings: -0.01 }],
      stages: [{ key: '5d', label: '近 5 日', days: 2, start: '2026-06-30', strategy: 0.02, benchmark: 0.05, holdings: 0.03, excess_benchmark: -0.03, excess_holdings: -0.01 }],
    },
    benchmark: {
      label: '沪深300ETF 510300 · 前复权',
      dates: ['2026-06-30', '2026-11-26', '2026-11-27'],
      totals: [4.0, 4.1, 4.2],
    },
    holdings_benchmark: {
      label: '持仓等权 · 每日再平衡',
      dates: ['2026-06-30', '2026-11-26', '2026-11-27'],
      totals: [1.0, 1.03, 1.05],
    },
    all_trades: [
      { date: '2026-06-30', signal_date: '2026-06-27', symbol: 'SH600000', direction: 'BUY', price: 10, size: 100, reason: '成立建仓' },
      { date: '2026-11-27', signal_date: '2026-11-26', symbol: 'SH600000', direction: 'SELL', price: 11, size: 100, reason: '卖出条件', pnl: 90 },
    ],
  }
  const api = vi.fn(async (url: string) => {
    const data = url.includes('/signal-days') ? [] : url.includes('/curve?') ? account : [account]
    return { ok: true, text: async () => JSON.stringify({ data }) }
  })
  vi.stubGlobal('fetch', api)
  const wrapper = mount(defineComponent({
    setup: () => () => h(KeepAlive, null, { default: () => h(PoolSimulationsPage) }),
  }))
  try {
    await flushPromises()
    expect(wrapper.text()).toContain('日收益')
    expect(wrapper.text()).toContain('沪深300同期')
    expect(wrapper.text()).toContain('持仓等权同期')
    await wrapper.get('.simulation-name').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('2026-06-30 → 2026-11-27 · 3 个交易日 · 2 笔交易')
    expect(wrapper.findAll('.segmented-tabs button').map(button => button.text()))
      .toEqual(['近 1 月', '近 3 月', '近半年', '近 1 年', '今年以来', '成立以来'])
    expect(wrapper.text()).toContain('沪深300ETF 510300 · 前复权')
    expect(wrapper.text()).toContain('持仓等权 · 每日再平衡')
    expect(wrapper.text()).toContain('区间表现')
    expect(wrapper.text()).toContain('收益与回撤')
    expect(wrapper.text()).toContain('风险调整')
    expect(wrapper.text()).toContain('相对沪深300')
    expect(wrapper.text()).toContain('交易与集中度')
    expect(wrapper.text()).toContain('已平仓')
    expect(wrapper.findAll('polyline[stroke="#f59e0b"]')).toHaveLength(1)
    expect(wrapper.findAll('polyline[stroke="#38bdf8"]')).toHaveLength(1)
    const labels = wrapper.findAll('svg g[aria-label]').map(marker => marker.attributes('aria-label') ?? '')
    expect(labels).toHaveLength(2)
    expect(labels[0]).toContain('标的：SH600000')
    expect(labels[0]).toContain('方向：买入')
    await wrapper.findAll('.segmented-tabs button')[0].trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('2026-11-26 → 2026-11-27 · 2 个交易日 · 1 笔交易')
    expect(wrapper.findAll('svg g[aria-label]').map(marker => marker.attributes('aria-label') ?? '').join('\n'))
      .toBe('日期：2026-11-27\n标的：SH600000\n方向：卖出\n价格：11.00\n数量：100\n盈亏：90.00\n原因：卖出条件\n信号日：2026-11-26')
  } finally {
    wrapper.unmount()
    vi.unstubAllGlobals()
    window.history.replaceState({}, '', original)
  }
})
