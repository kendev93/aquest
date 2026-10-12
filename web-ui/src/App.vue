<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'

import CandidateLibraryPage from './pages/CandidateLibraryPage.vue'
import ConditionResearchPage from './pages/ConditionResearchPage.vue'
import FactorAttributionPage from './pages/FactorAttributionPage.vue'
import FactorTestPage from './pages/FactorTestPage.vue'
import FormulaScreenPage from './pages/FormulaScreenPage.vue'
import HistoryComparePage from './pages/HistoryComparePage.vue'
import InstrumentLabPage from './pages/InstrumentLabPage.vue'
import IndividualResearchPage from './pages/IndividualResearchPage.vue'
import MarketDataPage from './pages/MarketDataPage.vue'
import PortfolioBacktestPage from './pages/PortfolioBacktestPage.vue'
import StrategyDiscoveryPage from './pages/StrategyDiscoveryPage.vue'
import StrategyFitnessPage from './pages/StrategyFitnessPage.vue'
import StrategyPoolPage from './pages/StrategyPoolPage.vue'
import StrategyPoolDetailPage from './pages/StrategyPoolDetailPage.vue'
import StrategyTrackingPage from './pages/StrategyTrackingPage.vue'
import SwingResearchPage from './pages/SwingResearchPage.vue'
import UserManualPage from './pages/UserManualPage.vue'

const currentPath = ref(typeof window !== 'undefined' ? window.location.pathname : '/aquest')

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
  if (pathname.startsWith('/factor-attribution')) return FactorAttributionPage
  if (pathname.startsWith('/history-compare')) return HistoryComparePage
  if (pathname.startsWith('/instrument-lab')) return InstrumentLabPage
  if (pathname.startsWith('/individual-research')) return IndividualResearchPage
  if (/^\/strategy-pool\/(SH|SZ)\/\d{6}$/.test(pathname)) return StrategyPoolDetailPage
  if (pathname.startsWith('/strategy-pool')) return StrategyPoolPage
  if (pathname.startsWith('/candidate-library')) return CandidateLibraryPage
  if (pathname.startsWith('/user-manual')) return UserManualPage
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
  <!--
    KeepAlive 是刻意的：切走再切回来时表单里的配置还在，不然调了半天的参数会因为
    点了一次导航而清空。代价是页面实例不会重新创建，onMounted 里的初始化不会重跑
    ——需要「每次进页面都刷新」的东西不能只写在 onMounted 里。

    注意导航链接是每个页面各自手写的（每个页面一份，加新页面时全部页面都要同步补
    一项），这里的 switch 也要加一个分支。`src/data/pageGuides.spec.ts` 盯着这件事。
  -->
  <div class="app-shell" @click.capture="handleNavigation">
    <KeepAlive>
      <component :is="routeComponent" :key="routeComponent === StrategyPoolDetailPage ? currentPath : undefined" />
    </KeepAlive>
  </div>
</template>
