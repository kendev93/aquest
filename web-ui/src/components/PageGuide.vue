<script setup lang="ts">
import { computed, ref } from 'vue'

import pageGuides from '../data/pageGuides.json'

const props = defineProps<{ pageKey: string }>()

// 展开状态按页面分别记住：老用户不想每次进来都看到同一段说明，
// 新用户第一次点开之后也希望它保持展开。localStorage 在隐私模式下会抛错，
// 记不住状态不算故障，退化成「每次默认收起」即可。
const STORAGE_PREFIX = 'aquest.guide-expanded.'

function readStoredState(): boolean {
  try {
    return window.localStorage.getItem(STORAGE_PREFIX + props.pageKey) === '1'
  } catch {
    return false
  }
}

const expanded = ref(readStoredState())

const guide = computed(
  () => pageGuides.guides.find((item) => item.key === props.pageKey) ?? null,
)

function toggle(): void {
  expanded.value = !expanded.value
  try {
    window.localStorage.setItem(STORAGE_PREFIX + props.pageKey, expanded.value ? '1' : '0')
  } catch {
    // 存不下就算了，不影响使用
  }
}
</script>

<template>
  <section v-if="guide" class="page-guide" :data-testid="`page-guide-${pageKey}`">
    <button
      type="button"
      class="page-guide-toggle"
      :aria-expanded="expanded"
      :data-testid="`page-guide-toggle-${pageKey}`"
      @click="toggle"
    >
      <span class="page-guide-label">这个功能怎么用</span>
      <span class="page-guide-summary">{{ guide.summary }}</span>
      <span class="page-guide-action">{{ expanded ? '收起' : '展开' }}</span>
    </button>

    <div v-if="expanded" class="page-guide-body" :data-testid="`page-guide-body-${pageKey}`">
      <div class="guide-columns">
        <div class="guide-block">
          <p class="guide-heading">什么时候用它</p>
          <ul class="guide-list">
            <li v-for="item in guide.useCases" :key="item">{{ item }}</li>
          </ul>
        </div>

        <div class="guide-block">
          <p class="guide-heading">操作步骤</p>
          <ol class="guide-list ordered">
            <li v-for="item in guide.steps" :key="item">{{ item }}</li>
          </ol>
        </div>

        <div class="guide-block">
          <p class="guide-heading">它能回答什么</p>
          <ul class="guide-list">
            <li v-for="item in guide.answers" :key="item">{{ item }}</li>
          </ul>
        </div>

        <div class="guide-block limits">
          <p class="guide-heading">它不能回答什么</p>
          <ul class="guide-list">
            <li v-for="item in guide.limits" :key="item">{{ item }}</li>
          </ul>
        </div>
      </div>

      <div class="guide-block caliber">
        <p class="guide-heading">口径提醒（决定了结论能不能信）</p>
        <ul class="guide-list">
          <li v-for="item in guide.caliber" :key="item">{{ item }}</li>
        </ul>
      </div>

      <p v-if="guide.next.length" class="guide-next">
        下一步：
        <a v-for="item in guide.next" :key="item.path" class="guide-next-link" :href="item.path">
          {{ item.label }}
        </a>
      </p>
    </div>
  </section>
</template>

<style scoped>
.page-guide {
  margin-bottom: 26px;
  border: 1px solid var(--border);
  border-left: 2px solid var(--accent);
  background: rgba(11, 18, 35, 0.72);
}

.page-guide-toggle {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: 14px;
  width: 100%;
  padding: 13px 18px;
  border: 0;
  background: transparent;
  color: var(--text);
  cursor: pointer;
  text-align: left;
}

.page-guide-toggle:hover {
  background: var(--surface-hover);
}

.page-guide-label {
  color: var(--accent);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.15em;
  white-space: nowrap;
}

.page-guide-summary {
  color: var(--muted);
  font-size: 13px;
  line-height: 1.5;
}

.page-guide-action {
  color: var(--subtle);
  font-size: 12px;
  white-space: nowrap;
}

.page-guide-body {
  padding: 4px 18px 18px;
  border-top: 1px solid var(--border);
}

.guide-columns {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px 30px;
  padding-top: 16px;
}

.guide-block {
  min-width: 0;
}

.guide-block.caliber {
  margin-top: 16px;
  padding: 13px 15px;
  border-left: 2px solid var(--warning);
  background: rgba(245, 158, 11, 0.07);
}

.guide-heading {
  margin: 0 0 7px;
  color: var(--text);
  font-size: 12px;
  font-weight: 700;
}

.guide-block.limits .guide-heading {
  color: var(--warning);
}

.guide-list {
  margin: 0;
  padding-left: 17px;
  color: var(--muted);
  font-size: 12.5px;
  line-height: 1.65;
}

.guide-list.ordered {
  padding-left: 19px;
}

.guide-next {
  margin: 15px 0 0;
  color: var(--subtle);
  font-size: 12px;
}

.guide-next-link {
  color: var(--accent);
  text-decoration: none;
  margin-right: 16px;
}

.guide-next-link:hover {
  text-decoration: underline;
}

@media (max-width: 900px) {
  .guide-columns {
    grid-template-columns: minmax(0, 1fr);
  }

  .page-guide-summary {
    display: none;
  }
}
</style>
