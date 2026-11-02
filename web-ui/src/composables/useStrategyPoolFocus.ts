import { computed, ref, type Ref } from 'vue'

import {
  addStrategyPoolFocus,
  fetchStrategyPoolFocus,
  removeStrategyPoolFocus,
} from '../api/strategyPool'
import type {
  StrategyPoolEntry,
  StrategyPoolFocusItem,
  StrategyPoolFollowTarget,
  StrategyPoolObjective,
  StrategyPoolSignal,
} from '../types'
import { sortPoolEntriesByStatus, symbol } from '../utils/strategyPoolStatus'

interface StrategyPoolFocusOptions {
  entries: Ref<StrategyPoolEntry[]>
  signalStates: Ref<StrategyPoolSignal[]>
  heldSymbols: Ref<Set<string>>
  objective: Ref<StrategyPoolObjective | null>
  errorMessage: Ref<string>
  describeError: (error: unknown) => string
}

/** 特别关注名单：读一次，本地先行更新，失败回滚。 */
export function useStrategyPoolFocus(options: StrategyPoolFocusOptions) {
  const focusItems = ref<StrategyPoolFocusItem[]>([])
  const focusPending = ref(false)
  const followKeys = computed(() => new Set(focusItems.value.map(symbol)))
  const focusRows = computed(() =>
    sortPoolEntriesByStatus(
      options.entries.value.filter((entry) => followKeys.value.has(symbol(entry))),
      options.signalStates.value,
      options.heldSymbols.value,
      options.objective.value,
    ),
  )
  const focusMissing = computed(() => {
    const poolKeys = new Set(options.entries.value.map(symbol))
    return focusItems.value.filter((item) => !poolKeys.has(symbol(item)))
  })
  const focusMissingCount = computed(() => focusMissing.value.length)

  async function toggleFollow(target: StrategyPoolFollowTarget): Promise<void> {
    if (focusPending.value) return
    const key = symbol(target)
    const name = target.name ?? ''
    const followed = followKeys.value.has(key)
    const previous = focusItems.value
    focusPending.value = true
    // 先按本地更新让面板跟手，再用服务端返回的权威名单覆盖；被拒（例如超过上限）就整表回滚。
    focusItems.value = followed
      ? previous.filter((item) => symbol(item) !== key)
      : [...previous, { market: target.market, code: target.code, name }]
    options.errorMessage.value = ''
    try {
      focusItems.value = followed
        ? await removeStrategyPoolFocus(target.market, target.code)
        : await addStrategyPoolFocus(target.market, target.code, name)
    } catch (error) {
      focusItems.value = previous
      options.errorMessage.value = options.describeError(error)
    } finally {
      focusPending.value = false
    }
  }

  // 池外的关注标的在两张表里都没有行，也就没有单只取消的入口；这里给一个批量移出的出口，
  // 逐只走同一个 DELETE 接口，避免为了「清理」再加一套后端语义。
  async function pruneMissingFocus(): Promise<void> {
    if (focusPending.value) return
    const missing = focusMissing.value
    if (!missing.length) return
    const previous = focusItems.value
    focusPending.value = true
    options.errorMessage.value = ''
    try {
      let items = previous
      for (const item of missing) {
        items = await removeStrategyPoolFocus(item.market, item.code)
      }
      focusItems.value = items
    } catch (error) {
      // 中途失败时后端可能已经移除了几只，重读一次让界面和库里一致。
      focusItems.value = await fetchStrategyPoolFocus().catch(() => previous)
      options.errorMessage.value = options.describeError(error)
    } finally {
      focusPending.value = false
    }
  }

  async function loadFocus(): Promise<void> {
    try {
      focusItems.value = await fetchStrategyPoolFocus()
    } catch {
      // 读不到关注名单就当作一只都没关注，不打断页面初始化。
    }
  }

  return {
    focusItems,
    focusPending,
    followKeys,
    focusRows,
    focusMissingCount,
    toggleFollow,
    pruneMissingFocus,
    loadFocus,
  }
}
