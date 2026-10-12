import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import * as api from '../api/candidateLibrary'
import * as researchApi from '../api/conditionResearch'
import { fetchMetadata, FormulaScreenApiError } from '../api/formulaScreen'
import type { CandidateLibraryItem, CandidatePublishPreview, FormulaScreenMetadata } from '../types'
import CandidateLibraryPage from './CandidateLibraryPage.vue'

vi.mock('../api/candidateLibrary', async () => {
  const actual = await vi.importActual<typeof import('../api/candidateLibrary')>(
    '../api/candidateLibrary',
  )
  return {
    ...actual,
    fetchCandidates: vi.fn(),
    deleteCandidate: vi.fn(),
    fetchPublishPreview: vi.fn(),
    publishCandidate: vi.fn(),
    updateCandidateStatus: vi.fn(),
  }
})

vi.mock('../api/formulaScreen', async () => {
  const actual = await vi.importActual<typeof import('../api/formulaScreen')>(
    '../api/formulaScreen',
  )
  return { ...actual, fetchMetadata: vi.fn() }
})

vi.mock('../api/conditionResearch', async () => {
  const actual = await vi.importActual<typeof import('../api/conditionResearch')>(
    '../api/conditionResearch',
  )
  return { ...actual, getConditionResearchHistory: vi.fn() }
})

const metadata: FormulaScreenMetadata = {
  indicators: [
    {
      id: 'technical',
      name: '技术指标',
      description: '',
      values: [{ id: 'technical.rsrs_zscore', display_name: 'RSRS 标准分', rankable: true }],
    },
  ],
  signals: [],
} as unknown as FormulaScreenMetadata

function candidate(overrides: Partial<CandidateLibraryItem> = {}): CandidateLibraryItem {
  return {
    candidate_id: 'c1',
    source: 'condition_research',
    source_history_id: 'study-1-aaaaaaaa',
    source_candidate_id: 'buy-1',
    side: 'buy',
    label: '乖离率20 ≤ -5',
    condition: { left_value: 'technical.bias20', operator: 'lte', right_value: null, right_constant: -5 },
    evidence: {
      lift: 1.8,
      support_count: 320,
      instrument_count: 120,
      outcome_rate: 0.62,
      baseline_rate: 0.5,
      support_ratio: 0.08,
      permutation_pvalue: 0.02,
      significant: true,
      lift_threshold: 1.5,
      top_n_outcome_rate: 0.6,
      top_n_filtered_days: 40,
      median_retention: 0.7,
      worst_retention: 0.4,
      passed_constraints: true,
      constraint_failures: [],
      created_at: '2026-09-20 10:00:00',
      validation: { trade_count: 320, win_rate: 0.62 },
      holdout: { trade_count: 90, win_rate: 0.58 },
      consistency: { evaluated: 3, better_than_baseline: 2 },
      study: { start_date: '2024-01-02', end_date: '2026-09-10', scanned_stocks: 5045 },
    },
    status: 'pending',
    note: '',
    published_strategy_id: null,
    created_at: '2026-09-20 10:00:00',
    updated_at: '2026-09-20 10:00:00',
    ...overrides,
  }
}

function preview(overrides: Partial<CandidatePublishPreview> = {}): CandidatePublishPreview {
  return {
    candidate_id: 'c1',
    label: '乖离率20 ≤ -5',
    side: 'buy',
    status: 'adopted',
    request: { ranking_value: 'technical.rsrs_zscore' },
    validated: true,
    notes: ['这条条件已追加到买入规则（保持原有组合方式）。'],
    blockers: [],
    blocked: false,
    ready: true,
    ...overrides,
  }
}

