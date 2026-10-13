<script setup lang="ts">
import { computed, ref } from 'vue'

import HighlightText from '../components/HighlightText.vue'
import PageIntro from '../components/PageIntro.vue'
import { manualAnchor, searchManual } from '../utils/manual'
import AppTopbar from '../components/AppTopbar.vue'

const query = ref('')

const result = computed(() => searchManual(query.value))

const countText = computed(() => {
  const { querying, sections, total, hitLines } = result.value
  if (!querying) return `共 ${total} 个页面`
  return `匹配到 ${sections.length} / ${total} 个页面，${hitLines} 条内容`
})
</script>

<template>
  <div class="screen-page user-manual-page" data-testid="user-manual-page">
    <AppTopbar />

    <main class="content-shell">
      <PageIntro
        eyebrow="USER MANUAL"
        title="使用手册"
        note-label="为什么汇总成一页"
        guide-key="user-manual"
      >
        <template #copy>
          每个页面各自能回答什么、不能回答什么，以及每个数字背后的口径，都在这里。
          <strong>左边是目录，上面可以搜。</strong>
        </template>
        <p>
          口径散在各页时，「这个数到底怎么算的」只能靠翻。汇总之后可以搜索，
          而且内容与页内折叠说明、<code>docs/user-manual.md</code> 出自同一份数据源，不会说法不一致。
        </p>
      </PageIntro>

      <section class="manual-layout" data-testid="manual-layout">
        <aside class="manual-sidebar">
          <div class="manual-search">
            <input
              v-model="query"
              type="search"
              data-testid="manual-search"
              aria-label="搜索使用手册"
              placeholder="搜索，例如：口径 / 佣金 / MinTRL"
            >
            <button
              v-if="query"
              class="text-button"
              type="button"
              data-testid="manual-search-clear"
              @click="query = ''"
            >
              清空
            </button>
          </div>
          <p class="manual-count" data-testid="manual-count">{{ countText }}</p>
          <nav class="manual-toc" aria-label="手册目录" data-testid="manual-toc">
            <a
              v-for="section in result.sections"
              :key="section.key"
              class="manual-toc-link"
              :href="`#${manualAnchor(section.key)}`"
            >
              <span>{{ section.title }}</span>
              <span v-if="result.querying" class="manual-toc-hits">{{ section.hits }}</span>
            </a>
          </nav>
        </aside>

        <div class="manual-content">
          <article
            v-for="section in result.sections"
            :id="manualAnchor(section.key)"
            :key="section.key"
            class="manual-article"
            data-testid="manual-article"
          >
            <div class="manual-section-head">
              <h2><HighlightText :segments="section.titleSegments" /></h2>
              <a class="manual-open" :href="section.path">打开这一页</a>
            </div>
            <p class="manual-summary"><HighlightText :segments="section.summarySegments" /></p>

            <div class="manual-grid">
              <div
                v-for="group in section.groups.filter((item) => item.tone !== 'caliber')"
                :key="group.label"
                class="manual-block"
                :class="{ limits: group.tone === 'limits' }"
              >
                <p class="manual-label">{{ group.label }}</p>
                <ul v-if="!group.ordered" class="manual-list">
                  <li v-for="line in group.lines" :key="line.text">
                    <HighlightText :segments="line.segments" />
                  </li>
                </ul>
                <ol v-else class="manual-list ordered">
                  <li v-for="line in group.lines" :key="line.text">
                    <HighlightText :segments="line.segments" />
                  </li>
                </ol>
              </div>
            </div>

            <div
              v-for="group in section.groups.filter((item) => item.tone === 'caliber')"
              :key="group.label"
              class="manual-block caliber"
            >
              <p class="manual-label">{{ group.label }}</p>
              <ul class="manual-list">
                <li v-for="line in group.lines" :key="line.text">
                  <HighlightText :segments="line.segments" />
                </li>
              </ul>
            </div>

            <p v-if="section.next.length" class="manual-next">
              下一步：
              <a v-for="item in section.next" :key="item.path" class="manual-next-link" :href="item.path">
                {{ item.label }}
              </a>
            </p>
          </article>

          <p v-if="!result.sections.length" class="manual-empty" data-testid="manual-empty">
            没有匹配的内容。搜索是字面匹配，换个更短的词试试。
          </p>
        </div>
      </section>
    </main>
  </div>
</template>
