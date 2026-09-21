import { describe, expect, it } from 'vitest'

import type {
  InstrumentLabCheckItem,
  InstrumentLabLibraryItem,
  InstrumentLabSeries,
} from '../api/instrumentLab'
import {
  buildTradeMarkers,
  differenceTone,
  formatDifferencePoints,
  formatPValue,
  formatPercent,
  libraryOptionLabel,
  percentileText,
  percentileTone,
  selectedValueLabel,
  seriesPoints,
  sideLabel,
  significanceLabel,
  significanceTone,
  summaryText,
} from './instrumentLab'

function item(overrides: Partial<InstrumentLabCheckItem> = {}): InstrumentLabCheckItem {
  return {
    key: 'k',
    label: 'RSI14 ≤ 30',
    hit_count: 40,
    sample_days: 200,
    hit_mean_return: 0.012,
    base_mean_return: 0.002,
    difference: 0.01,
    p_value: 0.02,
    adjusted_p_value: 0.04,
    significant: true,
    hit_dates: ['2024-01-02'],
    note: null,
    ...overrides,
  }
}

function series(overrides: Partial<InstrumentLabSeries> = {}): InstrumentLabSeries {
  return {
    value_id: 'technical.rsi14',
    label: 'RSI14',
    dates: ['2024-01-02', '2024-01-03'],
    values: [30, 40],
    last_value: 40,
    percentile: 83.2,
    note: null,
    ...overrides,
  }
}

describe('读数格式化', () => {
  it('收益带符号，缺失显示占位符', () => {
    expect(formatPercent(0.0131)).toBe('+1.31%')
    expect(formatPercent(-0.02)).toBe('-2.00%')
    expect(formatPercent(null)).toBe('—')
  })

  it('差值用百分点而不是倍数', () => {
    expect(formatDifferencePoints(0.01)).toBe('+1.00 个百分点')
    expect(formatDifferencePoints(-0.005)).toBe('-0.50 个百分点')
    expect(formatDifferencePoints(null)).toBe('—')
  })

  it('p 值过小只给下界', () => {
    expect(formatPValue(0.0004)).toBe('< 0.001')
    expect(formatPValue(0.02)).toBe('0.020')
    expect(formatPValue(null)).toBe('—')
  })

  it('分位写成自身历史 Pxx', () => {
    expect(percentileText(83.2)).toBe('自身历史 P83')
    expect(percentileText(null)).toBe('—')
    expect(percentileTone(90)).toBe('high')
    expect(percentileTone(10)).toBe('low')
    expect(percentileTone(50)).toBe('mid')
    expect(selectedValueLabel(series())).toBe('RSI14 40.000 · 自身历史 P83')
  })

  it('差值方向决定颜色', () => {
    expect(differenceTone(0.01)).toBe('up')
    expect(differenceTone(-0.01)).toBe('down')
    expect(differenceTone(0)).toBe('flat')
    expect(differenceTone(null)).toBe('flat')
  })
})

describe('显著性措辞', () => {
  it('显著 / 没显著 / 没判定说不同的话', () => {
    expect(significanceLabel(item())).toBe('显著')
    expect(significanceLabel(item({ significant: false }))).toBe('和随机挑几天没有区别')
    expect(significanceLabel(item({ significant: null }))).toBe('未做判定')
    expect(significanceLabel(item({ significant: null, hit_count: 0 }))).toBe('没有命中')
    expect(significanceTone(item())).toBe('up')
    expect(significanceTone(item({ significant: false }))).toBe('muted')
    expect(significanceTone(item({ significant: null }))).toBe('warn')
  })

  it('汇总句把命中、差值、p 值都带上', () => {
    expect(summaryText(item())).toContain('命中 40 / 200 个交易日')
    expect(summaryText(item())).toContain('校正后')
    expect(summaryText(item({ hit_count: 0, difference: null }))).toBe('该标的上从未命中这个条件')
  })
})

describe('条件库与成交标记', () => {
  const libraryItem: InstrumentLabLibraryItem = {
    key: 'technical.rsi14|lte||30',
    label: 'RSI14 ≤ 30',
    side: 'buy',
    condition: {
      left_value: 'technical.rsi14',
      operator: 'lte',
      right_value: null,
      right_constant: 30,
    },
    lift: 1.8,
    pvalue: 0.01,
    support_count: 300,
    instrument_count: 120,
    history_id: 'h1',
    created_at: '2026-09-01T10:00:00',
    scanned_stocks: 500,
  }

  it('选项文案带上提升与命中次数', () => {
    expect(libraryOptionLabel(libraryItem)).toBe('RSI14 ≤ 30 · 提升 1.80 倍 · 命中 300 次')
    expect(sideLabel('buy')).toBe('买入侧')
    expect(sideLabel('sell')).toBe('卖出侧')
    expect(sideLabel(null)).toBe('—')
  })

  it('成交记录只保留日期与方向', () => {
    const markers = buildTradeMarkers([
      { datetime: '2024-01-02 00:00:00', direction: 'buy', price: 10 },
      { datetime: '2024-01-09 00:00:00', direction: 'sell', price: 11 },
      { datetime: '2024-01-16 00:00:00', direction: 'other' },
      { price: 9 },
    ])
    expect(markers).toEqual([
      { date: '2024-01-02', side: 'buy' },
      { date: '2024-01-09', side: 'sell' },
    ])
  })

  it('序列转坐标点时缺值变成 NaN 让折线断开', () => {
    const points = seriesPoints([series({ values: [1, null, 3] })])
    expect(points[0].map((point) => point.y)).toEqual([1, NaN, 3])
  })
})
