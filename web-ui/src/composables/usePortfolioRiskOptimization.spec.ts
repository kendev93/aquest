import { beforeEach, describe, expect, it, vi } from 'vitest'

import { getRiskOptimizationHistory } from '../api/strategyDiscovery'
import { usePortfolioRiskOptimization } from './usePortfolioRiskOptimization'
import type { PortfolioRiskOptimizationDeps } from './usePortfolioRiskOptimization'
import type { StrategyDiscoveryResult } from '../types'

vi.mock('../api/strategyDiscovery', () => ({
  cancelStrategyDiscovery: vi.fn(),
  createPortfolioRiskOptimization: vi.fn(),
  fetchRiskOptimizationHistory: vi.fn(),
  getRiskOptimizationHistory: vi.fn(),
  getStrategyDiscovery: vi.fn(),
  getStrategyDiscoveryResults: vi.fn(),
}))

vi.mock('../api/jobs', () => ({ fetchActiveJobs: vi.fn() }))

function makeSavedResult(): StrategyDiscoveryResult {
  return {
    mode: 'risk_optimization',
    request: { base_config: { universe: 'all', max_positions: 5 } },
    results_by_sharpe: [],
    results_by_return: [],
    search_metadata: {},
    validation_metadata: {},
    data_fingerprint: 'portfolio-fingerprint',
    configuration_fingerprint: 'risk-base-fingerprint',
  }
}

function makeDeps() {
  // 结果区锚点由页面注入并绑定到模板：这里必须是同一个 ref，
  // 否则「载入候选」的滚动会落到一个从未绑定的元素上。
  const scrollIntoView = vi.fn()
  const resultsSection = { value: { scrollIntoView } as unknown as HTMLElement }
  const deps = {
    validate: () => ({}),
    errors: { value: {} as Record<string, string> },
    buildPayload: () => ({ universe: 'all' }),
    message: { value: '' },
    describeError: (error: unknown) => (error instanceof Error ? error.message : '未知错误'),
    form: { dateMode: 'recent5', startDate: '', endDate: '', excludeSt: true, atrPeriod: 14 },
    applyStoredCandidate: vi.fn(),
    onCandidateApplied: vi.fn(),
    applyHistoryRequest: vi.fn().mockResolvedValue(undefined),
    resultsSection,
  } as unknown as PortfolioRiskOptimizationDeps
  return { deps, scrollIntoView }
}

describe('usePortfolioRiskOptimization：历史候选载入', () => {
  let deps: PortfolioRiskOptimizationDeps
  let scrollIntoView: ReturnType<typeof vi.fn>

  beforeEach(() => {
    vi.clearAllMocks()
    const made = makeDeps()
    deps = made.deps
    scrollIntoView = made.scrollIntoView
    vi.mocked(getRiskOptimizationHistory).mockResolvedValue(makeSavedResult())
  })

  it('scrolls to the results anchor the page injected', async () => {
    const optimization = usePortfolioRiskOptimization(deps)
    await optimization.loadRiskOptimizationHistoryDetail('risk-1')

    expect(deps.applyHistoryRequest).toHaveBeenCalledTimes(1)
    expect(scrollIntoView).toHaveBeenCalledWith({ behavior: 'smooth', block: 'start' })
    expect(deps.onCandidateApplied).toHaveBeenCalledWith(null)
  })

  it('keeps the panel closed and reports why when the history has no base config', async () => {
    vi.mocked(getRiskOptimizationHistory).mockResolvedValue({
      ...makeSavedResult(),
      request: {},
    })
    const optimization = usePortfolioRiskOptimization(deps)
    await optimization.loadRiskOptimizationHistoryDetail('risk-2')

    expect(deps.applyHistoryRequest).not.toHaveBeenCalled()
    expect(deps.message.value).toContain('缺少寻优时的基础配置')
    expect(scrollIntoView).not.toHaveBeenCalled()
  })
})
