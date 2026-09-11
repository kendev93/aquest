<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import type {
  CombineMode,
  ConditionOperator,
  ConditionOperatorDefinition,
  RuleGroupPayload,
  ScreenCondition,
  ValueDefinition,
} from '../types'
import { valueOptionLabel } from '../utils/formulaScreen'

const props = withDefaults(defineProps<{
  modelValue: RuleGroupPayload
  values: ValueDefinition[]
  conditionOperators?: ConditionOperatorDefinition[]
  title: string
  testid: string
  fixed?: boolean
  maxConditions?: number
}>(), {
  conditionOperators: () => [],
  fixed: false,
  maxConditions: 32,
})

const emit = defineEmits<{
  'update:modelValue': [value: RuleGroupPayload]
}>()

const defaultOperators: ConditionOperatorDefinition[] = [
  { value: 'gt', label: '大于' },
  { value: 'gte', label: '大于等于' },
  { value: 'lt', label: '小于' },
  { value: 'lte', label: '小于等于' },
  { value: 'eq', label: '等于' },
  { value: 'neq', label: '不等于' },
  { value: 'cross_up', label: '上穿' },
  { value: 'cross_down', label: '下穿' },
]

function cloneCondition(condition: ScreenCondition): ScreenCondition {
  return { ...condition }
}

function cloneRule(rule: RuleGroupPayload): RuleGroupPayload {
  return {
    signals: [],
    conditions: rule.conditions.map(cloneCondition),
    combine_mode: props.fixed ? 'all' : rule.combine_mode,
    minimum_matches: props.fixed ? null : rule.minimum_matches,
  }
}

const draft = ref<RuleGroupPayload>(cloneRule(props.modelValue))
const operators = computed(() => props.conditionOperators.length ? props.conditionOperators : defaultOperators)
const itemCount = computed(() => draft.value.conditions.length)
let syncingFromParent = false

watch(() => props.modelValue, (value) => {
  syncingFromParent = true
  draft.value = cloneRule(value)
}, { deep: true })

watch(draft, (value) => {
  if (syncingFromParent) {
    syncingFromParent = false
    return
  }
  emit('update:modelValue', cloneRule(value))
}, { deep: true })

function updateCombineMode(value: CombineMode): void {
  if (props.fixed) return
  draft.value.combine_mode = value
  if (value === 'at_least') {
    draft.value.minimum_matches = Math.min(Math.max(draft.value.minimum_matches ?? 1, 1), Math.max(itemCount.value, 1))
  } else {
    draft.value.minimum_matches = null
  }
}

function emptyCondition(): ScreenCondition {
  const left = props.values[0]?.id ?? ''
  const right = props.values.find((value) => value.id !== left)?.id ?? left
  return {
    left_value: left,
    operator: operators.value[0]?.value ?? 'gt',
    right_value: right || null,
    right_constant: right ? null : 0,
  }
}

function addCondition(): void {
  if (draft.value.conditions.length >= props.maxConditions) return
  draft.value.signals = []
  draft.value.conditions = [...draft.value.conditions, emptyCondition()]
  normalizeMinimum()
}

function removeCondition(index: number): void {
  draft.value.conditions = draft.value.conditions.filter((_, current) => current !== index)
  normalizeMinimum()
}

function updateCondition(index: number, patch: Partial<ScreenCondition>): void {
  draft.value.signals = []
  draft.value.conditions = draft.value.conditions.map((condition, current) => (
    current === index ? { ...condition, ...patch } : condition
  ))
}

function setRightType(index: number, type: 'value' | 'constant'): void {
  const condition = draft.value.conditions[index]
  if (!condition) return
  if (type === 'constant') {
    updateCondition(index, { right_value: null, right_constant: condition.right_constant ?? 0 })
    return
  }
  const fallback = props.values.find((value) => value.id !== condition.left_value)?.id
    ?? props.values[0]?.id
    ?? ''
  updateCondition(index, { right_value: condition.right_value ?? fallback, right_constant: null })
}

function normalizeMinimum(): void {
  if (draft.value.combine_mode !== 'at_least') return
  draft.value.minimum_matches = Math.min(
    Math.max(draft.value.minimum_matches ?? 1, 1),
    Math.max(itemCount.value, 1),
  )
}

function valueLabel(valueId: string): string {
  const value = props.values.find((candidate) => candidate.id === valueId)
  return value ? valueOptionLabel(value) : valueId
}
</script>

