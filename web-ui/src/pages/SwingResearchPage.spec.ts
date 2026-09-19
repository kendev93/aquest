import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { SwingOverviewResult, SwingStudyResult } from '../types'
import SwingResearchPage from './SwingResearchPage.vue'

import * as swingApi from '../api/swingResearch'
import { SWING_PLAN_STORAGE_KEY } from '../utils/swingHandoff'

vi.mock('../api/swingResearch', async () => {
  const actual = await vi.importActual<typeof import('../api/swingResearch')>('../api/swingResearch')
  return { ...actual, runSwingOverview: vi.fn(), runSwingIndicatorStudy: vi.fn() }
})

const overview: SwingOverviewResult = {
  stock_count: 1000,
  scanned_stocks: 1000,
  min_bars: 5,
  atr_window: 14,
  date_range: { start: '2021-09-01', end: '2026-09-11', years: 5.0 },
  scenarios: [{
    reversal_multiple: 3,
    min_bars: 5,
    stock_count: 1000,
    segment_count: 4200,
    long_segment_count: 3100,
    segments_per_stock_per_year: 0.62,
    up: {
      count: 1600,
      duration: { p25: 6, p50: 9, p75: 15, mean: 11, p10: 4, p90: 24 },
      amplitude: { p25: 4.1, p50: 6.2, p75: 9.8, mean: 7.4, p10: 3.0, p90: 15.1 },
      duration_histogram: [{ bucket: '0-3', count: 10 }, { bucket: '3-5', count: 40 }],
      amplitude_histogram: [{ bucket: '0-3', count: 120 }, { bucket: '3-5', count: 300 }],
    },
    down: {
      count: 1500,
      duration: { p25: 5, p50: 8, p75: 13, mean: 10, p10: 3, p90: 21 },
      amplitude: { p25: 3.8, p50: 5.7, p75: 9.1, mean: 6.9, p10: 2.7, p90: 14.2 },
      duration_histogram: [{ bucket: '0-3', count: 12 }, { bucket: '3-5', count: 36 }],
      amplitude_histogram: [{ bucket: '0-3', count: 140 }, { bucket: '3-5', count: 280 }],
    },
  }],
}

const condition = {
  left_value: 'technical.annual_trend24',
  operator: 'lte' as const,
  right_value: null,
  right_constant: -0.273902,
}

const rule = {
  signals: [],
  conditions: [condition],
  required_conditions: [],
  combine_mode: 'all' as const,
  minimum_matches: null,
}

const study: SwingStudyResult = {
  stock_count: 1000,
  scanned_stocks: 980,
  reversal_multiple: 3,
  min_bars: 5,
  atr_window: 14,
  horizon: 10,
  permutations: 200,
  block_size: 20,
  alpha: 0.05,
  value_count: 20,
  labels: [{ id: 'up_onset_ahead', display_name: '近期将启动上涨' }],
  date_range: { start: '2021-09-01', end: '2026-09-11', days: 1219 },
  scores: [{
    value_id: 'technical.annual_trend24',
    display_name: '年化趋势斜率',
    label: 'up_onset_ahead',
    order: 'low',
    ic_mean: 0.111,
    icir: 1.4,
    ic_days: 1210,
    ic_threshold: 0.005,
    top_lift: 0.02,
    threshold_hint: -0.273902,
    pvalue: 0.001,
    qvalue: 0.004,
    significant: true,
    note: null,
  }],
  significant_count: 1,
  reason: null,
  condition_plan: {
    templates: [{
      value_id: 'technical.annual_trend24',
      display_name: '年化趋势斜率',
      label: 'up_onset_ahead',
      label_display: '近期将启动上涨',
      side: 'buy',
      tradeable: true,
      operator: 'lte',
      threshold: -0.273902,
      ic_mean: 0.111,
      icir: 1.4,
      significant: true,
      condition,
      rule,
      note: '阈值取自横截面分位的中位数，会随行情强弱漂移。',
    }],
    buy_rule: rule,
    sell_rule: null,
    ranking: {
      value: 'technical.annual_trend24',
      order: 'asc',
      display_name: '年化趋势斜率',
      ic_mean: 0.111,
      note: '按波段标签给出先验建议。',
    },
    tradeable_count: 1,
    reason: null,
  },
}

/** 和页面里的 defaultStartDate 同一套算法，避免把日期写死之后过几年就失效。 */
function expectedFiveYearsAgo(): string {
  const target = new Date()
  target.setFullYear(target.getFullYear() - 5)
  const month = String(target.getMonth() + 1).padStart(2, '0')
  const day = String(target.getDate()).padStart(2, '0')
  return `${target.getFullYear()}-${month}-${day}`
}