describe('CandidateLibraryPage', () => {
  beforeEach(() => {
    vi.mocked(api.fetchCandidates).mockResolvedValue([])
    vi.mocked(api.fetchPublishPreview).mockResolvedValue(preview())
    vi.mocked(api.publishCandidate).mockResolvedValue({
      candidate: candidate({ status: 'published' }),
      strategy: { strategy_id: 's1' } as never,
      online: false,
      notes: [],
      message: '已发布为策略「甲策略」，未上线。',
    })
    vi.mocked(api.updateCandidateStatus).mockResolvedValue(candidate({ status: 'adopted' }))
    vi.mocked(api.deleteCandidate).mockResolvedValue({ deleted: true })
    vi.mocked(fetchMetadata).mockResolvedValue(metadata)
  })

  it('shows an empty hint that points at the research page', async () => {
    const wrapper = mount(CandidateLibraryPage)
    await flushPromises()

    expect(wrapper.get('[data-testid="candidate-empty"]').text()).toContain('买卖条件研究')
  })

  it('renders the evidence snapshot with its provenance', async () => {
    vi.mocked(api.fetchCandidates).mockResolvedValue([candidate()])
    const wrapper = mount(CandidateLibraryPage)
    await flushPromises()

    expect(wrapper.text()).toContain('乖离率20 ≤ -5')
    expect(wrapper.text()).toContain('通过置换检验')
    expect(wrapper.text()).toContain('1.80 倍')
    expect(wrapper.text()).toContain('2024-01-02 ~ 2026-09-10')
    expect(wrapper.text()).toContain('扫过 5045 只')
    expect(wrapper.get('[data-testid="candidate-status-c1"]').text()).toBe('待定')
  })

  it('adopts a pending candidate', async () => {
    vi.mocked(api.fetchCandidates).mockResolvedValue([candidate()])
    const wrapper = mount(CandidateLibraryPage)
    await flushPromises()

    await wrapper.get('[data-testid="candidate-action-adopted-c1"]').trigger('click')
    await flushPromises()

    expect(api.updateCandidateStatus).toHaveBeenCalledWith('c1', { status: 'adopted' })
    expect(wrapper.get('[data-testid="candidate-message"]').text()).toContain('已采纳')
  })

  it('refuses to publish before adoption and says why', async () => {
    vi.mocked(api.fetchCandidates).mockResolvedValue([candidate()])
    const wrapper = mount(CandidateLibraryPage)
    await flushPromises()

    await wrapper.get('[data-testid="candidate-publish-open-c1"]').trigger('click')
    await flushPromises()

    expect(wrapper.get('[data-testid="publish-blocked-reason"]').text()).toContain('先采纳')
    expect(
      wrapper.get('[data-testid="candidate-publish-confirm-c1"]').attributes('disabled'),
    ).toBeDefined()
  })

  it('publishes after showing what will be written, and reports it stayed offline', async () => {
    vi.mocked(api.fetchCandidates).mockResolvedValue([candidate({ status: 'adopted' })])
    const wrapper = mount(CandidateLibraryPage)
    await flushPromises()

    await wrapper.get('[data-testid="candidate-publish-open-c1"]').trigger('click')
    await flushPromises()

    expect(wrapper.get('[data-testid="publish-warning"]').text()).toContain('不会上线')
    expect(wrapper.get('[data-testid="publish-notes"]').text()).toContain('追加到买入规则')

    await wrapper.get('[data-testid="publish-name-c1"]').setValue('甲策略')
    await wrapper.get('[data-testid="candidate-publish-confirm-c1"]').trigger('click')
    await flushPromises()

    expect(api.publishCandidate).toHaveBeenCalled()
    expect(wrapper.get('[data-testid="publish-result"]').text()).toContain('未上线')
  })

  it('asks for an exit rule when the study had none', async () => {
    vi.mocked(api.fetchCandidates).mockResolvedValue([candidate({ status: 'adopted' })])
    vi.mocked(api.fetchPublishPreview).mockResolvedValue(
      preview({
        blocked: true,
        ready: false,
        validated: false,
        blockers: ['这次研究没设卖出规则（当初是持有到期末），但组合回测不设离场规则就永远不卖。'],
      }),
    )
    const wrapper = mount(CandidateLibraryPage)
    await flushPromises()

    await wrapper.get('[data-testid="candidate-publish-open-c1"]').trigger('click')
    await flushPromises()

    expect(wrapper.get('[data-testid="publish-blockers"]').text()).toContain('离场')
    expect(wrapper.get('[data-testid="publish-stop-loss-c1"]').element).toBeDefined()
  })

  it('offers the rankable values when the study had no ranking rule', async () => {
    vi.mocked(api.fetchCandidates).mockResolvedValue([candidate({ status: 'adopted' })])
    vi.mocked(api.fetchPublishPreview).mockResolvedValue(
      preview({ blocked: true, ready: false, blockers: ['这次研究没有指定排序规则'] }),
    )
    const wrapper = mount(CandidateLibraryPage)
    await flushPromises()

    await wrapper.get('[data-testid="candidate-publish-open-c1"]').trigger('click')
    await flushPromises()

    const options = wrapper.get('[data-testid="publish-ranking-c1"]').findAll('option')
    expect(options.map((option) => option.text()).join()).toContain('RSRS 标准分')
  })

  it('withdraws a publication and says the strategy went with it', async () => {
    vi.mocked(api.fetchCandidates).mockResolvedValue([
      candidate({ status: 'published', published_strategy_id: 's1-aaaaaaaa' }),
    ])
    const wrapper = mount(CandidateLibraryPage)
    await flushPromises()

    await wrapper.get('[data-testid="candidate-action-adopted-c1"]').trigger('click')
    await flushPromises()

    expect(wrapper.get('[data-testid="candidate-message"]').text()).toContain('撤回发布')
  })

  it('deletes an unpublished candidate', async () => {
    vi.mocked(api.fetchCandidates).mockResolvedValue([candidate()])
    const wrapper = mount(CandidateLibraryPage)
    await flushPromises()

    await wrapper.get('[data-testid="candidate-delete-c1"]').trigger('click')
    await flushPromises()

    expect(api.deleteCandidate).toHaveBeenCalledWith('c1')
  })

  it('filters by status', async () => {
    vi.mocked(api.fetchCandidates).mockResolvedValue([candidate()])
    const wrapper = mount(CandidateLibraryPage)
    await flushPromises()

    await wrapper.get('[data-testid="candidate-filter-adopted"]').trigger('click')
    await flushPromises()

    expect(api.fetchCandidates).toHaveBeenLastCalledWith('adopted')
  })

  it('hands the source study request to the research page', async () => {
    vi.mocked(api.fetchCandidates).mockResolvedValue([candidate()])
    vi.mocked(researchApi.getConditionResearchHistory).mockResolvedValue({
      history_id: 'study-1-aaaaaaaa',
      request: { horizon: 3, holdout_ratio: 0.2 },
    } as never)

    const navigated: string[] = []
    const original = window.location
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { set href(value: string) { navigated.push(value) }, get href() { return '' } },
    })
    const wrapper = mount(CandidateLibraryPage)
    await flushPromises()
    await wrapper.get('[data-testid="candidate-restore-c1"]').trigger('click')
    await flushPromises()
    Object.defineProperty(window, 'location', { configurable: true, value: original })

    expect(researchApi.getConditionResearchHistory).toHaveBeenCalledWith('study-1-aaaaaaaa')
    expect(navigated).toEqual(['/condition-research'])
    // 走 storage 而不是 URL 参数：整份 request 有几百字节，塞进查询串既难看又有长度上限。
    expect(JSON.parse(sessionStorage.getItem('aquest.condition-research.restore') ?? '{}'))
      .toEqual({ horizon: 3, holdout_ratio: 0.2 })
  })

  it('says so instead of doing nothing when the source study was evicted', async () => {
    vi.mocked(api.fetchCandidates).mockResolvedValue([candidate()])
    vi.mocked(researchApi.getConditionResearchHistory)
      .mockRejectedValue(new FormulaScreenApiError('历史记录不存在', 404))

    const wrapper = mount(CandidateLibraryPage)
    await flushPromises()
    await wrapper.get('[data-testid="candidate-restore-c1"]').trigger('click')
    await flushPromises()

    expect(wrapper.get('[data-testid="candidate-message"]').text()).toContain('已被淘汰')
    expect(wrapper.get('[data-testid="candidate-message"]').text()).toContain('无法还原配置')
  })
})
