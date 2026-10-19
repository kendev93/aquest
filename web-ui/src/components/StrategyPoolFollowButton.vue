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
const title = computed(() => (followed.value
  ? `取消关注 ${props.target.market} ${props.target.code}`
  : `关注 ${props.target.market} ${props.target.code}，加入特别关注面板`))
</script>

<template>
  <button
    class="text-button pool-follow-button"
    :data-testid="testId || `pool-follow-${target.code}`"
    :disabled="busy"
    :title="title"
    @click="emit('toggle', target)"
  >{{ followed ? '取消关注' : '关注' }}</button>
</template>
