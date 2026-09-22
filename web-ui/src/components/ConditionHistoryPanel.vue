<script setup lang="ts">
import { computed, watch } from 'vue'

import {
  formatHistoryDate,
  formatNumber,
  formatPercent,
  historyDurationText,
  historyRangeText,
  historySideText,
  historyUniverseText,
  summaryConditionText,
  summaryNumber,
} from '../utils/conditionResearch'
import type { ResearchHistorySummary } from '../utils/conditionResearch'
import type { ValueDefinition } from '../types'

const props = defineProps<{
  history: ResearchHistorySummary[]
  loading: boolean
  activeId: string
  values: ValueDefinition[]
  page: number
}>()

const emit = defineEmits<{
  'update:page': [number]
  refresh: []
  load: [string]
  remove: [string]
}>()

// 历史列表：后端已按完成时间由近到远排好，这里只管切片分页。
const pageSize = 10
const pageCount = computed(() => Math.max(1, Math.ceil(props.history.length / pageSize)))
const pageRows = computed(() => props.history.slice(
  (props.page - 1) * pageSize,
  props.page * pageSize,
))
// 删到只剩一页时把页码收回来，否则会停在一个空白页上。
watch([pageCount], () => {
  if (props.page > pageCount.value) emit('update:page', pageCount.value)
})

function goPrev(): void {
  emit('update:page', Math.max(1, props.page - 1))
}

function goNext(): void {
  emit('update:page', Math.min(pageCount.value, props.page + 1))
}

function summaryText(item: ResearchHistorySummary): string {
  return summaryConditionText(props.values, item)
}
</script>

<template>
<section class="panel condition-history" data-testid="condition-research-history">
  <div class="panel-heading">
    <div><span class="section-kicker">SAVED RESEARCH</span><h2>历史研究</h2></div>
    <div class="history-head-actions">
      <span v-if="loading" class="history-status">读取中…</span>
      <button class="secondary-button" type="button" :disabled="loading" data-testid="condition-history-refresh" @click="emit('refresh')">刷新</button>
    </div>
  </div>
  <p class="helper">每次研究跑完会自动保存，只留最近 20 次。加载会一并还原当时的标的范围、买卖规则、日期区间和交易费用，方便在此基础上继续调整。</p>
  <div v-if="!history.length" class="inline-empty">暂无历史研究记录。</div>
  <template v-else>
    <ul class="history-list">
      <li
        v-for="item in pageRows"
        :key="item.history_id"
        class="history-card"
        :class="{ 'history-card-active': activeId === item.history_id }"
        :data-testid="`condition-history-item-${item.history_id}`"
      >
        <div class="history-card-head">
          <strong>{{ formatHistoryDate(item.created_at) }}</strong>
          <span v-if="activeId === item.history_id" class="history-badge">当前</span>
          <span v-if="historyDurationText(item)" class="history-duration">{{ historyDurationText(item) }}</span>
        </div>
        <div class="history-row">
          <span class="history-chip">{{ historyRangeText(item) }}</span>
          <span class="history-chip">{{ historyUniverseText(item) }}</span>
          <span class="history-chip">{{ historySideText(item) }}</span>
          <span v-if="summaryNumber(item, 'processed') !== null" class="history-chip">跑了 {{ summaryNumber(item, 'processed') }} 只</span>
        </div>
        <div class="history-row">
          <span class="history-metric">候选 <strong>{{ summaryNumber(item, 'candidate_count') ?? 0 }}</strong> 条</span>
          <span class="history-metric">合格 <strong>{{ summaryNumber(item, 'qualified_count') ?? 0 }}</strong> 条</span>
          <span class="history-metric">最强 <strong class="history-condition">{{ summaryText(item) }}</strong></span>
          <span v-if="summaryNumber(item, 'top_lift') !== null" class="history-metric">提升 <strong>{{ formatNumber(summaryNumber(item, 'top_lift'), 2) }}</strong></span>
          <span v-if="summaryNumber(item, 'top_outcome_rate') !== null" class="history-metric">命中后胜率 <strong>{{ formatPercent(summaryNumber(item, 'top_outcome_rate')) }}</strong></span>
          <span v-if="summaryNumber(item, 'top_baseline_rate') !== null" class="history-metric">基线 <strong>{{ formatPercent(summaryNumber(item, 'top_baseline_rate')) }}</strong></span>
          <span v-if="summaryNumber(item, 'top_instrument_count') !== null" class="history-metric">覆盖 <strong>{{ summaryNumber(item, 'top_instrument_count') }}</strong> 只</span>
        </div>
        <div class="history-card-actions">
          <button class="table-link" type="button" :disabled="loading" :data-testid="`condition-history-load-${item.history_id}`" @click="emit('load', item.history_id)">加载</button>
          <button class="table-link danger-link" type="button" :disabled="loading" :data-testid="`condition-history-remove-${item.history_id}`" @click="emit('remove', item.history_id)">删除</button>
        </div>
      </li>
    </ul>
    <div v-if="pageCount > 1" class="history-pagination" data-testid="condition-history-pagination">
      <span>共 {{ history.length }} 条 · 第 {{ page }} / {{ pageCount }} 页</span>
      <div>
        <button class="secondary-button" type="button" :disabled="page <= 1" data-testid="condition-history-prev" @click="goPrev">上一页</button>
        <button class="secondary-button" type="button" :disabled="page >= pageCount" data-testid="condition-history-next" @click="goNext">下一页</button>
      </div>
    </div>
  </template>
</section>
</template>
