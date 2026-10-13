import { describe, expect, it } from 'vitest'

import {
  NAV_ITEMS,
  activeNavPath,
  currentPath,
  currentRoute,
  currentRouteKey,
  navigateTo,
  syncPath,
} from './router'
import CandidateLibraryPage from './pages/CandidateLibraryPage.vue'
import FormulaScreenPage from './pages/FormulaScreenPage.vue'
import MarketDataPage from './pages/MarketDataPage.vue'
import PortfolioBacktestPage from './pages/PortfolioBacktestPage.vue'
import StrategyDiscoveryPage from './pages/StrategyDiscoveryPage.vue'
import StrategyPoolDetailPage from './pages/StrategyPoolDetailPage.vue'
import StrategyPoolPage from './pages/StrategyPoolPage.vue'
import UserManualPage from './pages/UserManualPage.vue'

describe('router', () => {
  it('resolves each top-level path to its own page', () => {
    const expected: [string, unknown][] = [
      ['/aquest', FormulaScreenPage],
      ['/market-data', MarketDataPage],
      ['/strategy-lab', StrategyDiscoveryPage],
      ['/strategy-discovery', StrategyDiscoveryPage],
      ['/portfolio-backtest', PortfolioBacktestPage],
      ['/candidate-library', CandidateLibraryPage],
      ['/user-manual', UserManualPage],
    ]
    for (const [path, page] of expected) {
      currentPath.value = path
      expect(currentRoute.value.component, `${path} 路由不对`).toBe(page)
    }
  })

  it('sends the pool detail url to the detail page and elsewhere to the list', () => {
    currentPath.value = '/strategy-pool/SH/600000'
    expect(currentRoute.value.component).toBe(StrategyPoolDetailPage)
    expect(currentRouteKey.value).toBe('/strategy-pool/SH/600000')

    currentPath.value = '/strategy-pool'
    expect(currentRoute.value.component).toBe(StrategyPoolPage)
    // 列表页不加 key，KeepAlive 才能保留已加载的列表与滚动位置。
    expect(currentRouteKey.value).toBeUndefined()
  })

  it('falls back to the formula screen for unknown paths', () => {
    currentPath.value = '/not-a-page'
    expect(currentRoute.value.component).toBe(FormulaScreenPage)
  })

  it('highlights the longest matching nav entry so detail pages stay anchored', () => {
    expect(activeNavPath('/strategy-pool/SH/600000')).toBe('/strategy-pool')
    expect(activeNavPath('/factor-test')).toBe('/factor-test')
    expect(activeNavPath('/')).toBeUndefined()
  })

  it('keeps every nav entry pointing at a known path', () => {
    const paths = new Set(NAV_ITEMS.map((item) => item.path))
    expect(paths.has('/aquest')).toBe(true)
    expect(new Set(NAV_ITEMS.map((item) => item.label)).size).toBe(NAV_ITEMS.length)
  })

  it('moves the browser to a new path without reloading', () => {
    const before = window.location.pathname
    navigateTo('/market-data')
    expect(window.location.pathname).toBe('/market-data')
    expect(currentPath.value).toBe('/market-data')
    window.history.replaceState({}, '', before)
    syncPath()
    expect(currentPath.value).toBe(before)
  })
})
