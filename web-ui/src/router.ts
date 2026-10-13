/**
 * 路由与导航的单一来源。
 *
 * 以前这两件事都散落在 App.vue 和各页面里：App.vue 用一串 if 把路径映射到组件，
 * 而 17 个页面各自手写一遍 16 项导航链接（App.vue 的注释里自己也承认「加新页面时
 * 全部页面都要同步补一项」）。现在路径、显示名、组件三者放在一张表里，
 * 加页面只需要动这里。
 */

import { computed, ref } from 'vue'
import type { Component } from 'vue'

import CandidateLibraryPage from './pages/CandidateLibraryPage.vue'
import ConditionResearchPage from './pages/ConditionResearchPage.vue'
import FactorAttributionPage from './pages/FactorAttributionPage.vue'
import FactorTestPage from './pages/FactorTestPage.vue'
import FormulaScreenPage from './pages/FormulaScreenPage.vue'
import HistoryComparePage from './pages/HistoryComparePage.vue'
import IndividualResearchPage from './pages/IndividualResearchPage.vue'
import InstrumentLabPage from './pages/InstrumentLabPage.vue'
import MarketDataPage from './pages/MarketDataPage.vue'
import PortfolioBacktestPage from './pages/PortfolioBacktestPage.vue'
import StrategyDiscoveryPage from './pages/StrategyDiscoveryPage.vue'
import StrategyFitnessPage from './pages/StrategyFitnessPage.vue'
import StrategyPoolDetailPage from './pages/StrategyPoolDetailPage.vue'
import StrategyPoolPage from './pages/StrategyPoolPage.vue'
import StrategyTrackingPage from './pages/StrategyTrackingPage.vue'
import SwingResearchPage from './pages/SwingResearchPage.vue'
import UserManualPage from './pages/UserManualPage.vue'

/** 顶部导航的一项。顺序即显示顺序。 */
export interface NavItem {
  path: string
  label: string
}

export const NAV_ITEMS: readonly NavItem[] = [
  { path: '/aquest', label: '公式选股' },
  { path: '/portfolio-backtest', label: '组合回测' },
  { path: '/strategy-lab', label: '策略实验室' },
  { path: '/condition-research', label: '买卖条件研究' },
  { path: '/individual-research', label: '个股研究' },
  { path: '/strategy-pool', label: '全市场策略池' },
  { path: '/strategy-fitness', label: '策略适配性' },
  { path: '/market-data', label: '本地行情' },
  { path: '/swing-research', label: '波段研究' },
  { path: '/strategy-tracking', label: '策略追踪' },
  { path: '/factor-test', label: '因子检验' },
  { path: '/history-compare', label: '历史对比' },
  { path: '/instrument-lab', label: '标的检验台' },
  { path: '/candidate-library', label: '候选库' },
  { path: '/factor-attribution', label: '因子归因' },
  { path: '/user-manual', label: '使用手册' },
]

interface RouteDefinition {
  /** 匹配用；越具体的路由必须排在越前面。 */
  test: (pathname: string) => boolean
  component: Component
  /**
   * 是否需要随 URL 变化重建实例。
   * 详情页（如 /strategy-pool/SH/600000）要在不同标的间切换时重新取数，
   * 靠的就是这个 key；列表页则相反，要保持 KeepAlive 复用。
   */
  keyed?: boolean
}

/** 前缀匹配：/strategy-pool 也能命中 /strategy-pool/SH/600000。 */
function prefix(path: string): (pathname: string) => boolean {
  return (pathname) => pathname === path || pathname.startsWith(`${path}/`)
}

const ROUTES: readonly RouteDefinition[] = [
  { test: prefix('/market-data'), component: MarketDataPage },
  { test: prefix('/condition-research'), component: ConditionResearchPage },
  { test: prefix('/swing-research'), component: SwingResearchPage },
  { test: prefix('/strategy-fitness'), component: StrategyFitnessPage },
  { test: prefix('/strategy-lab'), component: StrategyDiscoveryPage },
  { test: prefix('/strategy-discovery'), component: StrategyDiscoveryPage },
  { test: prefix('/portfolio-backtest'), component: PortfolioBacktestPage },
  { test: prefix('/strategy-tracking'), component: StrategyTrackingPage },
  { test: prefix('/factor-test'), component: FactorTestPage },
  { test: prefix('/factor-attribution'), component: FactorAttributionPage },
  { test: prefix('/history-compare'), component: HistoryComparePage },
  { test: prefix('/instrument-lab'), component: InstrumentLabPage },
  { test: prefix('/individual-research'), component: IndividualResearchPage },
  {
    test: (pathname) => /^\/strategy-pool\/(SH|SZ)\/\d{6}$/.test(pathname),
    component: StrategyPoolDetailPage,
    keyed: true,
  },
  { test: prefix('/strategy-pool'), component: StrategyPoolPage },
  { test: prefix('/candidate-library'), component: CandidateLibraryPage },
  { test: prefix('/user-manual'), component: UserManualPage },
  { test: prefix('/aquest'), component: FormulaScreenPage },
  // 兜底：站点根路径和未知路径都落到公式选股。
  { test: () => true, component: FormulaScreenPage },
]

export const currentPath = ref(
  typeof window !== 'undefined' ? window.location.pathname : '/aquest',
)

export function syncPath(): void {
  currentPath.value = window.location.pathname
}

/** 由 App.vue 的链接拦截调用：改地址栏并同步，不重新加载页面。 */
export function navigateTo(href: string): void {
  const url = new URL(href, window.location.origin)
  window.history.pushState({}, '', `${url.pathname}${url.search}${url.hash}`)
  syncPath()
}

export const currentRoute = computed(
  () => ROUTES.find((route) => route.test(currentPath.value)) ?? ROUTES[ROUTES.length - 1]!,
)

/**
 * 当前路径对应的 KeepAlive key。只有详情页需要，其余页面靠 undefined 保持复用。
 */
export const currentRouteKey = computed(() =>
  currentRoute.value.keyed ? currentPath.value : undefined,
)

/**
 * 顶部导航里该点亮哪一项：取路径最长的前缀项，这样策略池详情页也会点亮
 * 「全市场策略池」而不是什么都不亮。
 */
export function activeNavPath(pathname: string): string | undefined {
  const matched = NAV_ITEMS.filter(
    (item) => pathname === item.path || pathname.startsWith(`${item.path}/`),
  )
  return matched.sort((a, b) => b.path.length - a.path.length)[0]?.path
}
