import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import App from './App.vue'

vi.mock('./pages/FormulaScreenPage.vue', () => ({
  default: {
    name: 'FormulaScreenPage',
    template: '<main><input data-testid="formula-draft"><a href="/market-data">行情</a></main>',
  },
}))

vi.mock('./pages/MarketDataPage.vue', () => ({
  default: {
    name: 'MarketDataPage',
    template: '<main>行情页面<a href="/indicator-lab">公式选股</a></main>',
  },
}))

vi.mock('./pages/BacktestPage.vue', () => ({
  default: { name: 'BacktestPage', template: '<main>单股回测</main>' },
}))

vi.mock('./pages/ConditionResearchPage.vue', () => ({
  default: { name: 'ConditionResearchPage', template: '<main>买卖条件研究</main>' },
}))

vi.mock('./pages/PortfolioBacktestPage.vue', () => ({
  default: { name: 'PortfolioBacktestPage', template: '<main>组合回测</main>' },
}))

vi.mock('./pages/StrategyDiscoveryPage.vue', () => ({
  default: { name: 'StrategyDiscoveryPage', template: '<main>策略实验室</main>' },
}))

vi.mock('./pages/StrategyFitnessPage.vue', () => ({
  default: { name: 'StrategyFitnessPage', template: '<main>策略适配性</main>' },
}))

describe('App navigation', () => {
  beforeEach(() => {
    window.history.replaceState({}, '', '/indicator-lab')
  })

  afterEach(() => {
    window.history.replaceState({}, '', '/indicator-lab')
  })

  it('switches internal tabs without reloading and preserves cached page state', async () => {
    const wrapper = mount(App)
    await flushPromises()

    await wrapper.get('[data-testid="formula-draft"]').setValue('用户正在编辑的条件')
    await wrapper.get('a[href="/market-data"]').trigger('click')
    await flushPromises()

    expect(window.location.pathname).toBe('/market-data')
    expect(wrapper.text()).toContain('行情页面')

    await wrapper.get('a[href="/indicator-lab"]').trigger('click')
    await flushPromises()

    expect((wrapper.get('[data-testid="formula-draft"]').element as HTMLInputElement).value).toBe('用户正在编辑的条件')
  })

  it('opens the independent buy and sell condition research section', async () => {
    const wrapper = mount(App)
    window.history.pushState({}, '', '/condition-research')
    window.dispatchEvent(new PopStateEvent('popstate'))
    await flushPromises()

    expect(wrapper.text()).toContain('买卖条件研究')
  })
})
