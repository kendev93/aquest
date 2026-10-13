import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'

import { importCandidates } from '../api/candidateLibrary'
import { importSummaryText } from '../utils/candidateLibrary'
import { useCandidateSelection } from './useCandidateSelection'
import type { CandidateSelectionDeps } from './useCandidateSelection'
import type { ConditionResearchReport, RuleGroupPayload, ScreenCondition } from '../types'

vi.mock('../api/candidateLibrary', () => ({ importCandidates: vi.fn() }))

function condition(over: Partial<ScreenCondition> = {}): ScreenCondition {
  return { left_value: 'indicator_three.varo7', operator: 'gte', right_value: null, right_constant: 1.2, ...over }
}

function rule(conditions: ScreenCondition[] = [], over: Partial<RuleGroupPayload> = {}): RuleGroupPayload {
  return { signals: [], conditions, combine_mode: 'all', minimum_matches: null, ...over }
}

const buyCondition = condition()
const sellCondition = condition({ left_value: 'indicator_three.prepare_rally' })

function makeDeps(over: Partial<CandidateSelectionDeps> = {}): CandidateSelectionDeps {
  const rules: Record<'buy' | 'sell', RuleGroupPayload> = { buy: rule(), sell: rule() }
  const deps: CandidateSelectionDeps = {
    report: ref<ConditionResearchReport | null>({
      candidates: [
        { candidate_id: 'c-buy', side: 'buy', condition: buyCondition },
        { candidate_id: 'c-sell', side: 'sell', condition: sellCondition },
      ],
    } as unknown as ConditionResearchReport),
    activeHistoryId: ref('history-1'),
    message: ref(''),
    saving: ref(false),
    holdToEnd: ref(false),
    alwaysInMarket: ref(false),
    emptyRule: () => rule(),
    readRule: (side) => rules[side],
    setBuyRule: vi.fn((next: RuleGroupPayload) => {
      rules.buy = next
    }),
    setSellRule: vi.fn((next: RuleGroupPayload) => {
      rules.sell = next
    }),
    submit: vi.fn().mockResolvedValue(true),
    describeError: (error: unknown) => (error instanceof Error ? error.message : '入库失败'),
    ...over,
  }
  return deps
}

