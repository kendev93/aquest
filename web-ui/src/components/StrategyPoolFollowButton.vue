<script setup lang="ts">
import { computed } from 'vue'

import { symbol } from '../utils/strategyPoolStatus'
import type { StrategyPoolFollowTarget } from '../types'

interface Props {
  target: StrategyPoolFollowTarget
  followKeys: Set<string>
  busy?: boolean
  testId?: string
}

const props = withDefaults(defineProps<Props>(), {
  busy: false,
  testId: '',
})

const emit = defineEmits<{
  toggle: [target: StrategyPoolFollowTarget]
}>()

const followed = computed(() => props.followKeys.has(symbol(props.target)))
const title = computed(() => `管理 ${props.target.market} ${props.target.code} 的关注分组`)
</script>

<template>
  <button
    class="text-button pool-follow-button"
    :data-testid="testId || `pool-follow-${target.code}`"
    :disabled="busy"
    :title="title"
    @click="emit('toggle', target)"
  >{{ followed ? '管理分组' : '添加到分组' }}</button>
</template>
