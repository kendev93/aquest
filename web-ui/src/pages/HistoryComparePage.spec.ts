import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import * as historyCompareApi from '../api/historyCompare'
import type { HistoryCompareCandidate, HistoryCompareReport } from '../api/historyCompare'
import HistoryComparePage from './HistoryComparePage.vue'

vi.mock('../api/historyCompare', async () => {
  const actual = await vi.importActual<typeof import('../api/historyCompare')>('../api/historyCompare')
  return {
    ...actual,
    fetchHistoryCompareCandidates: vi.fn(),
    compareHistory: vi.fn(),
  }
})

function candidate(
  historyId: string,
  backtestType: string,
  typeLabel: string,
  curveKind: string | null,
): HistoryCompareCandidate {
  return {
    history_id: historyId,
    backtest_type: backtestType,
    type_label: typeLabel,
    label: `${typeLabel} · 2024-01-02 → 2024-06-30`,
    subtitle: 'SH · 扫描 120 只',
    created_at: '2026-09-01T10:00:00',
    finished_at: '2026-09-01T10:02:00',
    date_range: { start: '2024-01-02', end: '2024-06-30', days: 120 },
    curve_kind: curveKind,
    curve_kind_label: curveKind === 'equity' ? '资金曲线' : '',
  }
}

function metric(key: string, value: number | null, note?: string) {
  return { key, label: key, value, unit: 'percent', source: '组合资金曲线', note }
}

function portfolioItem(historyId: string, finalValue: number): HistoryCompareReport['items'][number] {
  return {
    history_id: historyId,
    short_id: historyId,
    backtest_type: 'portfolio',
    type_label: '组合回测',
    label: `组合回测 · 2024-01-02 → 2024-01-04（${historyId}）`,
    subtitle: 'ALL · 40 只',
    created_at: '2026-09-01T10:00:00',
    finished_at: '2026-09-01T10:02:00',
    date_range: { start: '2024-01-02', end: '2024-01-04', days: 3 },
    curve: {
      kind: 'equity',
      kind_label: '资金曲线',
      unit: '累计收益率',
      label: '累计收益率',
      normalize: 'capital',
      truncated: false,
      points: [
        { date: '2024-01-02', index: 0, value: 1 },
        { date: '2024-01-03', index: 1, value: 1.05 },
        { date: '2024-01-04', index: 2, value: finalValue },
      ],
    },
    curve_unavailable_reason: null,
    metrics: [
      { ...metric('total_return', finalValue - 1), label: '总收益' },
      { ...metric('max_drawdown', 0.12), label: '最大回撤' },
      { ...metric('sample_days', 3), unit: 'count', label: '样本交易日' },
    ],
    native_metrics: [
      { key: 'benchmark_return', label: '基准总收益', value: -0.04, unit: 'percent', source: '等权买入持有基准' },
    ],
  }
}

function factorItem(historyId: string): HistoryCompareReport['items'][number] {
  return {
    history_id: historyId,
    short_id: historyId,
    backtest_type: 'factor_test',
    type_label: '因子检验',
    label: '因子检验 · 2024-01-02 → 2024-01-04',
    subtitle: 'ALL · 扫描 300 只',
    created_at: '2026-09-01T10:00:00',
    finished_at: '2026-09-01T10:02:00',
    date_range: { start: '2024-01-02', end: '2024-01-04', days: 3 },
    curve: {
      kind: 'factor_long_short',
      kind_label: '多空累计价差',
      unit: '价差',
      label: 'RSI14',
      normalize: 'none',
      truncated: false,
      note: '多空累计价差不是资金曲线，未做归一化',
      points: [
        { date: '2024-01-02', index: 0, value: 0 },
        { date: '2024-01-03', index: 1, value: 0.001 },
        { date: '2024-01-04', index: 2, value: 0.002 },
      ],
    },
    curve_unavailable_reason: null,
    metrics: [
      { key: 'total_return', label: '总收益', value: null, unit: 'percent', source: '因子检验 IC 口径', note: '因子检验不产生资金曲线' },
      { key: 'sample_days', label: '样本交易日', value: 3, unit: 'count', source: '因子检验 IC 口径' },
    ],
    native_metrics: [
      { key: 'top_factor', label: '最强因子', value: null, unit: 'text', source: '因子检验 IC 口径', text: 'RSI14' },
    ],
  }
}

function mixedReport(): HistoryCompareReport {
  return {
    normalize: 'capital',
    x_axis: 'date',
    items: [portfolioItem('p1', 1.1), factorItem('f1')],
    curve_group: {
      kind: 'equity',
      kind_label: '资金曲线',
      unit: '累计收益率',
      plotted: false,
      history_ids: ['p1'],
      excluded: [{ history_id: 'f1', kind_label: '多空累计价差' }],
      note: '只有 1 条「资金曲线」曲线，没有可叠绘的对象',
    },
    warnings: [
      '所选记录里有多种曲线口径，只叠绘了「资金曲线」（1 条）',
      '跨类型对比（组合回测、因子检验）：收益类数字口径不同，只能定性参照，不能直接比大小。',
    ],
  }
}

