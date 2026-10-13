<script setup lang="ts">
/**
 * 页面标题区：eyebrow + h1 + 一句话说明，右侧一条口径提示，下面接本页的功能说明。
 *
 * 16 个页面这块结构一字不差，只差文案，所以写一遍。短文案走 props；
 * 文案里要嵌 TermHint / strong / code 时用同名插槽覆盖（插槽内容优先）。
 * 默认插槽就是右栏那条强调行（strong + small 或一个 p），
 * 没有子内容也没有 noteLabel 时整个右栏不渲染。
 */
import PageGuide from './PageGuide.vue'

defineProps<{
  eyebrow: string
  title?: string
  copy?: string
  noteLabel?: string
  guideKey: string
}>()
</script>

<template>
  <section class="page-intro">
    <div>
      <p class="eyebrow">{{ eyebrow }}</p>
      <h1><slot name="title">{{ title }}</slot></h1>
      <p v-if="copy || $slots.copy" class="intro-copy"><slot name="copy">{{ copy }}</slot></p>
    </div>
    <div v-if="noteLabel || $slots.default" class="intro-note">
      <span v-if="noteLabel" class="note-label">{{ noteLabel }}</span>
      <slot />
    </div>
  </section>

  <PageGuide :page-key="guideKey" />
</template>
