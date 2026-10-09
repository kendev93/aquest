import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'

import MarketCandlestickChart from './MarketCandlestickChart.vue'

// jsdom 30 起 PointerEvent 是原生实现，clientX/pointerId 只能通过构造函数初始化；
// vue-test-utils 的 trigger 会在构造完成后再赋值一次，命中原型上只读的 getter 而抛错。
async function firePointer(element: Element, type: string, clientX: number, pointerId: number) {
  element.dispatchEvent(
    new PointerEvent(type, {
      bubbles: true,
      cancelable: true,
      button: 0,
      pointerType: 'mouse',
      clientX,
      pointerId,
    }),
  )
  await nextTick()
}

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
    await firePointer(surface.element, 'pointerdown', 100, 1)
    await firePointer(surface.element, 'pointermove', 500, 1)
    await firePointer(surface.element, 'pointerup', 500, 1)

    expect(wrapper.get('[data-testid="market-chart-note"]').text()).toContain('第 1–220 根')

    Object.defineProperty(surface.element, 'getBoundingClientRect', {
      configurable: true,
      value: () => ({ left: 0, width: 960 }),
    })
    await firePointer(surface.element, 'pointerdown', 480, 2)
    await firePointer(surface.element, 'pointerup', 480, 2)
    await surface.trigger('click', { clientX: 480 })
    expect(wrapper.get('[data-testid="market-chart-selection"]').text()).toContain('开 10.00')
  })
})