describe('useCandidateSelection', () => {
  let deps: CandidateSelectionDeps

  beforeEach(() => {
    vi.clearAllMocks()
    deps = makeDeps()
  })

  it('toggles candidate ids without mutating the previous list', () => {
    const selection = useCandidateSelection(deps)
    selection.toggleCandidate('c-buy')
    selection.toggleCandidate('c-sell')
    expect(selection.selectedCandidateIds.value).toEqual(['c-buy', 'c-sell'])

    selection.toggleCandidate('c-buy')
    expect(selection.selectedCandidateIds.value).toEqual(['c-sell'])

    selection.clearCandidateSelection()
    expect(selection.selectedCandidateIds.value).toEqual([])
  })

  it('refuses to store candidates that have no history to hang on', async () => {
    deps = makeDeps({ activeHistoryId: ref('') })
    const selection = useCandidateSelection(deps)
    selection.toggleCandidate('c-buy')
    await selection.saveSelectedCandidates()

    expect(importCandidates).not.toHaveBeenCalled()
    expect(deps.message.value).toBe('这次研究还没有历史记录，无法入库。')
    expect(selection.selectedCandidateIds.value).toEqual(['c-buy'])
  })

  it('reports the import outcome, clears the tick boxes and resets saving', async () => {
    vi.mocked(importCandidates).mockResolvedValue({
      added: [{ candidate_id: 'c-buy' }],
      skipped: [{ candidate_id: 'c-sell' }, { candidate_id: 'c-dup' }],
    } as never)
    const selection = useCandidateSelection(deps)
    selection.toggleCandidate('c-buy')
    selection.toggleCandidate('c-sell')
    await selection.saveSelectedCandidates()

    expect(importCandidates).toHaveBeenCalledWith({ history_id: 'history-1', candidate_ids: ['c-buy', 'c-sell'] })
    expect(deps.message.value).toBe(importSummaryText(1, 2))
    expect(selection.selectedCandidateIds.value).toEqual([])
    expect(deps.saving.value).toBe(false)

    vi.mocked(importCandidates).mockRejectedValueOnce(new Error('历史已被淘汰'))
    selection.toggleCandidate('c-buy')
    await selection.saveSelectedCandidates()
    expect(deps.message.value).toBe('历史已被淘汰')
    expect(deps.saving.value).toBe(false)
  })

  it('only ever writes the rule on the side the candidate was mined for', async () => {
    const selection = useCandidateSelection(deps)
    selection.toggleCandidate('c-buy')
    await selection.applySelectedCandidates()
    expect(deps.setBuyRule).toHaveBeenCalledTimes(1)
    expect(deps.setSellRule).not.toHaveBeenCalled()

    vi.mocked(deps.setBuyRule).mockClear()
    selection.toggleCandidate('c-sell')
    await selection.applySelectedCandidates()
    expect(deps.setSellRule).toHaveBeenCalledTimes(1)
    expect(deps.setBuyRule).not.toHaveBeenCalled()
  })

  it('swaps the baseline instead of merging while holdToEnd or alwaysInMarket is on', async () => {
    deps = makeDeps({ holdToEnd: ref(true) })
    const sellSide = useCandidateSelection(deps)
    sellSide.toggleCandidate('c-sell')
    await sellSide.applySelectedCandidates()

    expect(deps.holdToEnd.value).toBe(false)
    expect(deps.setSellRule).toHaveBeenCalledWith(
      expect.objectContaining({ signals: [], conditions: [sellCondition] }),
    )
    expect(deps.setBuyRule).not.toHaveBeenCalled()

    deps = makeDeps({ alwaysInMarket: ref(true) })
    const buySide = useCandidateSelection(deps)
    buySide.toggleCandidate('c-buy')
    await buySide.applySelectedCandidates()
    expect(deps.alwaysInMarket.value).toBe(false)
    expect(deps.setBuyRule).toHaveBeenCalledWith(
      expect.objectContaining({ signals: [], conditions: [buyCondition] }),
    )
    expect(deps.submit).toHaveBeenCalledTimes(1)
  })

  it('ANDs buy candidates onto the base rule and skips conditions already present', async () => {
    const base = condition({ left_value: 'indicator_three.bias20', right_constant: -3 })
    deps = makeDeps({ readRule: (side) => rule(side === 'buy' ? [base] : [sellCondition]) })
    const selection = useCandidateSelection(deps)
    selection.toggleCandidate('c-buy')
    selection.toggleCandidate('c-sell')
    await selection.applySelectedCandidates()

    expect(deps.setBuyRule).toHaveBeenCalledWith(
      expect.objectContaining({ signals: [], combine_mode: 'all', conditions: [base, buyCondition] }),
    )
    // 卖出候选与基础卖出规则里的同一条重复：不加，也不能因此声称重跑了。
    expect(deps.setSellRule).not.toHaveBeenCalled()
    expect(deps.submit).toHaveBeenCalledTimes(1)
  })

  it('ORs sell candidates and says so when nothing new was added', async () => {
    const base = condition({ left_value: 'indicator_three.bias20' })
    deps = makeDeps({ readRule: () => rule([base]) })
    const selection = useCandidateSelection(deps)
    selection.toggleCandidate('c-sell')
    await selection.applySelectedCandidates()
    expect(deps.setSellRule).toHaveBeenCalledWith(
      expect.objectContaining({ signals: [], combine_mode: 'any', conditions: [base, sellCondition] }),
    )

    deps = makeDeps({ readRule: () => rule([buyCondition, sellCondition]) })
    const dup = useCandidateSelection(deps)
    dup.toggleCandidate('c-buy')
    dup.toggleCandidate('c-sell')
    await dup.applySelectedCandidates()
    expect(deps.message.value).toBe('勾选的条件都已经在基础规则里了，无需重复添加。')
    expect(deps.submit).not.toHaveBeenCalled()
    expect(dup.selectedCandidateIds.value).toEqual(['c-buy', 'c-sell'])
  })

  it('does nothing once the report is gone', async () => {
    deps = makeDeps({ report: ref(null) })
    const selection = useCandidateSelection(deps)
    selection.toggleCandidate('c-buy')
    await selection.applySelectedCandidates()
    expect(deps.submit).not.toHaveBeenCalled()
    expect(deps.setBuyRule).not.toHaveBeenCalled()
  })
})