<template>
  <fieldset class="form-section rule-group-editor" :data-testid="testid">
    <legend>{{ title }} <span class="legend-count">{{ itemCount }} 个条件项</span></legend>

    <div v-if="!fixed" class="condition-combine-control">
      <div class="condition-combine-field">
        <label :for="`${testid}-combine-mode`">条件关系</label>
        <select :id="`${testid}-combine-mode`" :value="draft.combine_mode" :data-testid="`${testid}-combine-mode`" @change="updateCombineMode(($event.target as HTMLSelectElement).value as CombineMode)">
          <option value="all">AND · 全部满足</option>
          <option value="any">OR · 任一满足</option>
          <option value="at_least">至少满足 N 个</option>
        </select>
      </div>
      <div v-if="draft.combine_mode === 'at_least'" class="condition-combine-field">
        <label :for="`${testid}-minimum-matches`">至少满足数量</label>
        <input :id="`${testid}-minimum-matches`" v-model.number="draft.minimum_matches" :data-testid="`${testid}-minimum-matches`" type="number" min="1" :max="Math.max(itemCount, 1)" @change="normalizeMinimum">
      </div>
      <small class="condition-combine-helper">{{ draft.combine_mode === 'all' ? '所有指标条件同时满足才触发。' : draft.combine_mode === 'any' ? '任一指标条件满足就触发。' : `至少满足 ${draft.minimum_matches ?? 1} 个指标条件才触发。` }}</small>
    </div>
    <div v-else class="condition-combine-control fixed-rule-note fixed-rule-note--compact">每条条件都会作为硬约束，候选必须全部满足。</div>

    <div class="rule-condition-list">
      <span class="rule-subtitle">指标条件</span>
      <p class="helper">这里只使用指标输出：可以与另一个指标比较，也可以与固定数值比较。</p>
      <div v-for="(condition, index) in draft.conditions" :key="`${testid}-condition-${index}`" class="condition-row" :data-testid="`${testid}-condition-${index}`">
        <div class="condition-field condition-left">
          <label :for="`${testid}-left-${index}`">左侧指标</label>
          <select :id="`${testid}-left-${index}`" :value="condition.left_value" @change="updateCondition(index, { left_value: ($event.target as HTMLSelectElement).value })">
            <option v-for="value in values" :key="value.id" :value="value.id">{{ valueLabel(value.id) }}</option>
          </select>
        </div>
        <div class="condition-field condition-operator">
          <label :for="`${testid}-operator-${index}`">关系</label>
          <select :id="`${testid}-operator-${index}`" :value="condition.operator" @change="updateCondition(index, { operator: ($event.target as HTMLSelectElement).value as ConditionOperator })">
            <option v-for="operator in operators" :key="operator.value" :value="operator.value">{{ operator.label }}</option>
          </select>
        </div>
        <div class="condition-field condition-right-type">
          <label :for="`${testid}-right-type-${index}`">右侧类型</label>
          <select :id="`${testid}-right-type-${index}`" :value="condition.right_value === null ? 'constant' : 'value'" @change="setRightType(index, ($event.target as HTMLSelectElement).value as 'value' | 'constant')">
            <option value="value">另一个指标</option>
            <option value="constant">固定数值</option>
          </select>
        </div>
        <div class="condition-field condition-right">
          <label :for="`${testid}-right-${index}`">{{ condition.right_value === null ? '数值' : '右侧指标' }}</label>
          <input v-if="condition.right_value === null" :id="`${testid}-right-${index}`" :value="condition.right_constant ?? 0" type="number" step="any" @input="updateCondition(index, { right_constant: Number(($event.target as HTMLInputElement).value) })">
          <select v-else :id="`${testid}-right-${index}`" :value="condition.right_value" @change="updateCondition(index, { right_value: ($event.target as HTMLSelectElement).value })">
            <option v-for="value in values" :key="value.id" :value="value.id">{{ valueLabel(value.id) }}</option>
          </select>
        </div>
        <button class="condition-remove text-button" type="button" :data-testid="`${testid}-remove-${index}`" @click="removeCondition(index)">删除</button>
      </div>
      <p v-if="!draft.conditions.length" class="helper">还没有指标条件，可点击下方按钮添加。</p>
      <button class="secondary-button add-condition-button" type="button" :data-testid="`${testid}-add-condition`" :disabled="draft.conditions.length >= maxConditions" @click="addCondition">＋ 添加指标条件</button>
      <small v-if="draft.conditions.length >= maxConditions" class="scope-helper">已达到 {{ maxConditions }} 条条件上限。</small>
    </div>
  </fieldset>
</template>

<style scoped>
.rule-group-editor {
  display: grid;
  gap: 1rem;
  width: 100%;
  min-width: 0;
}

.rule-subtitle {
  display: block;
  margin-bottom: 0.55rem;
  color: var(--text-muted);
  font-size: 0.78rem;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.condition-row {
  display: grid;
  align-items: end;
  gap: 0.65rem;
  grid-template-columns: minmax(0, 1.15fr) minmax(118px, 0.85fr);
  grid-template-areas:
    "left left"
    "operator right-type"
    "right right"
    "remove remove";
  width: 100%;
  min-width: 0;
  margin-bottom: 0.7rem;
}

.condition-field {
  min-width: 0;
}

.rule-condition-list {
  width: 100%;
  min-width: 0;
}

.condition-remove {
  padding-bottom: 0.7rem;
}

.add-condition-button {
  width: 100%;
}

.fixed-rule-note {
  color: var(--subtle);
  font-size: 0.7rem;
  font-weight: 500;
  line-height: 1.45;
  text-wrap: pretty;
}

.fixed-rule-note--compact {
  min-height: 0;
}

@media (max-width: 900px) {
  .condition-remove {
    padding-bottom: 0;
  }
}
</style>
