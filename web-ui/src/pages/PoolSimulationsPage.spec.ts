import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h, KeepAlive } from 'vue'
import { expect, it, vi } from 'vitest'

import PoolSimulationsPage from './PoolSimulationsPage.vue'

it('creates from the chosen signal-board settings and opens the account detail', async () => {
  const original = window.location.href
  window.history.replaceState({}, '', '/strategy-pool/simulations?create=1&objective=sharpe&ranking=excess&min_days=20')
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
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    const post = api.mock.calls.find(([, init]) => init?.method === 'POST')
    expect(JSON.parse(String(post?.[1]?.body))).toMatchObject({
      name: '超额盘', objective: 'sharpe', ranking: 'excess', min_days: 20, start_date: '2026-11-26',
    })
    expect(wrapper.text()).toContain('超额盘 · 持仓与成交')
  } finally {
    wrapper.unmount()
    vi.unstubAllGlobals()
    window.history.replaceState({}, '', original)
  }
})
