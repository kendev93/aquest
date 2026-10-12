<script setup lang="ts">
// 命中的片段裹 <mark>，其余原样输出。不用 v-html：内容虽然只来自仓库里的 JSON，
// 但一旦以后允许外部文案进来，v-html 就是个现成的注入口子。
// 标签之间不留空白，否则相邻片段之间会多出一个空格。
import type { ManualSegment } from '../types'

defineProps<{ segments: ManualSegment[] }>()
</script>

<template>
  <template v-for="(segment, index) in segments" :key="index"><mark
    v-if="segment.hit"
    data-testid="manual-hit"
  >{{ segment.text }}</mark><template v-else>{{ segment.text }}</template></template>
</template>
