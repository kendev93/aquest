import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { BacktestHistorySummary, SwingOverviewResult, SwingStudyResult } from '../types'
import SwingResearchPage from './SwingResearchPage.vue'

import * as swingApi from '../api/swingResearch'
import type { SwingHistoryRecord } from '../api/swingResearch'
import { SWING_PLAN_STORAGE_KEY } from '../utils/swingHandoff'
import { ApiError } from '../api/client'

vi.mock('../api/swingResearch', async () => {
  const actual = await vi.importActual<typeof import('../api/swingResearch')>('../api/swingResearch')
  return {
    ...actual,
    runSwingOverview: vi.fn(),
    runSwingIndicatorStudy: vi.fn(),
    fetchSwingResearchHistory: vi.fn(),
    getSwingResearchHistory: vi.fn(),
    deleteSwingResearchHistory: vi.fn(),
  }
})

/** 一条只够列表渲染的记录。分页与删除用例靠它凑数量。 */
function historyItem(index: number): BacktestHistorySummary {
  const day = String(index + 1).padStart(2, '0')
  return {
    history_id: `hist-${index}`,
    backtest_type: 'swing_research',
    created_at: `2026-09-${day}T02:00:00`,
    started_at: `2026-09-${day}T01:58:00`,
    finished_at: `2026-09-${day}T02:00:00`,
    duration_seconds: 120,
    summary: {
      universe: 'sh',
      stock_count: 1000 - index,
      reversal_multiple: 3,
      min_bars: 5,
      horizon: 10,
      significant_count: index,
      value_count: 20,
      noise_floor: 0.017,
      tradeable_count: 1,
      label_names: ['近期将启动上涨'],
      date_range: { start: '2021-09-01', end: '2026-09-11', days: 1219 },
      top_indicator: {
        value_id: 'technical.annual_trend24',
        display_name: '年化趋势斜率',
        ic_mean: 0.111,
        label: 'up_onset_ahead',
        order: 'low',
        significant: true,
      },
    },
  }
}

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
      condition_supported: true,
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
    vi.mocked(swingApi.runSwingIndicatorStudy).mockResolvedValue({
      result: study,
      historyId: 'hist-1',
    })
    vi.mocked(swingApi.fetchSwingResearchHistory).mockResolvedValue([])
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
    expect(wrapper.get('[data-testid="swing-plan-buy"]').text()).toContain('年化趋势斜率 ≤ -0.2739')
    expect(wrapper.get('[data-testid="swing-plan-buy"]').text()).not.toContain('annual_trend24')
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
      return { result: study, historyId: null }
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
      new ApiError('面板需要 5.2 GB 临时空间，超出上限 3 GB', 400),
    )
    const wrapper = mount(SwingResearchPage)
    await wrapper.get('[data-testid="swing-study-run"]').trigger('click')
    await flushPromises()

    expect(wrapper.get('[data-testid="swing-message"]').text()).toContain('超出上限 3 GB')
  })

  it('studies the custom formulas by default and can switch them off', async () => {
    const wrapper = mount(SwingResearchPage)
    const toggle = wrapper.get('[data-testid="swing-study-include-custom"]')
      .element as HTMLInputElement
    expect(toggle.checked).toBe(true)

    await wrapper.get('[data-testid="swing-study-run"]').trigger('click')
    await flushPromises()
    expect(vi.mocked(swingApi.runSwingIndicatorStudy).mock.calls[0]?.[0]?.include_custom).toBe(
      true,
    )

    await wrapper.get('[data-testid="swing-study-include-custom"]').setValue(false)
    await wrapper.get('[data-testid="swing-study-run"]').trigger('click')
    await flushPromises()
    expect(vi.mocked(swingApi.runSwingIndicatorStudy).mock.calls[1]?.[0]?.include_custom).toBe(
      false,
    )
  })

  it('marks a finding that cannot become a rule as reference only', async () => {
    vi.mocked(swingApi.runSwingIndicatorStudy).mockResolvedValue({
      result: {
        ...study,
        condition_plan: {
          ...study.condition_plan!,
          templates: [{
            ...study.condition_plan!.templates[0]!,
            value_id: 'indicator_three.varo7',
            display_name: '准备拉升 · 建仓强度',
            condition_supported: false,
            rule: null,
          }],
          buy_rule: null,
        },
      },
      historyId: null,
    })

    const wrapper = mount(SwingResearchPage)
    await wrapper.get('[data-testid="swing-study-run"]').trigger('click')
    await flushPromises()

    const row = wrapper.get('[data-testid="swing-template-row"]')
    expect(row.text()).toContain('准备拉升 · 建仓强度')
    expect(row.text()).toContain('仅参考')
  })

  it('restores both steps when a saved study is loaded', async () => {
    const record: SwingHistoryRecord = {
      ...historyItem(0),
      history_id: 'hist-1',
      request: {
        universe: 'sh',
        start_date: '2020-01-01',
        end_date: '2025-12-31',
        reversal_multiple: 4,
        min_bars: 7,
        atr_window: 21,
        horizon: 15,
        labels: ['up_onset_ahead'],
        include_custom: false,
        permutations: 50,
        block_size: 10,
        alpha: 0.1,
        min_cross_section: 30,
        limit_stocks: 300,
        overview_request: {
          universe: 'sh',
          startDate: '2020-01-01',
          endDate: '2025-12-31',
          multiplesText: '2, 4',
          minBars: 7,
          atrWindow: 21,
          limitStocks: 300,
        },
        overview_result: overview,
      },
      result: study,
    }
    vi.mocked(swingApi.fetchSwingResearchHistory).mockResolvedValue([record])
    vi.mocked(swingApi.getSwingResearchHistory).mockResolvedValue(record)

    const wrapper = mount(SwingResearchPage)
    await flushPromises()

    expect(wrapper.get('[data-testid="swing-history-item-hist-1"]').text()).toContain('年化趋势斜率')
    await wrapper.get('[data-testid="swing-history-load-hist-1"]').trigger('click')
    await flushPromises()

    expect((wrapper.get('[data-testid="swing-study-multiple"]').element as HTMLInputElement).value).toBe('4')
    expect((wrapper.get('[data-testid="swing-study-horizon"]').element as HTMLInputElement).value).toBe('15')
    expect((wrapper.get('[data-testid="swing-study-start"]').element as HTMLInputElement).value).toBe('2020-01-01')
    // 第一步的表单与结果一起回来了，而不是只剩第二步。
    expect((wrapper.get('[data-testid="swing-overview-multiples"]').element as HTMLInputElement).value).toBe('2, 4')
    expect((wrapper.get('[data-testid="swing-overview-universe"]').element as HTMLSelectElement).value).toBe('sh')
    expect(wrapper.get('[data-testid="swing-overview-result"]').text()).toContain('2021-09-01 → 2026-09-11')
    expect(wrapper.get('[data-testid="swing-study-result"]').text()).toContain('1 / 1')
  })

  it('deletes a saved study and says so', async () => {
    vi.mocked(swingApi.fetchSwingResearchHistory)
      .mockResolvedValueOnce([historyItem(0)])
      .mockResolvedValueOnce([])
    vi.mocked(swingApi.deleteSwingResearchHistory).mockResolvedValue(true)

    const wrapper = mount(SwingResearchPage)
    await flushPromises()
    await wrapper.get('[data-testid="swing-history-remove-hist-0"]').trigger('click')
    await flushPromises()

    expect(swingApi.deleteSwingResearchHistory).toHaveBeenCalledWith('hist-0')
    expect(wrapper.find('[data-testid="swing-history-item-hist-0"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="swing-research-history"]').text()).toContain('暂无历史研究记录')
  })

  it('pages the saved studies ten at a time, newest first', async () => {
    // 后端已按完成时间由近到远排好，这里倒序就是「最近跑的在前」。
    const items = Array.from({ length: 12 }, (_, index) => historyItem(index)).reverse()
    vi.mocked(swingApi.fetchSwingResearchHistory).mockResolvedValue(items)

    const wrapper = mount(SwingResearchPage)
    await flushPromises()

    const rows = wrapper.findAll('[data-testid^="swing-history-item-"]')
    expect(rows).toHaveLength(10)
    expect(rows[0]?.attributes('data-testid')).toBe('swing-history-item-hist-11')

    await wrapper.get('[data-testid="swing-history-next"]').trigger('click')
    await flushPromises()
    expect(wrapper.findAll('[data-testid^="swing-history-item-"]')).toHaveLength(2)
  })

  it('edits every overview and study control, then copies the resulting plan', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })

    const wrapper = mount(SwingResearchPage)
    await flushPromises()

    await wrapper.get('[data-testid="swing-overview-universe"]').setValue('sh')
    await wrapper.get('[data-testid="swing-overview-multiples"]').setValue('2, 3')
    await wrapper.get('[data-testid="swing-overview-min-bars"]').setValue('6')
    await wrapper.get('[data-testid="swing-overview-atr"]').setValue('1.5')
    await wrapper.get('[data-testid="swing-overview-start"]').setValue('2024-01-01')
    await wrapper.get('[data-testid="swing-overview-end"]').setValue('2024-12-31')
    await wrapper.get('[data-testid="swing-overview-limit"]').setValue('600')

    await wrapper.get('[data-testid="swing-study-universe"]').setValue('sz')
    await wrapper.get('[data-testid="swing-study-multiple"]').setValue('4')
    await wrapper.get('[data-testid="swing-study-min-bars"]').setValue('7')
    await wrapper.get('[data-testid="swing-study-atr"]').setValue('1.25')
    await wrapper.get('[data-testid="swing-study-horizon"]').setValue('12')
    await wrapper.get('[data-testid="swing-study-permutations"]').setValue('200')
    await wrapper.get('[data-testid="swing-study-block"]').setValue('10')
    await wrapper.get('[data-testid="swing-study-alpha"]').setValue('0.05')
    await wrapper.get('[data-testid="swing-study-cross"]').setValue('0.4')
    await wrapper.get('[data-testid="swing-study-start"]').setValue('2024-02-01')
    await wrapper.get('[data-testid="swing-study-end"]').setValue('2024-11-30')
    await wrapper.get('[data-testid="swing-study-limit"]').setValue('500')
    // 默认四个都勾，所以这里**取消**勾选才验证得到 toggle 真的生效——
    // 勾一个本来就选中的框是 no-op，即使 toggleLabel 坏了测试也会过。
    await wrapper.get('[data-testid="swing-label-up_leg_start"]').setValue(false)

    await wrapper.get('[data-testid="swing-study-run"]').trigger('click')
    await flushPromises()

    expect(vi.mocked(swingApi.runSwingIndicatorStudy).mock.calls[0][0]).toEqual(expect.objectContaining({
      universe: 'sz',
      reversal_multiple: 4,
      min_bars: 7,
      atr_window: 1.25,
      horizon: 12,
      permutations: 200,
      min_cross_section: 0.4,
      start_date: '2024-02-01',
      end_date: '2024-11-30',
      limit_stocks: 500,
      labels: expect.not.arrayContaining(['up_leg_start']),
    }))

    await wrapper.get('[data-testid="swing-plan-copy"]').trigger('click')
    await flushPromises()

    expect(writeText).toHaveBeenCalledTimes(1)
    expect(wrapper.get('[data-testid="swing-plan-copy"]').text()).toContain('已复制')
  })

  it('checks all four labels by default, pivot ones included', async () => {
    // 只默认勾「近期将启动」时，页面看起来在研究「什么时候启动」，而真正研究顶和底
    // 的两条（波段最低点 / 最高点）藏在不勾的框里——默认全勾才让人一眼看到它们。
    const wrapper = mount(SwingResearchPage)
    await flushPromises()

    const ids = ['up_onset_ahead', 'down_onset_ahead', 'up_leg_start', 'down_leg_start']
    const checked = ids.filter((id) => {
      const input = wrapper.get(`[data-testid="swing-label-${id}"]`).element as HTMLInputElement
      return input.checked
    })

    expect(checked).toEqual(ids)
  })
})
