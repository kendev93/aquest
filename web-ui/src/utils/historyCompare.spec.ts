import { describe, expect, it } from 'vitest'

import type { HistoryCompareItem } from '../api/historyCompare'
import {
  axisNote,
  buildDateAxis,
  buildSeries,
  colorFor,
  dateRangeText,
  formatLastValue,
  formatMetricValue,
  formatNativeValue,
  isEquityCurve,
  lastValueTone,
  metricCell,
  metricTitle,
  normalizeNote,
  seriesLabel,
} from './historyCompare'

function item(overrides: Partial<HistoryCompareItem> = {}): HistoryCompareItem {
  return {
    history_id: 'p1',
    short_id: 'p1',
    backtest_type: 'portfolio',
    type_label: '组合回测',
    label: '组合回测 · 2024-01-02 → 2024-01-04',
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
        { date: '2024-01-04', index: 1, value: 1.12 },
      ],
    },
    curve_unavailable_reason: null,
    metrics: [
      { key: 'total_return', label: '总收益', value: 0.12, unit: 'percent', source: '组合资金曲线' },
      {
        key: 'sample_days',
        label: '样本交易日',
        value: 3.4,
        unit: 'count',
        source: '组合资金曲线',
      },
      {
        key: 'max_drawdown',
        label: '最大回撤',
        value: null,
        unit: 'percent',
        source: '因子检验 IC 口径',
        note: '因子检验不产生资金曲线',
      },
    ],
    native_metrics: [
      { key: 'top_factor', label: '最强因子', value: null, unit: 'text', source: '因子检验', text: 'RSI14' },
      { key: 'top_ic', label: '最强 IC', value: 0.0312, unit: 'ratio', source: '因子检验' },
    ],
    ...overrides,
  }
}

describe('history compare helpers', () => {
  it('formats values by unit and never invents a number', () => {
    const record = item()
    expect(formatMetricValue(metricCell(record, 'total_return'))).toBe('12.00%')
    expect(formatMetricValue(metricCell(record, 'sample_days'))).toBe('3')
    expect(formatMetricValue(metricCell(record, 'max_drawdown'))).toBe('—')
    expect(formatMetricValue(undefined)).toBe('—')
  })

  it('explains where a missing number would have come from', () => {
    const record = item()
    expect(metricTitle(record, 'max_drawdown')).toContain('因子检验不产生资金曲线')
    expect(metricTitle(record, 'total_return')).toBe('组合资金曲线')
    expect(metricTitle(record, 'missing')).toBe('')
  })

  it('renders text metrics as text', () => {
    const record = item()
    expect(formatNativeValue(record.native_metrics[0])).toBe('RSI14')
    expect(formatNativeValue(record.native_metrics[1])).toBe('0.031')
  })

  it('aligns different-length curves on a shared date axis', () => {
    const long = item()
    const short = item({
      history_id: 'p2',
      curve: {
        kind: 'equity',
        kind_label: '资金曲线',
        unit: '累计收益率',
        label: '累计收益率',
        normalize: 'capital',
        truncated: false,
        points: [{ date: '2024-01-04', index: 0, value: 0.94 }],
      },
    })

    const axis = buildDateAxis([long, short])
    expect(axis.get('2024-01-02')).toBe(0)
    expect(axis.get('2024-01-04')).toBe(1)

    const byDate = buildSeries([long, short], { xAxis: 'date', isEquity: true, axis })
    expect(byDate[0].map((point) => point.x)).toEqual([0, 1])
    expect(byDate[1].map((point) => point.x)).toEqual([1])
    expect(byDate[0][1].y).toBeCloseTo(12, 6)

    const byIndex = buildSeries([long, short], { xAxis: 'index', isEquity: true, axis })
    expect(byIndex[1].map((point) => point.x)).toEqual([0])
  })

  it('keeps raw values for non-equity curves', () => {
    const factor = item({
      curve: {
        kind: 'factor_ic',
        kind_label: '累计 IC',
        unit: '累计 IC',
        label: 'RSI14',
        normalize: 'none',
        truncated: false,
        points: [
          { date: '2024-01-02', index: 0, value: 0.01 },
          { date: '2024-01-04', index: 1, value: 0.02 },
        ],
      },
    })

    const series = buildSeries([factor], { xAxis: 'date', isEquity: false })
    expect(series[0].map((point) => point.y)).toEqual([0.01, 0.02])
    expect(isEquityCurve('factor_ic')).toBe(false)
    expect(isEquityCurve('equity')).toBe(true)
  })

  it('shows profit and loss only for return curves', () => {
    const winner = item()
    const loser = item({
      history_id: 'p2',
      curve: {
        kind: 'equity',
        kind_label: '资金曲线',
        unit: '累计收益率',
        label: '累计收益率',
        normalize: 'capital',
        truncated: false,
        points: [{ date: '2024-01-04', index: 0, value: 0.94 }],
      },
    })

    expect(formatLastValue(winner, true)).toBe('+12.00%')
    expect(formatLastValue(loser, true)).toBe('-6.00%')
    expect(lastValueTone(winner, true)).toBe('positive')
    expect(lastValueTone(loser, true)).toBe('negative')
    // IC 口径没有盈亏方向，不该染成涨跌色。
    expect(lastValueTone(winner, false)).toBe('')
    expect(formatLastValue(winner, false)).toBe('1.120')
  })

  it('handles records without a curve', () => {
    const bare = item({ curve: null, curve_unavailable_reason: '条件研究不产生任何曲线' })
    expect(formatLastValue(bare, true)).toBe('—')
    expect(lastValueTone(bare, true)).toBe('')
    expect(seriesLabel(bare)).toBe(`${bare.label} · p1`)
  })

  it('describes the axis, normalization, range and colors', () => {
    const record = item()
    expect(axisNote('date')).toContain('按日期')
    expect(axisNote('index')).toContain('第 N 个交易日')
    expect(normalizeNote('capital', 'equity')).toContain('除以本金')
    expect(normalizeNote('first', 'equity')).toContain('除以首值')
    expect(normalizeNote('capital', null)).toBe('')
    expect(dateRangeText(record)).toBe('2024-01-02 → 2024-01-04（3 个交易日）')
    expect(dateRangeText(item({ date_range: { start: null, end: null, days: null } }))).toBe(
      '区间未记录',
    )
    expect(colorFor([record], 'p1')).toMatch(/^#/)
    expect(colorFor([], 'nope')).toMatch(/^#/)
    expect(seriesLabel(record)).toContain('累计收益率')
    expect(seriesLabel(record)).toContain('p1')
  })
})
