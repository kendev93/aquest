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
    template: '<main>行情页面<a href="/aquest">公式选股</a></main>',
  },
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

vi.mock('./pages/StrategyPoolPage.vue', () => ({
  default: { name: 'StrategyPoolPage', template: '<main>全市场策略池</main>' },
}))

vi.mock('./pages/SwingResearchPage.vue', () => ({
  default: { name: 'SwingResearchPage', template: '<main>波段研究</main>' },
}))

describe('App navigation', () => {
  beforeEach(() => {
    window.history.replaceState({}, '', '/aquest')
  })

  afterEach(() => {
    window.history.replaceState({}, '', '/aquest')
  })

  it('switches internal tabs without reloading and preserves cached page state', async () => {
    const wrapper = mount(App)
    await flushPromises()

    await wrapper.get('[data-testid="formula-draft"]').setValue('用户正在编辑的条件')
    await wrapper.get('a[href="/market-data"]').trigger('click')
    await flushPromises()

    expect(window.location.pathname).toBe('/market-data')
    expect(wrapper.text()).toContain('行情页面')

    await wrapper.get('a[href="/aquest"]').trigger('click')
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

  it('opens the swing research section on its own route', async () => {
    const wrapper = mount(App)
    window.history.pushState({}, '', '/swing-research')
    window.dispatchEvent(new PopStateEvent('popstate'))
    await flushPromises()

    expect(wrapper.text()).toContain('波段研究')
  })

  it('opens the full-market strategy pool on its own route', async () => {
    const wrapper = mount(App)
    window.history.pushState({}, '', '/strategy-pool')
    window.dispatchEvent(new PopStateEvent('popstate'))
    await flushPromises()

    expect(wrapper.text()).toContain('全市场策略池')
  })
})
