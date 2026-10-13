import { beforeEach, describe, expect, it, vi } from 'vitest'
import { reactive } from 'vue'

import { usePortfolioHandoff } from './usePortfolioHandoff'
import type { PortfolioHandoffDeps } from './usePortfolioHandoff'
import type { PortfolioFormState } from '../types'

function makeForm(): PortfolioFormState {
  return reactive({
    mode: 'preset',
    buyRule: { signals: [], conditions: [], combine_mode: 'all', minimum_matches: null },
    universe: 'all',
    universeFile: '',
    codesText: '',
    instrumentTypes: ['stock' as const],
    boards: [] as Array<'main' | 'star' | 'chinext'>,
    rankingValue: 'indicator_three.varo7',
    rankOrder: 'desc' as const,
    maxPositions: 5,
    rebalanceFrequency: 'daily' as const,
    execution: 'next_open' as const,
    formulaText: '',
    formulaParameters: {},
    sellRule: { signals: [], conditions: [], combine_mode: 'any', minimum_matches: null },
    stopLossEnabled: false,
    stopLossMode: 'pct' as const,
    stopLossPct: 8,
    stopLossAtrMultiplier: 2,
    stopLossCooldownDays: 0,
    takeProfitEnabled: false,
    takeProfitMode: 'pct' as const,
    takeProfitPct: 15,
    takeProfitAtrMultiplier: 2,
    takeProfitCooldownDays: 0,
    trailingStopEnabled: false,
    trailingStopMode: 'pct' as const,
    trailingStopPct: 10,
    trailingStopAtrMultiplier: 2,
    trailingStopCooldownDays: 0,
    maxHoldDaysEnabled: false,
    maxHoldDays: 10,
    atrPeriod: 14,
    dateMode: 'recent5' as const,
    startDate: '',
    endDate: '',
    initialCash: 100000,
    commission: 0.0003,
    minCommission: 5,
    stampTax: 0.001,
    slippage: 0,
    fitnessRankWeight: 0.5,
    fitnessMinTrades: 5,
    fitnessMaxDrawdown: 20,
    positionMode: 'full' as const,
    fixedSize: 100,
    excludeSt: true,
  })
}

function makeDeps(form: PortfolioFormState) {
  const appliedDiscoveryCandidate = { value: null as null | Record<string, unknown> }
  const replayComparison = { value: null as null | Record<string, unknown> }
  return {
    form,
    errors: { value: {} as Record<string, string> },
    message: { value: '' },
    availableValues: {
      value: [
        { id: 'indicator_three.varo7', display_name: '指标三 · 量比', rankable: true },
        { id: 'indicator_three.bias20', display_name: '指标三 · 乖离', rankable: false },
      ],
    },
    parseCustomFormula: vi.fn().mockResolvedValue(undefined),
    customMetadata: { value: null },
    appliedDiscoveryCandidate,
    replayComparison,
    describeError: (error: unknown) => (error instanceof Error ? error.message : '未知错误'),
  } as unknown as PortfolioHandoffDeps & {
    appliedDiscoveryCandidate: { value: null | Record<string, unknown> }
    replayComparison: { value: null | Record<string, unknown> }
  }
}

