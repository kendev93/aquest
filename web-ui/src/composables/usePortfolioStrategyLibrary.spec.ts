import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'

import {
  createStrategy,
  deleteStrategy,
  fetchStrategies,
  getStrategy,
  updateStrategy,
} from '../api/savedStrategies'
import { usePortfolioStrategyLibrary } from './usePortfolioStrategyLibrary'
import type { PortfolioBacktestResult, SavedStrategyDetail, SavedStrategySummary } from '../types'

vi.mock('../api/savedStrategies', () => ({
  createStrategy: vi.fn(),
  deleteStrategy: vi.fn(),
  fetchStrategies: vi.fn(),
  getStrategy: vi.fn(),
  updateStrategy: vi.fn(),
}))

const stored: SavedStrategySummary = {
  strategy_id: 'strategy-1',
  name: '主力进场轮动',
  note: '',
  source_history_id: null,
  created_at: '2026-09-21 01:00:00',
  updated_at: '2026-09-21 01:00:00',
  summary: {
    start_date: '2024-01-02',
    end_date: '2024-01-06',
    bars: 60,
    max_positions: 5,
    ranking_value: 'indicator_three.varo7',
    universe: 'all',
    performance: { total_return: 0.12 },
  },
}

/** 详情接口多带一份可重放的 request；列表项没有。 */
const detail = { ...stored, request: { universe: 'sz' } } as unknown as SavedStrategyDetail

const result = {
  universe: 'all',
  start_date: '2024-01-02',
  end_date: '2024-06-30',
  bars: 120,
  max_positions: 8,
  ranking_value: 'indicator_three.varo7',
  performance: { total_return: 0.31 },
  benchmark: null,
} as unknown as PortfolioBacktestResult

function makeDeps(withResult = true) {
  const applied: Array<[unknown, string | undefined]> = []
  const deps = {
    result: ref<PortfolioBacktestResult | null>(withResult ? result : null),
    message: ref(''),
    describeError: (error: unknown) => (error instanceof Error ? error.message : '未知错误'),
    buildPayload: vi.fn().mockReturnValue({ universe: 'all' }),
    applyStoredResearch: vi.fn(async (raw: unknown, notice?: string) => {
      applied.push([raw, notice])
    }),
  }
  return { deps, applied }
}

describe('usePortfolioStrategyLibrary', () => {
  let made: ReturnType<typeof makeDeps>

  beforeEach(() => {
    vi.clearAllMocks()
    made = makeDeps()
    vi.mocked(fetchStrategies).mockResolvedValue([stored])
  })

  it('refuses to save without a result or a name', async () => {
    const empty = makeDeps(false)
    const guard = usePortfolioStrategyLibrary(empty.deps)
    await guard.saveCurrentStrategy()
    expect(createStrategy).not.toHaveBeenCalled()

    const library = usePortfolioStrategyLibrary(made.deps)
    library.newStrategyName.value = '   '
    await library.saveCurrentStrategy()
    expect(createStrategy).not.toHaveBeenCalled()
    expect(made.deps.message.value).toBe('请先填写策略名称。')
  })

  it('saves the current payload with a result snapshot and refreshes the list', async () => {
    vi.mocked(createStrategy).mockResolvedValue(detail)
    const library = usePortfolioStrategyLibrary(made.deps)
    library.newStrategyName.value = '  主力进场轮动 '
    await library.saveCurrentStrategy()

    expect(createStrategy).toHaveBeenCalledWith({
      name: '主力进场轮动',
      request: { universe: 'all' },
      summary: {
        start_date: '2024-01-02',
        end_date: '2024-06-30',
        bars: 120,
        max_positions: 8,
        ranking_value: 'indicator_three.varo7',
        universe: 'all',
        performance: { total_return: 0.31 },
        benchmark: null,
      },
    })
    expect(library.newStrategyName.value).toBe('')
    expect(fetchStrategies).toHaveBeenCalledTimes(1)
    expect(made.deps.message.value).toBe('策略「主力进场轮动」已保存。')
    expect(library.strategySaving.value).toBe(false)
  })

  it('loads a stored strategy through the page backfill and always clears busy', async () => {
    vi.mocked(getStrategy).mockResolvedValue(detail)
    const library = usePortfolioStrategyLibrary(made.deps)
    await library.applyStrategy('strategy-1')

    expect(made.applied).toEqual([
      [{ universe: 'sz' }, '已载入策略「主力进场轮动」的配置，请确认后开始回测。'],
    ])
    expect(library.strategyBusyId.value).toBe('')

    vi.mocked(getStrategy).mockRejectedValueOnce(new Error('策略已被删除'))
    await library.applyStrategy('strategy-1')
    expect(made.deps.message.value).toBe('策略已被删除')
    expect(library.strategyBusyId.value).toBe('')
  })

  it('keeps rename local until the draft is usable', async () => {
    const library = usePortfolioStrategyLibrary(made.deps)
    library.startRename(stored)
    expect(library.renamingId.value).toBe('strategy-1')
    expect(library.renameDraft.value).toBe('主力进场轮动')

    library.renameDraft.value = '  '
    await library.commitRename('strategy-1')
    expect(updateStrategy).not.toHaveBeenCalled()
    expect(made.deps.message.value).toBe('策略名称不能为空。')
    expect(library.renamingId.value).toBe('strategy-1')

    library.renameDraft.value = ' 新名字 '
    vi.mocked(updateStrategy).mockResolvedValue(detail)
    await library.commitRename('strategy-1')
    expect(updateStrategy).toHaveBeenCalledWith('strategy-1', { name: '新名字' })
    expect(library.renamingId.value).toBe('')
    expect(made.deps.message.value).toBe('策略已重命名。')

    library.cancelRename()
    expect(library.renamingId.value).toBe('')
    expect(library.renameDraft.value).toBe('')
  })

  it('deletes a strategy, closes the confirm row, and reports failures', async () => {
    vi.mocked(deleteStrategy).mockResolvedValue({ deleted: true })
    const library = usePortfolioStrategyLibrary(made.deps)
    library.pendingDeleteId.value = 'strategy-1'
    await library.confirmDelete('strategy-1')
    expect(library.pendingDeleteId.value).toBe('')
    expect(fetchStrategies).toHaveBeenCalledTimes(1)
    expect(made.deps.message.value).toBe('策略已删除。')

    vi.mocked(deleteStrategy).mockRejectedValueOnce(new Error('删除失败'))
    await library.confirmDelete('strategy-2')
    expect(made.deps.message.value).toBe('删除失败')
    expect(library.strategyBusyId.value).toBe('')
  })

  it('labels a legacy strategy without a stored range', () => {
    const library = usePortfolioStrategyLibrary(made.deps)
    expect(library.strategyRange(stored)).toBe('2024-01-02 ~ 2024-01-06')
    expect(library.strategyRange({
      ...stored,
      summary: { ...stored.summary, end_date: '' },
    })).toBe('区间未记录')
  })
})
