<script setup lang="ts">
import { computed } from 'vue'

import { TERMS } from '../utils/terms'

const props = defineProps<{ term: string }>()

const definition = computed(() => TERMS[props.term] ?? null)
</script>

<template>
  <span v-if="definition" class="term-hint">
    <button
      type="button"
      class="term-hint-mark"
      :data-testid="`term-hint-${term}`"
      :aria-label="`${definition.name}说明`"
    >?</button>
    <span class="term-hint-pop" role="tooltip">
      <span class="term-hint-name">{{ definition.name }}</span>
      <span class="term-hint-en">{{ definition.english }}</span>
      <span class="term-hint-intro">{{ definition.intro }}</span>
    </span>
  </span>
</template>

<style scoped>
.term-hint {
  position: relative;
  display: inline-flex;
  vertical-align: middle;
  margin-left: 4px;
}

.term-hint-mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 14px;
  height: 14px;
  padding: 0;
  border: 1px solid var(--border-strong);
  border-radius: 50%;
  background: transparent;
  color: var(--subtle);
  font-size: 10px;
  line-height: 1;
  cursor: help;
}

.term-hint-mark:hover {
  border-color: var(--accent);
  color: var(--text);
}

.term-hint-pop {
  position: absolute;
  bottom: calc(100% + 8px);
  left: 50%;
  z-index: 30;
  display: none;
  width: 268px;
  padding: 10px 12px;
  border: 1px solid var(--border-strong);
  border-radius: 8px;
  background: var(--surface-raised);
  box-shadow: 0 14px 32px rgba(0, 0, 0, 0.4);
  transform: translateX(-50%);
}

.term-hint:hover .term-hint-pop,
.term-hint:focus-within .term-hint-pop {
  display: grid;
  gap: 5px;
  text-align: left;
}

.term-hint-name {
  color: var(--text);
  font-size: 13px;
  font-weight: 500;
}

.term-hint-en {
  color: var(--subtle);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 10px;
  letter-spacing: 0.04em;
}

.term-hint-intro {
  color: var(--muted);
  font-size: 12px;
  line-height: 1.55;
}
</style>
