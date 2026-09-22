<script setup lang="ts">
import { computed } from 'vue'

import {
  formatConditionText,
  formatPercent,
  rollingNote,
  rollingWindowBaseline,
} from '../utils/conditionResearch'
import type {
  ConditionResearchCandidate,
  ConditionResearchReport,
  ValueDefinition,
} from '../types'
import TermHint from './TermHint.vue'

// 报告可能为 null：整块由外层 v-if 决定要不要渲染，这里只负责取值时别炸。
const props = defineProps<{
  report: ConditionResearchReport | null
  values: ValueDefinition[]
}>()

const rolling = computed(() => props.report?.rolling ?? null)
const candidates = computed(() => (
  (props.report?.candidates ?? []).filter(
    (candidate) => (candidate.windows ?? []).length > 0 && candidate.consistency,
  )
))
const baselineWindows = computed(() => props.report?.baseline.windows ?? [])

function conditionText(candidate: ConditionResearchCandidate): string {
  return formatConditionText(props.values, candidate)
}

function baselineWinRate(index: number): number | null {
  return rollingWindowBaseline(baselineWindows.value, index)
}
</script>

<template>
<div v-if="rolling" class="condition-rolling">
  <div class="section-heading">
    <div>
      <span class="section-kicker">ROLLING RE-VALIDATION</span>
      <h3>滚动复验<TermHint term="rollingConsistency" /></h3>
    </div>
  </div>
  <p class="helper">
    同一批候选在留出期的 {{ rolling.used }} 个连续窗口上各回放一次。
    表里只列事实——几个窗口跑赢了同一窗口的基线规则，以及各窗的原始胜率。
  </p>
  <p v-if="rolling.note" class="scope-helper" data-testid="condition-rolling-note">{{ rolling.note }}</p>
  <div class="table-wrap">
    <table>
      <caption class="sr-only">滚动复验逐窗结果</caption>
      <thead>
        <tr>
          <th>候选</th>
          <th>优于基线</th>
          <th>中位胜率</th>
          <th>最差窗口</th>
          <th
            v-for="window in baselineWindows"
            :key="window.index"
            :title="'窗口 ' + window.index + '：' + window.start_date + ' 至 ' + window.end_date + '，基线胜率 ' + formatPercent(window.win_rate)"
          >窗口 {{ window.index }}</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="candidate in candidates"
          :key="candidate.candidate_id"
          :data-testid="'condition-rolling-' + candidate.candidate_id"
        >
          <td class="condition-rule">{{ conditionText(candidate) }}</td>
          <td :data-testid="'rolling-consistency-' + candidate.candidate_id">
            {{ candidate.consistency?.better_than_baseline }}/{{ candidate.consistency?.evaluated }}
            <span v-if="(candidate.consistency?.skipped ?? 0) > 0" class="muted-code">（{{ candidate.consistency?.skipped }} 窗样本不足）</span>
          </td>
          <td>{{ formatPercent(candidate.consistency?.median_win_rate) }}</td>
          <td>{{ formatPercent(candidate.consistency?.worst_win_rate) }}</td>
          <td
            v-for="window in candidate.windows ?? []"
            :key="window.index"
            :class="{ positive: window.improved_win_rate === true, negative: window.improved_win_rate === false }"
            :title="rollingNote(candidate)"
          >
            <template v-if="window.insufficient_sample">
              <span class="muted-code" title="该窗口成交笔数不足，不判优劣">样本不足</span>
            </template>
            <template v-else>
              {{ formatPercent(window.win_rate) }}
              <small class="muted-code">基线 {{ formatPercent(window.baseline_win_rate ?? baselineWinRate(window.index)) }}</small>
            </template>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</div>
</template>