describe('usePortfolioHandoff', () => {
  let form: PortfolioFormState
  let deps: ReturnType<typeof makeDeps>
  let api: ReturnType<typeof usePortfolioHandoff>

  beforeEach(() => {
    form = makeForm()
    deps = makeDeps(form)
    api = usePortfolioHandoff(deps)
    sessionStorage.clear()
  })

  describe('applyStoredCandidate', () => {
    it('maps a strategy-lab candidate onto form fields and clears old errors', () => {
      deps.errors.value = { rankingValue: '旧错误' }
      api.applyStoredCandidate({
        universe: 'codes',
        codes: [' 600519 ', '000001'],
        position_mode: 'fixed',
        fixed_size: 200.4,
        exclude_st: false,
        max_positions: 3,
        ranking_value: 'indicator_three.varo7',
        rebalance_frequency: 'weekly',
        buy_rule: { signals: ['indicator_three.prepare_rally'], conditions: [], combine_mode: 'any', minimum_matches: null },
        stop_loss_pct: 0.05,
        max_hold_days: 8,
        initial_cash: 200000,
      })

      expect(form.universe).toBe('codes')
      // 候选链路的 codes 不做 trim：去空白是股池 handoff 的职责，两处口径不同。
      expect(form.codesText).toBe(' 600519 \n000001')
      expect(form.positionMode).toBe('fixed')
      expect(form.fixedSize).toBe(200)
      expect(form.excludeSt).toBe(false)
      expect(form.maxPositions).toBe(3)
      expect(form.rebalanceFrequency).toBe('weekly')
      expect(form.buyRule.signals).toEqual(['indicator_three.prepare_rally'])
      expect(form.stopLossEnabled).toBe(true)
      expect(form.stopLossPct).toBe(5)
      expect(form.atrPeriod).toBe(14)
      expect(form.maxHoldDaysEnabled).toBe(true)
      expect(form.maxHoldDays).toBe(8)
      expect(form.initialCash).toBe(200000)
      expect(deps.errors.value).toEqual({})
      expect(deps.message.value).toContain('已载入策略实验室候选配置')
    })

    it('keeps existing fields when the payload omits them', () => {
      api.applyStoredCandidate({})
      expect(form.universe).toBe('all')
      expect(form.rankingValue).toBe('indicator_three.varo7')
      expect(form.initialCash).toBe(100000)
    })
  })

  describe('applyStoredResearch', () => {
    it('parses a custom formula before applying the stored rules', async () => {
      await api.applyStoredResearch({
        formula_text: ' MA5 > MA20 ',
        formula_parameters: { n1: 5, bad: 'x' },
        buy_rule: { signals: [], conditions: [], combine_mode: 'all', minimum_matches: null },
        date_mode: 'custom',
        start_date: '2024-01-02',
        end_date: '2024-03-01',
      })
      expect(deps.parseCustomFormula).toHaveBeenCalled()
      expect(form.mode).toBe('custom')
      expect(form.formulaText).toBe('MA5 > MA20')
      expect(form.formulaParameters).toEqual({ n1: 5 })
      expect(form.startDate).toBe('2024-01-02')
    })

    it('keeps the current side defaults and says so when the study had none', async () => {
      await api.applyStoredResearch({
        universe: 'sh',
        buy_rule: null,
        sell_rule: null,
        date_mode: 'full',
      })
      expect(form.universe).toBe('sh')
      expect(form.dateMode).toBe('full')
      expect(deps.message.value).toContain('研究未设买入、卖出条件')
    })

    it('prefers the caller-provided notice for strategy loads', async () => {
      await api.applyStoredResearch(
        { buy_rule: { signals: [], conditions: [], combine_mode: 'all', minimum_matches: null } },
        '策略已载入',
      )
      expect(deps.message.value).toBe('策略已载入')
    })
  })
})

describe('usePortfolioHandoff：跨页带入', () => {
  let form: PortfolioFormState
  let deps: ReturnType<typeof makeDeps>
  let api: ReturnType<typeof usePortfolioHandoff>

  beforeEach(() => {
    form = makeForm()
    deps = makeDeps(form)
    api = usePortfolioHandoff(deps)
    sessionStorage.clear()
  })

  it('applies swing rules and reports what was kept unchanged', async () => {
    await api.applyPortfolioBacktestHandoff({
      kind: 'rules',
      source: 'swing-research' as const,
      source_label: '波段研究',
      buy_rule: { signals: [], conditions: [], combine_mode: 'all', minimum_matches: null },
      sell_rule: null,
      ranking_value: null,
      rank_order: 'desc',
    })
    expect(form.buyRule.signals).toEqual([])
    expect(deps.message.value).toContain('波段研究已带入')
    expect(deps.message.value).toContain('当前卖出规则保留')
  })

  it('applies a stock pool handoff with deduped codes and keeps rules', async () => {
    await api.applyPortfolioBacktestHandoff({
      kind: 'stock-pool',
      source: 'formula-screen' as const,
      source_label: '全市场策略池',
      mode: 'preset' as const,
      formula_text: null,
      formula_parameters: {},
      codes: ['600519', '600519', '000001', ''],
      ranking_value: null,
      rank_order: 'desc',
      signal_date: 20240115,
    })
    expect(form.universe).toBe('codes')
    expect(form.codesText).toBe('600519\n000001')
    expect(deps.message.value).toContain('已作为 2 只标的的固定候选池带入')
    expect(deps.message.value).toContain('选股信号日期 2024-01-15')
    expect(deps.message.value).toContain('未来信息偏差')
  })

  it('rejects a ranking value that is not rankable in this mode', async () => {
    await api.applyPortfolioBacktestHandoff({
      kind: 'stock-pool',
      source: 'formula-screen' as const,
      source_label: '全市场策略池',
      mode: 'preset' as const,
      formula_text: null,
      formula_parameters: {},
      codes: ['600519'],
      ranking_value: 'indicator_three.bias20',
      rank_order: 'asc',
      signal_date: null,
    })
    expect(form.rankingValue).toBe('indicator_three.varo7')
    expect(deps.message.value).toContain('不可用，已保留原排序')
  })
})

