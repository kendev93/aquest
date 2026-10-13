/**
 * 结果区候选条件的勾选与落地：多选、存入候选库、按方向并入基础规则后重跑。
 *
 * 从 ConditionResearchPage 拆出来。「按方向并入」深度读写表单规则与两个基准
 * 开关（holdToEnd / alwaysInMarket），还要立刻触发重跑，所以这些一律注入；
 * 本模块只持有勾选集合本身。
 */

import { ref } from 'vue'
import type { Ref } from 'vue'

import { importCandidates } from '../api/candidateLibrary'
import { importSummaryText } from '../utils/candidateLibrary'
import { isSameCondition } from '../utils/conditionResearch'
import type { ConditionResearchReport, RuleGroupPayload, ScreenCondition } from '../types'

export interface CandidateSelectionDeps {
  /** 当前报告；勾选的候选从这里按 id 取回。 */
  report: Ref<ConditionResearchReport | null>
  /** 勾选候选入库时要挂到的研究历史。 */
  activeHistoryId: Ref<string>
  /** 页面顶部的消息条。 */
  message: Ref<string>
  /** 入库请求进行中的标志（按钮禁用用）。 */
  saving: Ref<boolean>
  /** 基准开关：卖出候选并入时若「持有到期末」开着，先关掉它再换基准。 */
  holdToEnd: Ref<boolean>
  /** 基准开关：买入候选并入时若「始终在场」开着，对偶地先关掉它。 */
  alwaysInMarket: Ref<boolean>
  /** 构造一条空规则（并入候选作为唯一条件时用）。 */
  emptyRule: () => RuleGroupPayload
  /** 读当前某一侧的基础规则（查重用）。 */
  readRule: (side: 'buy' | 'sell') => RuleGroupPayload
  setBuyRule: (rule: RuleGroupPayload) => void
  setSellRule: (rule: RuleGroupPayload) => void
  /** 并入后立刻重跑研究。 */
  submit: () => Promise<boolean>
  /** 后端错误 → 用户可读文案。 */
  describeError: (error: unknown) => string
}

export function useCandidateSelection(deps: CandidateSelectionDeps) {
  const selectedCandidateIds = ref<string[]>([])

  function toggleCandidate(candidateId: string): void {
    selectedCandidateIds.value = selectedCandidateIds.value.includes(candidateId)
      ? selectedCandidateIds.value.filter((item) => item !== candidateId)
      : [...selectedCandidateIds.value, candidateId]
  }

  function clearCandidateSelection(): void {
    selectedCandidateIds.value = []
  }

  async function saveSelectedCandidates(): Promise<void> {
    if (!selectedCandidateIds.value.length) return
    if (!deps.activeHistoryId.value) {
      deps.message.value = '这次研究还没有历史记录，无法入库。'
      return
    }
    deps.saving.value = true
    try {
      const outcome = await importCandidates({
        history_id: deps.activeHistoryId.value,
        candidate_ids: [...selectedCandidateIds.value],
      })
      deps.message.value = importSummaryText(outcome.added.length, outcome.skipped.length)
      clearCandidateSelection()
    } catch (error) {
      deps.message.value = deps.describeError(error)
    } finally {
      deps.saving.value = false
    }
  }

  /**
   * 把勾选的候选条件按方向追加进基础规则，然后立刻重跑一次研究。
   *
   * 买入候选只进买入规则、卖出候选只进卖出规则：挖出来的方向就是它当初被
   * 评估时的位置，换到另一侧没有依据。已存在的相同条件不再重复添加。
   */
  async function applySelectedCandidates(): Promise<void> {
    const selected = (deps.report.value?.candidates ?? []).filter((candidate) => (
      selectedCandidateIds.value.includes(candidate.candidate_id)
    ))
    if (!selected.length) return
    let added = 0
    for (const candidate of selected) {
      const incoming: ScreenCondition = { ...candidate.condition }
      if (candidate.side === 'sell' && deps.holdToEnd.value) {
        // 基准从「一直持有」换成这条候选，否则重测还是以持有到期末为参照，
        // 只会挖出同一批条件。
        deps.holdToEnd.value = false
        deps.setSellRule({ ...deps.emptyRule(), conditions: [incoming] })
        added += 1
        continue
      }
      if (candidate.side === 'buy' && deps.alwaysInMarket.value) {
        // 对偶：基准从「天天买」换成这条候选，否则重测还是以持续满仓为参照。
        deps.alwaysInMarket.value = false
        deps.setBuyRule({ ...deps.emptyRule(), conditions: [incoming] })
        added += 1
        continue
      }
      const isSell = candidate.side === 'sell'
      const target = deps.readRule(isSell ? 'sell' : 'buy')
      if (target.conditions.some((item) => isSameCondition(item, incoming))) continue
      // 买入候选是 AND 到基础买入规则上的（收窄入场），卖出候选是 OR 上去的
      // （多一个离场触发）—— 后端 sell_mask |= condition_window，界面必须同口径，
      // 否则重测跑的是与候选当初被评估时不同的规则。
      const merged: RuleGroupPayload = {
        ...target,
        signals: [],
        combine_mode: isSell ? 'any' : target.combine_mode,
        conditions: [...target.conditions, incoming],
      }
      if (isSell) deps.setSellRule(merged)
      else deps.setBuyRule(merged)
      added += 1
    }
    if (!added) {
      deps.message.value = '勾选的条件都已经在基础规则里了，无需重复添加。'
      return
    }
    clearCandidateSelection()
    await deps.submit()
  }

  return {
    selectedCandidateIds,
    toggleCandidate,
    clearCandidateSelection,
    saveSelectedCandidates,
    applySelectedCandidates,
  }
}
