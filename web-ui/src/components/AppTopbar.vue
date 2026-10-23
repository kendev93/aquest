<script setup lang="ts">
/**
 * 顶栏：站标、主导航、本地数据标识、上游新版本提示。
 *
 * 以前这 24 行（含 16 项导航链接）在每个页面里各写了一份，加一个页面要把所有
 * 页面都改一遍。现在结构只此一份，导航项取自 `router.ts` 的 NAV_ITEMS，
 * 当前点亮哪一项由浏览器地址推导，页面自身不再参与。
 *
 * 版本提示读的是后端每天查一次的发布状态：只告诉用户「上游有更新的版本」，
 * 不代他升级——重启会把在跑的全市场任务标失败、行情库又是独占写、schema 变更
 * 还得看发布说明，那几步该由人按下。
 */
import { computed, onMounted, ref } from 'vue'

import { fetchReleaseStatus } from '../api/release'
import { NAV_ITEMS, activeNavPath, currentPath } from '../router'
import type { ReleaseStatus } from '../types'

/** 已经提示过哪个 tag：同版本不再重复提醒，出现更新的 tag 时提示自然回来。 */
const RELEASE_DISMISS_KEY = 'aquest.release-dismissed-tag'

const activePath = computed(() => activeNavPath(currentPath.value))
const release = ref<ReleaseStatus | null>(null)
const dismissedTag = ref(readDismissedTag())

function readDismissedTag(): string {
  try {
    return window.localStorage.getItem(RELEASE_DISMISS_KEY) ?? ''
  } catch {
    // 隐私模式下写不进去：本次会话内安静即可，不该为此抛错。
    return ''
  }
}

const releaseNotice = computed(() => {
  const status = release.value
  if (!status || !status.update_available || status.latest === null) return null
  return status.latest.tag === dismissedTag.value ? null : status.latest
})

const releaseNoticeTitle = computed(() => {
  const status = release.value
  const latest = releaseNotice.value
  if (!status || !latest) return ''
  const published = latest.published_at
    ? new Date(latest.published_at).toLocaleDateString('zh-CN')
    : '日期未知'
  return `上游已发布 ${latest.tag}（${published}），本机在跑 ${status.current}。`
    + '发布页写了这一版改了什么。升级要你自己执行 docker compose pull 再 docker compose up -d：'
    + '重启会让在跑的全市场任务失败，所以这里不代劳。'
})

function dismissRelease(): void {
  const latest = releaseNotice.value
  if (!latest) return
  dismissedTag.value = latest.tag
  try {
    window.localStorage.setItem(RELEASE_DISMISS_KEY, latest.tag)
  } catch {
    // 存不下也已经在内存里关掉了，下次出现更新的版本再提醒。
  }
}

onMounted(async () => {
  // 读不到就当没有：出网失败、被限流、离线部署都不该在顶栏点亮一条每天重复的错误。
  try {
    release.value = await fetchReleaseStatus()
  } catch {
    release.value = null
  }
})
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
    <div class="topbar-status">
      <template v-if="releaseNotice">
        <a
          class="release-notice"
          data-testid="release-notice"
          :href="releaseNotice.url"
          target="_blank"
          rel="noopener"
          :title="releaseNoticeTitle"
        >新版本 {{ releaseNotice.tag }} 可升级</a>
        <button
          class="release-dismiss"
          data-testid="release-dismiss"
          type="button"
          aria-label="这个版本不再提醒"
          @click="dismissRelease"
        >×</button>
      </template>
      <span class="status-dot" aria-hidden="true"></span> 本地数据模式
    </div>
  </header>
</template>
