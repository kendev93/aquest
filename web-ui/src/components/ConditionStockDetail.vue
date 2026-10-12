<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import {
  formatConditionText,
  formatNumber,
  formatPercent,
  winRateDelta,
} from '../utils/conditionResearch'
import type {
  ConditionResearchCandidate,
  ConditionResearchReport,
  ValueDefinition,
} from '../types'

const props = defineProps<{
  report: ConditionResearchReport | null
  values: ValueDefinition[]
  view: string
}>()

const emit = defineEmits<{ 'update:view': [string] }>()

// 选中哪套规则由外面决定：候选表点「个股」也是改这个值，两边要联动。
const selectedStockView = computed({
  get: () => props.view,
  set: (next: string) => emit('update:view', next),
})
const stockFilter = ref('')
const stockSort = ref<'win_rate' | 'payoff_ratio' | 'total_return' | 'sharpe' | 'code'>('win_rate')
const stockPage = ref(1)
const stockPageSize = 50

const selectedStockRows = computed(() => {
  if (!props.report) return []
  const [ruleId, period] = selectedStockView.value.split(':')
  if (ruleId === 'baseline') {
    return period === 'holdout'
      ? props.report.baseline.holdout?.stocks ?? []
      : props.report.baseline.full_period?.stocks ?? []
  }
  const candidate = props.report.candidates.find((item) => item.candidate_id === ruleId)
  if (!candidate) return []
  return period === 'holdout'
    ? candidate.holdout?.stocks ?? []
    : candidate.full_period?.stocks ?? []
})

const stockViewOptions = computed(() => {
  const options = [
    { value: 'baseline:full', label: '基础规则 · 全区间' },
    { value: 'baseline:holdout', label: '基础规则 · 最终留出' },
  ]
  for (const candidate of props.report?.candidates ?? []) {
    if (candidate.full_period) {
      options.push({
        value: candidate.candidate_id + ':full',
        label: (candidate.side === 'buy' ? '买入过滤' : '卖出触发')
          + ' · 全区间 · ' + conditionText(candidate),
      })
    }
    if (candidate.holdout) {
      options.push({
        value: candidate.candidate_id + ':holdout',
        label: (candidate.side === 'buy' ? '买入过滤' : '卖出触发')
          + ' · 最终留出 · ' + conditionText(candidate),
      })
    }
  }
  return options
})

const stockPeriodLabel = computed(() => (
  selectedStockView.value.endsWith(':holdout') ? '最终留出期' : '完整研究区间'
))

const filteredStockRows = computed(() => {
  const query = stockFilter.value.trim().toLowerCase()
  const matching = selectedStockRows.value.filter((stock) => (
    !query
    || stock.market.toLowerCase().includes(query)
    || stock.code.toLowerCase().includes(query)
    || (stock.name ?? '').toLowerCase().includes(query)
  ))
  return matching.sort((left, right) => {
    if (stockSort.value === 'code') {
      return (left.market + left.code).localeCompare(right.market + right.code)
    }
    const leftValue = left[stockSort.value]
    const rightValue = right[stockSort.value]
    return (rightValue ?? Number.NEGATIVE_INFINITY) - (leftValue ?? Number.NEGATIVE_INFINITY)
  })
})

const stockPageCount = computed(() => Math.max(1, Math.ceil(filteredStockRows.value.length / stockPageSize)))

const selectedPageRows = computed(() => (
  filteredStockRows.value.slice((stockPage.value - 1) * stockPageSize, stockPage.value * stockPageSize)
))

watch([selectedStockView, stockFilter, stockSort], () => {
  stockPage.value = 1
})

// 新报告（或历史记录）到达时必须回到第一页：沿用旧页码会出现
// 「第 3 / 1 页 + 空表体」，既不显示数据也不命中空态提示。
watch(() => props.report, () => {
  stockPage.value = 1
})

function conditionText(candidate: ConditionResearchCandidate): string {
  return formatConditionText(props.values, candidate)
}
</script>

<template>
<section class="condition-stock-details">
  <div class="section-heading">
    <div><span class="section-kicker">PER-STOCK DETAIL</span><h3>{{ stockPeriodLabel }}个股表现</h3></div>
    <div class="condition-stock-controls">
      <div class="condition-stock-view">
        <label for="condition-stock-view">规则与区间</label>
        <select id="condition-stock-view" v-model="selectedStockView" data-testid="condition-stock-view">
          <option v-for="option in stockViewOptions" :key="option.value" :value="option.value">{{ option.label }}</option>
        </select>
      </div>
      <div class="condition-stock-view">
        <label for="condition-stock-filter">筛选股票</label>
        <input id="condition-stock-filter" v-model="stockFilter" data-testid="condition-stock-filter" type="search" placeholder="代码或名称">
      </div>
      <div class="condition-stock-view">
        <label for="condition-stock-sort">排序方式</label>
        <select id="condition-stock-sort" v-model="stockSort" data-testid="condition-stock-sort">
          <option value="win_rate">胜率从高到低</option>
          <option value="payoff_ratio">盈亏比从高到低</option>
          <option value="total_return">收益从高到低</option>
          <option value="sharpe">Sharpe 从高到低</option>
          <option value="code">市场和代码</option>
        </select>
      </div>
    </div>
  </div>
  <div v-if="!selectedStockRows.length" class="inline-empty">
    {{ selectedStockView.startsWith('baseline') ? '基础规则没有可展示的逐股票结果。' : '此候选未进入对应区间复验列表。' }}
  </div>
  <template v-else>
    <div class="condition-stock-pagination">
      <span>筛选出 {{ filteredStockRows.length }} / {{ selectedStockRows.length }} 只股票 · 第 {{ stockPage }} / {{ stockPageCount }} 页</span>
      <div>
        <button class="secondary-button" type="button" :disabled="stockPage <= 1" @click="stockPage -= 1">上一页</button>
        <button class="secondary-button" type="button" :disabled="stockPage >= stockPageCount" @click="stockPage += 1">下一页</button>
      </div>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>市场</th><th>代码</th><th>名称</th><th>交易数</th><th>胜率</th><th>较基础规则</th><th>盈亏比</th><th>收益</th><th>Sharpe</th><th>回撤</th></tr></thead>
        <tbody>
          <tr v-for="stock in selectedPageRows" :key="stock.market + stock.code">
            <td>{{ stock.market }}</td><td>{{ stock.code }}</td><td>{{ stock.name || '—' }}</td>
            <td>{{ stock.closed_trades }}</td><td>{{ formatPercent(stock.win_rate) }}</td>
            <td>{{ winRateDelta(stock) }}</td>
            <td>{{ formatNumber(stock.payoff_ratio) }}</td><td>{{ formatPercent(stock.total_return) }}</td>
            <td>{{ formatNumber(stock.sharpe) }}</td><td>{{ formatPercent(stock.max_drawdown) }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </template>
</section>
</template>

