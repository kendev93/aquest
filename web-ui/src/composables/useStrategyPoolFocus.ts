import { computed, ref, type Ref } from 'vue'
import { createFocusGroup, deleteFocusGroup, fetchFocusGroups, renameFocusGroup, setFocusMembership, type StrategyPoolFocusGroup } from '../api/strategyPool'
import type { StrategyPoolEntry, StrategyPoolFocusItem, StrategyPoolFollowTarget, StrategyPoolObjective, StrategyPoolSignal } from '../types'
import { sortPoolEntriesByStatus, symbol } from '../utils/strategyPoolStatus'

interface StrategyPoolFocusOptions {
  entries: Ref<StrategyPoolEntry[]>
  signalStates: Ref<StrategyPoolSignal[]>
  heldSymbols: Ref<Set<string>>
  objective: Ref<StrategyPoolObjective | null>
  errorMessage: Ref<string>
  describeError: (error: unknown) => string
}

export function useStrategyPoolFocus(options: StrategyPoolFocusOptions) {
  const groups = ref<StrategyPoolFocusGroup[]>([{ id: 'default', name: '默认分组', items: [] }])
  const activeGroupId = ref('default')
  const focusPending = ref(true)
  const focusLoadError = ref(false)
  const selectedTarget = ref<StrategyPoolFocusItem | null>(null)
  const selectedGroupIds = ref<string[]>([])
  const focusItems = computed(() => groups.value.find((group) => group.id === activeGroupId.value)?.items ?? [])
  const followKeys = computed(() => new Set(groups.value.flatMap((group) => group.items.map(symbol))))
  const activeKeys = computed(() => new Set(focusItems.value.map(symbol)))
  const focusRows = computed(() => sortPoolEntriesByStatus(
    options.entries.value.filter((entry) => activeKeys.value.has(symbol(entry))),
    options.signalStates.value, options.heldSymbols.value, options.objective.value,
  ))
  const focusMissing = computed(() => {
    const poolKeys = new Set(options.entries.value.map(symbol))
    return focusItems.value.filter((item) => !poolKeys.has(symbol(item)))
  })
  const focusMissingCount = computed(() => focusMissing.value.length)

  function toggleFollow(target: StrategyPoolFollowTarget): void {
    if (focusPending.value) return
    selectedTarget.value = { market: target.market, code: target.code, name: target.name ?? '' }
    selectedGroupIds.value = groups.value.filter((group) => group.items.some((item) => symbol(item) === symbol(target))).map((group) => group.id)
  }

  async function change(action: () => Promise<StrategyPoolFocusGroup[]>): Promise<boolean> {
    if (focusPending.value) return false
    focusPending.value = true
    options.errorMessage.value = ''
    try {
      groups.value = await action()
      return true
    } catch (error) {
      options.errorMessage.value = options.describeError(error)
      return false
    } finally {
      focusPending.value = false
    }
  }

  async function saveMembership(): Promise<void> {
    if (!selectedTarget.value) return
    const target = selectedTarget.value
    if (await change(() => setFocusMembership(target, selectedGroupIds.value))) selectedTarget.value = null
  }
  async function addGroup(name: string): Promise<boolean> {
    const existing = new Set(groups.value.map((group) => group.id))
    if (!await change(() => createFocusGroup(name))) return false
    const created = groups.value.find((group) => !existing.has(group.id))
    if (created && selectedTarget.value) selectedGroupIds.value = [...selectedGroupIds.value, created.id]
    return true
  }
  async function renameGroup(id: string, name: string): Promise<void> {
    await change(() => renameFocusGroup(id, name))
  }
  async function removeGroup(id: string): Promise<void> {
    if (await change(() => deleteFocusGroup(id))) activeGroupId.value = 'default'
  }
  async function pruneMissingFocus(): Promise<void> {
    if (focusPending.value || !focusMissing.value.length) return
    const missing = [...focusMissing.value]
    const groupId = activeGroupId.value
    focusPending.value = true
    options.errorMessage.value = ''
    try {
      for (const item of missing) {
        const ids = groups.value.filter((group) => group.id !== groupId && group.items.some((member) => symbol(member) === symbol(item))).map((group) => group.id)
        groups.value = await setFocusMembership(item, ids)
      }
    } catch (error) {
      groups.value = await fetchFocusGroups().catch(() => groups.value)
      options.errorMessage.value = options.describeError(error)
    } finally {
      focusPending.value = false
    }
  }
  async function loadFocus(): Promise<void> {
    const retrying = focusLoadError.value
    focusPending.value = true
    focusLoadError.value = false
    try {
      groups.value = await fetchFocusGroups()
      if (retrying) options.errorMessage.value = ''
      focusPending.value = false
    } catch (error) {
      focusLoadError.value = true
      options.errorMessage.value = options.describeError(error)
    }
  }
  return { groups, activeGroupId, focusItems, focusPending, focusLoadError, followKeys, focusRows, focusMissingCount,
    selectedTarget, selectedGroupIds, toggleFollow, saveMembership, addGroup, renameGroup,
    removeGroup, pruneMissingFocus, loadFocus }
}
