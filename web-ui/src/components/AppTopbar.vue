<script setup lang="ts">
/**
 * 顶栏：站标、主导航、本地数据标识。
 *
 * 以前这 24 行（含 16 项导航链接）在每个页面里各写了一份，加一个页面要把所有
 * 页面都改一遍。现在结构只此一份，导航项取自 `router.ts` 的 NAV_ITEMS，
 * 当前点亮哪一项由浏览器地址推导，页面自身不再参与。
 */
import { computed } from 'vue'

import { NAV_ITEMS, activeNavPath, currentPath } from '../router'

const activePath = computed(() => activeNavPath(currentPath.value))
</script>

<template>
  <header class="topbar">
    <a class="brand" href="/aquest" aria-label="A股策略研究首页">
      <img class="brand-mark" src="/aquest-mark.png" alt="" aria-hidden="true" width="34" height="34">
      <span><strong>AQuest</strong><small>A股策略研究</small></span>
    </a>
    <nav aria-label="主导航">
      <a
        v-for="item in NAV_ITEMS"
        :key="item.path"
        class="nav-link"
        :class="{ active: item.path === activePath }"
        :href="item.path"
        :aria-current="item.path === activePath ? 'page' : undefined"
      >{{ item.label }}</a>
    </nav>
    <div class="topbar-status"><span class="status-dot" aria-hidden="true"></span> 本地数据模式</div>
  </header>
</template>
