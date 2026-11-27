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
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    const post = api.mock.calls.find(([, init]) => init?.method === 'POST')
    expect(JSON.parse(String(post?.[1]?.body))).toMatchObject({
      name: '超额盘', objective: 'sharpe', ranking: 'composite', start_date: '2026-11-26', max_positions: 3,
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
    cash: 1000000, value: 1000000, total_return: 0, max_drawdown: 0, total_fees: 0,
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
