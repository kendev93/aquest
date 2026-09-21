<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'

import ConditionResearchPage from './pages/ConditionResearchPage.vue'
import FactorTestPage from './pages/FactorTestPage.vue'
import FormulaScreenPage from './pages/FormulaScreenPage.vue'
import HistoryComparePage from './pages/HistoryComparePage.vue'
import InstrumentLabPage from './pages/InstrumentLabPage.vue'
import MarketDataPage from './pages/MarketDataPage.vue'
import PortfolioBacktestPage from './pages/PortfolioBacktestPage.vue'
import StrategyDiscoveryPage from './pages/StrategyDiscoveryPage.vue'
import StrategyFitnessPage from './pages/StrategyFitnessPage.vue'
import StrategyTrackingPage from './pages/StrategyTrackingPage.vue'
import SwingResearchPage from './pages/SwingResearchPage.vue'

const currentPath = ref(typeof window !== 'undefined' ? window.location.pathname : '/indicator-lab')

const routeComponent = computed(() => {
  const pathname = currentPath.value
  if (pathname.startsWith('/market-data')) return MarketDataPage
  if (pathname.startsWith('/condition-research')) return ConditionResearchPage
  if (pathname.startsWith('/swing-research')) return SwingResearchPage
  if (pathname.startsWith('/strategy-fitness')) return StrategyFitnessPage
  if (pathname.startsWith('/strategy-lab') || pathname.startsWith('/strategy-discovery')) {
    return StrategyDiscoveryPage
  }
  if (pathname.startsWith('/portfolio-backtest')) return PortfolioBacktestPage
  if (pathname.startsWith('/strategy-tracking')) return StrategyTrackingPage
  if (pathname.startsWith('/factor-test')) return FactorTestPage
  if (pathname.startsWith('/history-compare')) return HistoryComparePage
  if (pathname.startsWith('/instrument-lab')) return InstrumentLabPage
  return FormulaScreenPage
})

function syncPath(): void {
  currentPath.value = window.location.pathname
}

function handlePopState(): void {
  syncPath()
}

function handleNavigation(event: MouseEvent): void {
  if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
    return
  }
  const target = event.target
  if (!(target instanceof Element)) return
  const link = target.closest('a[href]')
  if (!(link instanceof HTMLAnchorElement) || link.target === '_blank' || link.hasAttribute('download')) {
    return
  }
  const href = link.getAttribute('href')
  if (!href || !href.startsWith('/') || href.startsWith('//')) return
  const url = new URL(href, window.location.origin)
  if (url.origin !== window.location.origin) return
  event.preventDefault()
  window.history.pushState({}, '', `${url.pathname}${url.search}${url.hash}`)
  syncPath()
}

onMounted(() => window.addEventListener('popstate', handlePopState))
onUnmounted(() => window.removeEventListener('popstate', handlePopState))
</script>

<template>
  <div class="app-shell" @click.capture="handleNavigation">
    <KeepAlive>
      <component :is="routeComponent" />
    </KeepAlive>
  </div>
</template>
