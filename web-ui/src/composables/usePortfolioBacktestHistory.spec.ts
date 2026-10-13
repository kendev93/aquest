import { beforeEach, describe, expect, it, vi } from 'vitest'
import { reactive, ref } from 'vue'

import {
  deletePortfolioBacktestHistory,
  fetchPortfolioBacktestHistory,
  getPortfolioBacktestHistory,
} from '../api/portfolioBacktest'
import { usePortfolioBacktestHistory } from './usePortfolioBacktestHistory'
import type { PortfolioBacktestHistoryDeps } from './usePortfolioBacktestHistory'
import type { BacktestHistorySummary, PortfolioBacktestResult } from '../types'

vi.mock('../api/portfolioBacktest', () => ({
  deletePortfolioBacktestHistory: vi.fn(),
  fetchPortfolioBacktestHistory: vi.fn(),
  getPortfolioBacktestHistory: vi.fn(),
}))

function makeForm(): PortfolioBacktestHistoryDeps['form'] {
  return reactive({
    mode: 'preset',
    formulaText: '',
    formulaParameters: {} as Record<string, number>,
    dateMode: 'recent5',
    atrPeriod: 14,
    fitnessRankWeight: 0.5,
    fitnessMinTrades: 5,
    fitnessMaxDrawdown: 20,
  })
}

function makeDeps() {
  const calls: string[] = []
  const form = makeForm()
  const deps = {
    form,
    message: ref(''),
    describeError: (error: unknown) => (error instanceof Error ? `已处理：${error.message}` : '未知错误'),
    parseCustomFormula: vi.fn().mockImplementation(async () => {
      calls.push('parse')
    }),
    applyStoredCandidate: vi.fn((raw: unknown) => {
      calls.push(`apply:${(raw as { universe?: string }).universe ?? '?'}`)
    }),
    onResultRestored: vi.fn(),
  }
  return { deps, calls, form }
}

const historyItem: BacktestHistorySummary = {
  history_id: 'history-1',
  backtest_type: 'portfolio',
  created_at: '2026-09-20 01:04:00',
  started_at: '2026-09-20 01:00:00',
  finished_at: '2026-09-20 01:04:00',
  duration_seconds: 240,
  summary: { total_return: 0.12 },
}

describe('usePortfolioBacktestHistory', () => {
  let deps: ReturnType<typeof makeDeps>

  beforeEach(() => {
    vi.clearAllMocks()
    deps = makeDeps()
    vi.mocked(fetchPortfolioBacktestHistory).mockResolvedValue([historyItem])
  })

  it('loads the list and keeps read failures inside the panel', async () => {
    const history = usePortfolioBacktestHistory(deps.deps)
    await history.loadHistory()
    expect(history.history.value).toEqual([historyItem])
    expect(history.historyError.value).toBe('')

    vi.mocked(fetchPortfolioBacktestHistory).mockRejectedValueOnce(new Error('库被锁住了'))
    await history.loadHistory()
    expect(history.historyError.value).toBe('库被锁住了')
    expect(deps.deps.message.value).toBe('')
    expect(history.historyLoading.value).toBe(false)
  })

  it('parses a custom formula before re-applying the stored rules', async () => {
    const history = usePortfolioBacktestHistory(deps.deps)
    await history.applyHistoryRequest({
      universe: 'all',
      formula_text: ' MA5 > MA20 ',
      formula_parameters: { n1: 5, bad: 'x', nan: Number.NaN },
    }, '已还原')

    expect(deps.calls).toEqual(['parse', 'apply:all'])
    expect(deps.form.mode).toBe('custom')
    expect(deps.form.formulaText).toBe('MA5 > MA20')
    expect(deps.form.formulaParameters).toEqual({ n1: 5 })
    expect(deps.deps.message.value).toBe('已还原')
  })

  it('falls back to preset mode and restores date and risk knobs', async () => {
    const history = usePortfolioBacktestHistory(deps.deps)
    await history.applyHistoryRequest({
      universe: 'sh',
      date_mode: 'custom',
      atr_period: 10,
      fitness_rank_weight: 0.7,
      fitness_min_trades: 8,
      // 存储里的小数比例（0.25）回填成表单上的百分数（25）。
      fitness_max_drawdown: 0.25,
    }, '已还原')

    expect(deps.form.mode).toBe('preset')
    expect(deps.calls).toEqual(['apply:sh'])
    expect(deps.form.dateMode).toBe('custom')
    expect(deps.form.atrPeriod).toBe(10)
    expect(deps.form.fitnessRankWeight).toBe(0.7)
    expect(deps.form.fitnessMaxDrawdown).toBe(25)
  })

  it('keeps the current form when the stored values are unusable', async () => {
    deps.form.fitnessRankWeight = 0.4
    deps.form.fitnessMinTrades = 6
    deps.form.fitnessMaxDrawdown = 18
    const history = usePortfolioBacktestHistory(deps.deps)
    await history.applyHistoryRequest({
      universe: 'sh',
      date_mode: 'yesterday',
      atr_period: 0,
      fitness_rank_weight: 'x',
      fitness_max_drawdown: null,
    }, '已还原')

    expect(deps.form.dateMode).toBe('recent5')
    expect(deps.form.atrPeriod).toBe(14)
    expect(deps.form.fitnessRankWeight).toBe(0.4)
    expect(deps.form.fitnessMinTrades).toBe(6)
    expect(deps.form.fitnessMaxDrawdown).toBe(18)
  })

  it('restores the stored result and its inputs together', async () => {
    const result = { universe: 'all', performance: {} } as PortfolioBacktestResult
    vi.mocked(getPortfolioBacktestHistory).mockResolvedValue({
      ...historyItem,
      result,
      request: { universe: 'sz', formula_text: 'RSI14 < 30' },
    })
    const history = usePortfolioBacktestHistory(deps.deps)
    await history.loadHistoryDetail('history-1')

    expect(deps.deps.onResultRestored).toHaveBeenCalledWith(result)
    expect(deps.calls).toEqual(['parse', 'apply:sz'])
    expect(deps.deps.message.value).toContain('请确认后重新运行')
  })

  it('deletes an entry, reloads the list, and reports backend wording', async () => {
    vi.mocked(deletePortfolioBacktestHistory).mockResolvedValue({ deleted: true })
    const history = usePortfolioBacktestHistory(deps.deps)
    await history.removeHistory('history-1')
    expect(fetchPortfolioBacktestHistory).toHaveBeenCalledTimes(1)
    expect(deps.deps.message.value).toBe('历史组合回测已删除。')

    vi.mocked(deletePortfolioBacktestHistory).mockRejectedValueOnce(new Error('还在跑'))
    await history.removeHistory('history-2')
    expect(deps.deps.message.value).toBe('已处理：还在跑')
  })
})
