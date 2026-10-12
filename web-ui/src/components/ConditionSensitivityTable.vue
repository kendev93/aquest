<script setup lang="ts">
import { computed } from 'vue'

import {
  formatConditionText,
  retentionClass,
  retentionText,
  sensitivityTitle,
} from '../utils/conditionResearch'
import type {
  ConditionResearchCandidate,
  ConditionResearchSensitivity,
  ValueDefinition,
} from '../types'
import TermHint from './TermHint.vue'

interface SensitivityRow {
  candidate: ConditionResearchCandidate
  sensitivity: ConditionResearchSensitivity
}

const props = defineProps<{
  candidates: ConditionResearchCandidate[]
  values: ValueDefinition[]
}>()

/**
 * 参数敏感性：把每个候选的阈值往松、往紧各挪两档重新计分。
 * 只挑出真的算出了邻域的候选——交叉条件之类没有可挪的标量参数，后端给 null。
 */
const rows = computed<SensitivityRow[]>(() => {
  const mapped = props.candidates.map((candidate) => ({
    candidate,
    sensitivity: candidate.sensitivity,
  }))
  return mapped.filter((row): row is SensitivityRow => row.sensitivity != null)
})

function conditionText(candidate: ConditionResearchCandidate): string {
  return formatConditionText(props.values, candidate)
}
</script>

<template>
<section v-if="rows.length" class="condition-sensitivity">
  <div class="section-heading">
    <div>
      <span class="section-kicker">PARAMETER SENSITIVITY</span>
      <h3>参数敏感性<TermHint term="sensitivity" /></h3>
    </div>
  </div>
  <p class="helper">
    把每个候选的阈值往松、往紧各挪两档（分位 ±5% / ±10%）重新计分，看优势还在不在：
    <strong>保持率</strong> 1.00 是一点没掉，0.50 是掉了一半，负数表示优势打反了。
    邻域都在是台地，只有基这一个点好用是悬崖——后者是过拟合最典型的形状。
  </p>
  <p class="scope-helper">
    邻域是在挖掘样本（寻优期）上重算的，回答的是「这个切点是不是噪声尖峰」，
    <strong>不代表样本外表现</strong>；样本外由验证期、留出期和滚动复验负责。
  </p>
  <div class="table-wrap">
    <table>
      <caption class="sr-only">参数敏感性邻域</caption>
      <thead>
        <tr>
          <th>候选</th>
          <th>更宽松 −10%</th>
          <th>−5%</th>
          <th>基点<TermHint term="sensitivityRetention" /></th>
          <th>+5%</th>
          <th>更严格 +10%</th>
          <th>仍有提升</th>
          <th>中位保持率</th>
          <th>最差保持率</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="row in rows"
          :key="row.candidate.candidate_id"
          :data-testid="'condition-sensitivity-' + row.candidate.candidate_id"
        >
          <td class="condition-rule">{{ conditionText(row.candidate) }}</td>
          <td
            v-for="point in row.sensitivity.points"
            :key="point.offset"
            class="sensitivity-cell"
            :class="retentionClass(point)"
            :title="sensitivityTitle(point)"
            :data-testid="'sensitivity-point-' + row.candidate.candidate_id + '-' + point.offset"
          >
            <template v-if="point.insufficient || point.degenerate">
              <span class="muted-code">{{ point.degenerate ? '恒真' : '样本不足' }}</span>
            </template>
            <template v-else>{{ retentionText(point.retention) }}</template>
          </td>
          <td :data-testid="'sensitivity-stable-' + row.candidate.candidate_id">
            {{ row.sensitivity.stable_offsets }}/{{ row.sensitivity.evaluated_offsets }}
            <span v-if="row.sensitivity.skipped > 0" class="muted-code">（{{ row.sensitivity.skipped }} 档未计入）</span>
          </td>
          <td>{{ retentionText(row.sensitivity.median_retention) }}</td>
          <td>{{ retentionText(row.sensitivity.worst_retention) }}</td>
        </tr>
      </tbody>
    </table>
  </div>
</section>
</template>
