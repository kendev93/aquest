/**
 * 页面右侧的策略库：保存当前回测为策略、载入、重命名、删除。
 *
 * 从 PortfolioBacktestPage 拆出来。列表与 busy 状态由本模块持有；保存时要
 * 打包当前表单（buildPayload）和当前结果摘要（result），载入要走页面的
 * 表单回填（applyStoredResearch），这些一律注入。
 */

import { ref } from 'vue'
import type { Ref } from 'vue'

import {
  createStrategy,
  deleteStrategy,
  fetchStrategies,
  getStrategy,
  updateStrategy,
} from '../api/savedStrategies'
import type { PortfolioBacktestPayload, PortfolioBacktestResult, SavedStrategySummary } from '../types'

export interface StrategyLibraryDeps {
  /** 当前回测结果：保存策略时从中提取摘要；为 null 时不能保存。 */
  result: Ref<PortfolioBacktestResult | null>
  /** 页面顶部的消息条。 */
  message: Ref<string>
  /** 后端错误 → 用户可读文案。 */
  describeError: (error: unknown) => string
  /** 当前表单的完整 payload（保存策略时打包存储）。 */
  buildPayload: () => PortfolioBacktestPayload
  /** 把一份存储的请求回填进表单（含「请确认后开始回测」的提示）。 */
  applyStoredResearch: (raw: unknown, notice?: string) => Promise<void>
}

export function usePortfolioStrategyLibrary(deps: StrategyLibraryDeps) {
  const strategies = ref<SavedStrategySummary[]>([])
  const newStrategyName = ref('')
  const strategyLoading = ref(false)
  const strategySaving = ref(false)
  const strategyBusyId = ref('')
  const renamingId = ref('')
  const renameDraft = ref('')
  const pendingDeleteId = ref('')

  async function loadStrategies(): Promise<void> {
    strategyLoading.value = true
    try {
      strategies.value = await fetchStrategies()
    } catch (error) {
      deps.message.value = deps.describeError(error)
    } finally {
      strategyLoading.value = false
    }
  }

  async function saveCurrentStrategy(): Promise<void> {
    if (!deps.result.value) return
    const name = newStrategyName.value.trim()
    if (!name) {
      deps.message.value = '请先填写策略名称。'
      return
    }
    strategySaving.value = true
    try {
      const current = deps.result.value
      await createStrategy({
        name,
        request: deps.buildPayload(),
        summary: {
          start_date: current.start_date,
          end_date: current.end_date,
          bars: current.bars,
          max_positions: current.max_positions,
          ranking_value: current.ranking_value,
          universe: current.universe,
          performance: current.performance,
          benchmark: current.benchmark ?? null,
        },
      })
      newStrategyName.value = ''
      await loadStrategies()
      deps.message.value = `策略「${name}」已保存。`
    } catch (error) {
      deps.message.value = deps.describeError(error)
    } finally {
      strategySaving.value = false
    }
  }

  async function applyStrategy(strategyId: string): Promise<void> {
    strategyBusyId.value = strategyId
    try {
      const detail = await getStrategy(strategyId)
      await deps.applyStoredResearch(
        detail.request,
        `已载入策略「${detail.name}」的配置，请确认后开始回测。`,
      )
    } catch (error) {
      deps.message.value = deps.describeError(error)
    } finally {
      strategyBusyId.value = ''
    }
  }

  function startRename(strategy: SavedStrategySummary): void {
    renamingId.value = strategy.strategy_id
    renameDraft.value = strategy.name
    pendingDeleteId.value = ''
  }

  function cancelRename(): void {
    renamingId.value = ''
    renameDraft.value = ''
  }

  async function commitRename(strategyId: string): Promise<void> {
    const name = renameDraft.value.trim()
    if (!name) {
      deps.message.value = '策略名称不能为空。'
      return
    }
    strategyBusyId.value = strategyId
    try {
      await updateStrategy(strategyId, { name })
      renamingId.value = ''
      await loadStrategies()
      deps.message.value = '策略已重命名。'
    } catch (error) {
      deps.message.value = deps.describeError(error)
    } finally {
      strategyBusyId.value = ''
    }
  }

  async function confirmDelete(strategyId: string): Promise<void> {
    strategyBusyId.value = strategyId
    try {
      await deleteStrategy(strategyId)
      pendingDeleteId.value = ''
      await loadStrategies()
      deps.message.value = '策略已删除。'
    } catch (error) {
      deps.message.value = deps.describeError(error)
    } finally {
      strategyBusyId.value = ''
    }
  }

  /** 列表行上的回测区间摘要；老策略可能没存区间。 */
  function strategyRange(strategy: SavedStrategySummary): string {
    const start = strategy.summary.start_date
    const end = strategy.summary.end_date
    return start && end ? `${start} ~ ${end}` : '区间未记录'
  }

  return {
    strategies,
    newStrategyName,
    strategyLoading,
    strategySaving,
    strategyBusyId,
    renamingId,
    renameDraft,
    pendingDeleteId,
    loadStrategies,
    saveCurrentStrategy,
    applyStrategy,
    startRename,
    cancelRename,
    commitRename,
    confirmDelete,
    strategyRange,
  }
}
