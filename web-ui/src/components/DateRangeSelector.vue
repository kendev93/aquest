<script setup lang="ts">
import { computed } from 'vue'

import type { DateRangeMode, DateRangeSelection } from '../types'

const props = withDefaults(defineProps<{
  modelValue: DateRangeSelection
  prefix: string
  testid: string
  title?: string
  helper?: string
  fieldSuffix?: string
}>(), {
  title: '历史区间',
  helper: '默认使用本地最新已完成日线向前 5 年；选择全历史或自定义日期可覆盖。',
  fieldSuffix: '',
})

const emit = defineEmits<{
  'update:modelValue': [value: DateRangeSelection]
}>()

const modeId = computed(() => `${props.prefix}-date-mode`)
const startId = computed(() => `${props.prefix}-start${props.fieldSuffix}`)
const endId = computed(() => `${props.prefix}-end${props.fieldSuffix}`)

function updateMode(mode: DateRangeMode): void {
  emit('update:modelValue', {
    mode,
    startDate: mode === 'custom' ? props.modelValue.startDate : '',
    endDate: mode === 'custom' ? props.modelValue.endDate : '',
  })
}

function updateDate(field: 'startDate' | 'endDate', value: string): void {
  emit('update:modelValue', { ...props.modelValue, [field]: value })
}
</script>

<template>
  <fieldset class="form-section date-range-selector" :data-testid="testid">
    <legend>{{ title }}</legend>
    <div class="compact-fields">
      <div>
        <label :for="modeId">回测区间</label>
        <select
          :id="modeId"
          :value="modelValue.mode"
          :data-testid="modeId"
          @change="updateMode(($event.target as HTMLSelectElement).value as DateRangeMode)"
        >
          <option value="recent5">最近 5 年（默认）</option>
          <option value="full">全历史</option>
          <option value="custom">自定义日期</option>
        </select>
      </div>
    </div>
    <div v-if="modelValue.mode === 'custom'" class="compact-fields">
      <div>
        <label :for="startId">开始日期</label>
        <input
          :id="startId"
          :value="modelValue.startDate"
          :data-testid="startId"
          type="date"
          @input="updateDate('startDate', ($event.target as HTMLInputElement).value)"
        >
      </div>
      <div>
        <label :for="endId">结束日期</label>
        <input
          :id="endId"
          :value="modelValue.endDate"
          :data-testid="endId"
          type="date"
          @input="updateDate('endDate', ($event.target as HTMLInputElement).value)"
        >
      </div>
    </div>
    <p class="helper">{{ helper }}</p>
  </fieldset>
</template>