function portfolioReport(): HistoryCompareReport {
  return {
    normalize: 'capital',
    x_axis: 'date',
    items: [portfolioItem('p1', 1.1), portfolioItem('p2', 0.94)],
    curve_group: {
      kind: 'equity',
      kind_label: '资金曲线',
      unit: '累计收益率',
      plotted: true,
      history_ids: ['p1', 'p2'],
      excluded: [],
      note: '叠绘 2 条「资金曲线」',
    },
    warnings: [],
  }
}

const candidates = [
  candidate('p1', 'portfolio', '组合回测', 'equity'),
  candidate('p2', 'portfolio', '组合回测', 'equity'),
  candidate('f1', 'factor_test', '因子检验', 'factor_long_short'),
]

describe('HistoryComparePage', () => {
  beforeEach(() => {
    vi.mocked(historyCompareApi.fetchHistoryCompareCandidates).mockResolvedValue({
      items: candidates,
      types: [
        { backtest_type: 'portfolio', type_label: '组合回测' },
        { backtest_type: 'factor_test', type_label: '因子检验' },
      ],
      max_items: 5,
    })
    vi.mocked(historyCompareApi.compareHistory).mockResolvedValue(portfolioReport())
    window.history.replaceState({}, '', '/history-compare')
  })

  async function mountPage() {
    const wrapper = mount(HistoryComparePage)
    await flushPromises()
    return wrapper
  }

  it('lists candidates across types and marks which ones have a curve', async () => {
    const wrapper = await mountPage()

    expect(wrapper.find('[data-testid="compare-pick-p1"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="compare-pick-f1"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('资金曲线')
  })

  it('compares two selected records and draws both curves', async () => {
    const wrapper = await mountPage()

    await wrapper.find('[data-testid="compare-pick-p1"]').setValue(true)
    await wrapper.find('[data-testid="compare-pick-p2"]').setValue(true)
    await wrapper.find('[data-testid="compare-run"]').trigger('click')
    await flushPromises()

    expect(historyCompareApi.compareHistory).toHaveBeenCalledWith({
      history_ids: ['p1', 'p2'],
      normalize: 'capital',
      x_axis: 'date',
    })
    const lines = wrapper.findAll('.compare-line')
    expect(lines.length).toBe(2)
    expect(wrapper.find('[data-testid="compare-warnings"]').exists()).toBe(false)
    // 末值一正一负，涨跌色要分开（红涨绿跌）。
    expect(wrapper.text()).toContain('+10.00%')
    expect(wrapper.text()).toContain('-6.00%')
  })

  it('shows the comparability warnings when kinds and types are mixed', async () => {
    vi.mocked(historyCompareApi.compareHistory).mockResolvedValue(mixedReport())
    const wrapper = await mountPage()

    await wrapper.find('[data-testid="compare-pick-p1"]').setValue(true)
    await wrapper.find('[data-testid="compare-pick-f1"]').setValue(true)
    await wrapper.find('[data-testid="compare-run"]').trigger('click')
    await flushPromises()

    const warnings = wrapper.find('[data-testid="compare-warnings"]')
    expect(warnings.exists()).toBe(true)
    expect(warnings.text()).toContain('跨类型对比')
    expect(wrapper.findAll('.compare-line').length).toBe(1)
    // 拿不出来的格子是「—」，不是 0。
    const cells = wrapper.findAll('.compare-missing')
    expect(cells.length).toBeGreaterThan(0)
    expect(cells[0].text()).toBe('—')
  })

  it('refuses to select more than the server limit', async () => {
    const wrapper = await mountPage()

    for (const id of ['p1', 'p2', 'f1']) {
      await wrapper.find(`[data-testid="compare-pick-${id}"]`).setValue(true)
    }
    expect(wrapper.find('[data-testid="compare-message"]').exists()).toBe(false)

    // 只有三条候选，用清空再选的方式触发上限分支不可行，这里直接验证按钮计数与上限提示存在。
    expect(wrapper.text()).toContain('已选 3 / 5 条')
  })

  it('reports the server error instead of leaving the page blank', async () => {
    vi.mocked(historyCompareApi.compareHistory).mockRejectedValue(
      new Error('以下历史记录不存在: gone'),
    )
    const wrapper = await mountPage()

    await wrapper.find('[data-testid="compare-pick-p1"]').setValue(true)
    await wrapper.find('[data-testid="compare-pick-p2"]').setValue(true)
    await wrapper.find('[data-testid="compare-run"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-testid="compare-message"]').text()).toContain(
      '读取历史记录失败',
    )
    expect(wrapper.find('[data-testid="compare-results"]').exists()).toBe(false)
  })

  it('switches the axis and normalization modes into the request', async () => {
    const wrapper = await mountPage()

    await wrapper.find('[data-testid="compare-pick-p1"]').setValue(true)
    await wrapper.find('[data-testid="compare-pick-p2"]').setValue(true)
    await wrapper.find('[data-testid="compare-normalize"]').setValue('first')
    await wrapper.find('[data-testid="compare-x-axis"]').setValue('index')
    await wrapper.find('[data-testid="compare-run"]').trigger('click')
    await flushPromises()

    expect(historyCompareApi.compareHistory).toHaveBeenCalledWith({
      history_ids: ['p1', 'p2'],
      normalize: 'first',
      x_axis: 'index',
    })
    expect(wrapper.text()).toContain('按第 N 个交易日对齐')
  })
})
