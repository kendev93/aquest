import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'

import MarketCandlestickChart from './MarketCandlestickChart.vue'

function candles(count: number) {
  return Array.from({ length: count }, (_, index) => ({
    date: `2024-01-${String(index + 1).padStart(2, '0')}`,
    open: 10,
    high: 11,
    low: 9,
    close: 10.5,
    volume: 1000,
    amount: 10000,
    ma: {},
    rsi14: null,
    macd: null,
    macd_signal: null,
    macd_histogram: null,
  }))
}

describe('MarketCandlestickChart', () => {
  it('starts at the latest window and lets the user drag to older candles', async () => {
    const wrapper = mount(MarketCandlestickChart, {
      props: {
        candles: candles(300),
        maKeys: [],
        period: 'daily',
        showMacd: false,
        showRsi: false,
        showVolume: false,
        formulaPanels: [],
      },
    })

    expect(wrapper.get('[data-testid="market-chart-note"]').text()).toContain('第 81–300 根')

    const surface = wrapper.get('[data-testid="market-chart-pan-surface"]')
    await surface.trigger('pointerdown', { clientX: 100, pointerId: 1, button: 0 })
    await surface.trigger('pointermove', { clientX: 500, pointerId: 1 })
    await surface.trigger('pointerup', { clientX: 500, pointerId: 1 })

    expect(wrapper.get('[data-testid="market-chart-note"]').text()).toContain('第 1–220 根')

    Object.defineProperty(surface.element, 'getBoundingClientRect', {
      configurable: true,
      value: () => ({ left: 0, width: 960 }),
    })
    await surface.trigger('pointerdown', { clientX: 480, pointerId: 2, button: 0 })
    await surface.trigger('pointerup', { clientX: 480, pointerId: 2 })
    await surface.trigger('click', { clientX: 480 })
    expect(wrapper.get('[data-testid="market-chart-selection"]').text()).toContain('开 10.00')
  })
})