describe('SwingResearchPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    sessionStorage.clear()
    vi.mocked(swingApi.runSwingOverview).mockResolvedValue(overview)
    vi.mocked(swingApi.runSwingIndicatorStudy).mockResolvedValue(study)
  })

  it('renders the segmentation calibration table with amplitudes as percentages', async () => {
    const wrapper = mount(SwingResearchPage)
    await wrapper.get('[data-testid="swing-overview-run"]').trigger('click')
    await flushPromises()

    const rows = wrapper.findAll('[data-testid="swing-scenario-row"]')
    expect(rows).toHaveLength(1)
    expect(rows[0].text()).toContain('3.0 倍')
    expect(rows[0].text()).toContain('6.2% / 5.7%')
    expect(wrapper.get('[data-testid="swing-overview-result"]').text()).toContain('2021-09-01 → 2026-09-11')
  })

  it('shows the study verdict and hands the plan to the condition research page', async () => {
    const wrapper = mount(SwingResearchPage)
    await wrapper.get('[data-testid="swing-study-run"]').trigger('click')
    await flushPromises()

    expect(wrapper.get('[data-testid="swing-study-result"]').text()).toContain('1 / 1')
    expect(wrapper.get('[data-testid="swing-plan-buy"]').text()).toContain('annual_trend24 ≤ -0.2739')
    expect(wrapper.get('[data-testid="swing-plan-ranking"]').text()).toContain('从小到大')
    expect(wrapper.get('[data-testid="swing-score-row"]').text()).toContain('通过检验')

    await wrapper.get('[data-testid="swing-plan-handoff"]').trigger('click')
    await flushPromises()

    const saved = JSON.parse(sessionStorage.getItem(SWING_PLAN_STORAGE_KEY) ?? '{}')
    expect(saved.buy_rule.conditions[0]).toEqual(condition)
    expect(saved.ranking.order).toBe('asc')
  })

  it('defaults both date ranges to the last five years and sends them along', async () => {
    const wrapper = mount(SwingResearchPage)
    const start = expectedFiveYearsAgo()

    const overviewInput = wrapper.get('[data-testid="swing-overview-start"]').element as HTMLInputElement
    const studyInput = wrapper.get('[data-testid="swing-study-start"]').element as HTMLInputElement
    expect(overviewInput.value).toBe(start)
    expect(studyInput.value).toBe(start)

    await wrapper.get('[data-testid="swing-overview-run"]').trigger('click')
    await wrapper.get('[data-testid="swing-study-run"]').trigger('click')
    await flushPromises()

    const overviewCall = vi.mocked(swingApi.runSwingOverview).mock.calls[0]?.[0]
    const studyCall = vi.mocked(swingApi.runSwingIndicatorStudy).mock.calls[0]?.[0]
    expect(overviewCall?.start_date).toBe(start)
    expect(studyCall?.start_date).toBe(start)
  })

  it('shows the stage of the running study on a progress bar', async () => {
    const gate: { release?: () => void } = {}
    vi.mocked(swingApi.runSwingIndicatorStudy).mockImplementation(async (_payload, onProgress) => {
      onProgress?.({ current: 1, total: 4, stage: '正在扫描交易日', ratio: 0.25 })
      await new Promise<void>((resolve) => {
        gate.release = resolve
      })
      onProgress?.({ current: 3, total: 4, stage: '正在逐日横截面打分', ratio: 0.75 })
      return study
    })

    const wrapper = mount(SwingResearchPage)
    void wrapper.get('[data-testid="swing-study-run"]').trigger('click')
    await flushPromises()

    const bar = wrapper.get('[data-testid="swing-study-progress"]')
    expect(bar.text()).toContain('正在扫描交易日')
    expect(bar.text()).toContain('25%')
    expect((bar.get('.swing-progress-fill').element as HTMLElement).style.width).toBe('25%')

    gate.release?.()
    await flushPromises()
    expect(wrapper.find('[data-testid="swing-study-progress"]').exists()).toBe(false)
  })

  it('shows the backend message when a study is rejected', async () => {
    vi.mocked(swingApi.runSwingIndicatorStudy).mockRejectedValue(
      new swingApi.SwingResearchApiError('面板需要 5.2 GB 临时空间，超出上限 3 GB', 400),
    )
    const wrapper = mount(SwingResearchPage)
    await wrapper.get('[data-testid="swing-study-run"]').trigger('click')
    await flushPromises()

    expect(wrapper.get('[data-testid="swing-message"]').text()).toContain('超出上限 3 GB')
  })
})
