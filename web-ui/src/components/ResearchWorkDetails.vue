<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { fetchMetadata } from '../api/formulaScreen'
import { buildIndicatorNameMap, indicatorDisplayName } from '../utils/indicatorNames'
import { formatTradingRule } from '../utils/strategyRules'
const props = defineProps<{ details?: Record<string, unknown> }>()
const names = ref<Map<string, string>>(new Map())
onMounted(async () => { try { names.value = buildIndicatorNameMap(await fetchMetadata()) } catch { /* Counts remain available when metadata is unavailable. */ } })
const work = computed(() => props.details ?? {})
function rule(side: string): string {
  const value = work.value[`${side}_rule`]
  return value && typeof value === 'object'
    ? formatTradingRule(value as Record<string, unknown>, id => indicatorDisplayName(names.value, id)) : ''
}
const buy = computed(() => rule('buy'))
const sell = computed(() => rule('sell'))
function metric(key: string, percent = false): string {
  const value = work.value[key]
  return typeof value === 'number' && Number.isFinite(value)
    ? percent ? `${(value * 100).toFixed(2)}%` : value.toFixed(2) : '—'
}
// 零成交候选的年化与夏普都是 0.0（一条纯现金曲线），光看那两个数会把「一次都没买进去」
// 显示成「收益 0.00%」的有效结果，所以成交笔数要单独摆出来。
function ordersText(): string {
  const value = work.value.best_orders
  return typeof value === 'number' && Number.isFinite(value) ? `${Math.round(value)} 笔` : '—'
}
</script>
<template>
  <div v-if="work.stage" class="research-work" data-testid="research-work-details">
    <p><strong>{{ work.stage }}</strong> · 本组已完成 {{ work.current }} / {{ work.total }}</p>
    <p v-if="buy">买入条件：{{ buy }}</p>
    <p v-if="sell">卖出条件：{{ sell }}</p>
    <p>本组搜索观察到的最高年化 {{ metric('best_return', true) }} · 最高夏普 {{ metric('best_sharpe') }} · 最多成交 {{ ordersText() }}<span v-if="typeof work.elapsed_seconds === 'number'"> · 本组累计 {{ Math.floor(work.elapsed_seconds) }} 秒</span></p>
    <small>后台计算中，完成一批评估后更新计数；最高指标属于历史模拟结果。成交 0 笔表示这组规则一次都没买进去，此时年化与夏普都会是 0，不是有效结果。</small>
  </div>
</template>
<style scoped>
.research-work { margin: 10px 0; padding: 12px; border-left: 3px solid var(--accent); background: rgba(25, 199, 122, .05); overflow-wrap: anywhere; }
p { margin: 4px 0; } small { opacity: .7; }
</style>
