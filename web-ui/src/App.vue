<script setup lang="ts">
import BacktestPage from './pages/BacktestPage.vue'
import FormulaScreenPage from './pages/FormulaScreenPage.vue'
import MarketDataPage from './pages/MarketDataPage.vue'
import PortfolioBacktestPage from './pages/PortfolioBacktestPage.vue'
import StrategyFitnessPage from './pages/StrategyFitnessPage.vue'

const pathname = typeof window !== 'undefined' ? window.location.pathname : '/indicator-lab'
const isIndicatorLab = pathname === '/' || pathname.startsWith('/indicator-lab') || pathname.startsWith('/formula-screen')
const isMarketData = pathname.startsWith('/market-data')
const isStrategyFitness = pathname.startsWith('/strategy-fitness')
const isPortfolioBacktest = pathname.startsWith('/portfolio-backtest')
const isBacktest = pathname.startsWith('/backtest')
</script>

<template>
  <MarketDataPage v-if="isMarketData" />
  <StrategyFitnessPage v-else-if="isStrategyFitness" />
  <PortfolioBacktestPage v-else-if="isPortfolioBacktest" />
  <BacktestPage v-else-if="isBacktest" />
  <FormulaScreenPage v-else-if="isIndicatorLab" />
  <FormulaScreenPage v-else />
</template>
