<script setup lang="ts">
/**
 * 应用外壳：拦住站内链接做无刷新切换，其余交给 `router.ts` 决定渲染什么。
 *
 * KeepAlive 是刻意的：切走再切回来时表单里的配置还在，不然调了半天的参数会因为
 * 点了一次导航而清空。代价是页面实例不会重新创建，onMounted 里的初始化不会重跑
 * ——需要「每次进页面都刷新」的东西不能只写在 onMounted 里。
 */
import { onMounted } from 'vue'

import { currentRoute, currentRouteKey, navigateTo, syncPath } from './router'

function handlePopState(): void {
  syncPath()
}

function handleNavigation(event: MouseEvent): void {
  if (
    event.defaultPrevented
    || event.button !== 0
    || event.metaKey
    || event.ctrlKey
    || event.shiftKey
    || event.altKey
  ) {
    return
  }
  const target = event.target
  if (!(target instanceof Element)) return
  const link = target.closest('a[href]')
  if (!(link instanceof HTMLAnchorElement) || link.target === '_blank' || link.hasAttribute('download')) {
    return
  }
  const href = link.getAttribute('href')
  if (!href || !href.startsWith('/') || href.startsWith('//')) return
  const url = new URL(href, window.location.origin)
  if (url.origin !== window.location.origin) return
  event.preventDefault()
  navigateTo(`${url.pathname}${url.search}${url.hash}`)
}

onMounted(() => {
  // 刷新或首次进入时，地址栏才是唯一真源。
  syncPath()
  window.addEventListener('popstate', handlePopState)
})
</script>

<template>
  <div class="app-shell" @click.capture="handleNavigation">
    <KeepAlive>
      <component :is="currentRoute.component" :key="currentRouteKey" />
    </KeepAlive>
  </div>
</template>