describe('usePortfolioHandoff：复现比对', () => {
  let form: PortfolioFormState
  let deps: ReturnType<typeof makeDeps>
  let api: ReturnType<typeof usePortfolioHandoff>
  const metrics = {
    total_return: 0.2, annual_return: 0.1, sharpe: 1.5, max_drawdown: 0.05,
    total_trades: 10, win_rate: 0.6, turnover: 2,
  }

  beforeEach(() => {
    form = makeForm()
    deps = makeDeps(form)
    api = usePortfolioHandoff(deps)
  })

  it('marks matching dimensions when the rerun reproduces the study', async () => {
    deps.appliedDiscoveryCandidate.value = {
      candidate_hash: 'c1',
      data_fingerprint: 'fp-1',
      validation_metrics: { ...metrics, trade_fingerprint: 'tf-1' },
    }
    await api.compareAppliedCandidate({
      performance: metrics,
      trades: [],
      data_fingerprint: 'fp-1',
    } as unknown as Parameters<typeof api.compareAppliedCandidate>[0])
    expect(deps.replayComparison.value).toMatchObject({
      candidateHash: 'c1',
      performance: '一致',
      dataFingerprint: '一致',
    })
  })

  it('flags performance mismatches and skips when nothing was applied', async () => {
    deps.appliedDiscoveryCandidate.value = {
      candidate_hash: 'c1', data_fingerprint: null, validation_metrics: metrics,
    }
    await api.compareAppliedCandidate({
      performance: { ...metrics, total_return: 0.9 },
      trades: [],
      data_fingerprint: 'fp-2',
    } as unknown as Parameters<typeof api.compareAppliedCandidate>[0])
    expect(deps.replayComparison.value?.performance).toBe('不一致')

    deps.replayComparison.value = null
    deps.appliedDiscoveryCandidate.value = null
    await api.compareAppliedCandidate({
      performance: metrics, trades: [], data_fingerprint: 'fp-1',
    } as unknown as Parameters<typeof api.compareAppliedCandidate>[0])
    // 没有应用过候选时直接返回：不清空也不覆盖既有的比对结论。
    expect(deps.replayComparison.value).toBeNull()
  })
})

describe('usePortfolioHandoff：带到公式选股', () => {
  let form: PortfolioFormState
  let deps: ReturnType<typeof makeDeps>
  let api: ReturnType<typeof usePortfolioHandoff>

  beforeEach(() => {
    form = makeForm()
    deps = makeDeps(form)
    api = usePortfolioHandoff(deps)
    sessionStorage.clear()
  })

  it('refuses an empty rule, a codes universe, and a mixed rule', () => {
    api.applyBuyRuleToFormulaScreen()
    expect(deps.message.value).toContain('没有买入筛选条件')

    form.buyRule = { signals: ['indicator_three.prepare_rally'], conditions: [], combine_mode: 'all', minimum_matches: null }
    form.universe = 'codes'
    api.applyBuyRuleToFormulaScreen()
    expect(deps.message.value).toContain('指定代码列表')

    form.universe = 'all'
    form.buyRule = {
      signals: ['indicator_three.prepare_rally'],
      conditions: [{ left_value: 'indicator_three.close', operator: 'gt', right_value: 'indicator_three.ma20', right_constant: null }],
      combine_mode: 'all',
      minimum_matches: null,
    }
    api.applyBuyRuleToFormulaScreen()
    expect(deps.message.value).toContain('同时包含快捷信号和指标条件')
  })

  it('writes the handoff payload for a valid signal-only rule', () => {
    form.buyRule = { signals: ['indicator_three.prepare_rally'], conditions: [], combine_mode: 'all', minimum_matches: null }
    form.rankingValue = 'indicator_three.varo7'
    api.applyBuyRuleToFormulaScreen()

    const stored = JSON.parse(sessionStorage.getItem('aquest.condition-research.to-screen') ?? '{}')
    expect(stored.source_label).toBe('组合回测')
    expect(stored.signals).toEqual(['indicator_three.prepare_rally'])
    expect(stored.ranking_value).toBe('indicator_three.varo7')
  })

  it('refuses an empty custom-list file, an unparsed formula, and a non-rankable ranking', () => {
    form.buyRule = { signals: ['indicator_three.prepare_rally'], conditions: [], combine_mode: 'all', minimum_matches: null }

    form.universe = 'custom'
    form.universeFile = '  '
    api.applyBuyRuleToFormulaScreen()
    expect(deps.message.value).toContain('自定义股票列表文件为空')

    form.universe = 'all'
    form.mode = 'custom'
    api.applyBuyRuleToFormulaScreen()
    expect(deps.message.value).toContain('请先解析自定义公式')

    deps.customMetadata.value = { signals: [], values: [] } as never
    form.rankingValue = 'indicator_three.bias20'
    api.applyBuyRuleToFormulaScreen()
    expect(deps.message.value).toContain('不是可跨标的比较的 rankable 输出')
  })
})
