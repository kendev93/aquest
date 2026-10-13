<script setup lang="ts">
/**
 * 排序规则推荐卡：把一次研究产出的 ranking 报告讲成「建议按什么排序、证据是什么」。
 *
 * 从 ConditionResearchPage 拆出来。只负责展示：排序配置的三个 ref
 * （rankingValue/Order/TopN）留在页面——它们同时被表单 payload、回填链和
 * 桥接按钮使用；「采用这个排序」通过 apply 事件交回页面处理。
 */
import { computed } from 'vue'

import { RANKING_CONFIDENCE_LABELS } from '../utils/conditionResearch'
import { formatNumber, formatPercent } from '../utils/format'
import TermHint from './TermHint.vue'
import type { ConditionResearchRanking } from '../types'

const props = defineProps<{ ranking: ConditionResearchRanking | null }>()

const emit = defineEmits<{ (event: 'apply'): void }>()

const recommendation = computed(() => props.ranking?.recommended ?? null)

const confidenceLabel = computed(
  () => RANKING_CONFIDENCE_LABELS[recommendation.value?.confidence ?? 'none'] ?? '暂无法判断',
)

const valueName = computed(
  () => recommendation.value?.display_name || recommendation.value?.value_id || '—',
)

const orderText = computed(() =>
  recommendation.value?.order === 'asc' ? '从小到大' : '从大到小',
)

const quantiles = computed(() =>
  (recommendation.value?.quantile_returns ?? []).map((item) => formatPercent(item)),
)
</script>

<template>
  <div v-if="ranking" class="ranking-advice" data-testid="condition-ranking-advice">
    <div class="ranking-advice-head">
      <strong>排序规则推荐</strong>
      <span class="ranking-confidence" data-testid="condition-ranking-confidence">{{ confidenceLabel }}</span>
    </div>
    <template v-if="recommendation">
      <p class="ranking-advice-line">
        建议按 <b>{{ valueName }}</b> {{ orderText }} 排序
        <button
          class="link-button"
          data-testid="condition-use-recommended-ranking"
          type="button"
          @click="emit('apply')"
        >采用这个排序</button>
      </p>
      <div class="ranking-evidence">
        <div>
          <span>平均信息系数<TermHint term="icMean" /></span>
          <strong>{{ formatNumber(recommendation.ic_mean, 3) }}</strong>
        </div>
        <div>
          <span>信息系数信息比率<TermHint term="icir" /></span>
          <strong>{{ formatNumber(recommendation.icir, 2) }}</strong>
        </div>
        <div>
          <span>分段同号<TermHint term="segment" /></span>
          <strong>{{ recommendation.same_sign_segments }} / {{ recommendation.segments }}</strong>
        </div>
        <div>
          <span>前 {{ ranking.top_n }} 名提升<TermHint term="topNLift" /></span>
          <strong>{{ formatPercent(recommendation.top_n_lift) }}</strong>
        </div>
      </div>
      <div v-if="quantiles.length" class="ranking-quantiles">
        <span>分层收益<TermHint term="quantile" /></span>
        <span v-for="(value, index) in quantiles" :key="index" class="ranking-quantile">
          <small>第 {{ index + 1 }} 档</small>{{ value }}
        </span>
      </div>
      <p class="ranking-note">{{ recommendation.note }}</p>
    </template>
    <p v-else class="ranking-note" data-testid="condition-ranking-reason">
      {{ ranking.reason ?? '这次研究没有形成可用的排序结论。' }}
    </p>
  </div>
</template>

<style scoped>
.ranking-advice {
  display: grid;
  gap: 10px;
  margin-top: 18px;
  padding: 14px 16px;
  border: 1px solid var(--border);
  border-left: 2px solid var(--accent);
  background: rgba(34, 197, 94, 0.05);
}

.ranking-advice-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.ranking-confidence {
  padding: 2px 8px;
  border: 1px solid var(--border-strong);
  border-radius: 999px;
  color: var(--muted);
  font-size: 11px;
}

.ranking-advice-line {
  margin: 0;
  color: var(--text);
  font-size: 14px;
}

.link-button {
  margin-left: 8px;
  padding: 0;
  border: 0;
  background: none;
  color: var(--accent);
  font-size: 13px;
  cursor: pointer;
  text-decoration: underline;
}

.ranking-evidence {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 10px;
}

.ranking-evidence > div {
  display: grid;
  gap: 3px;
}

.ranking-evidence span {
  color: var(--subtle);
  font-size: 11px;
}

.ranking-evidence strong {
  font-size: 15px;
  font-weight: 500;
}

.ranking-quantiles {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 10px;
  color: var(--subtle);
  font-size: 11px;
}

.ranking-quantile {
  color: var(--text);
  font-size: 13px;
}

.ranking-note {
  margin: 0;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.55;
}
</style>
