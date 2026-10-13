import { describe, expect, it } from 'vitest'

import {
  formatCount,
  formatDuration,
  formatMoney,
  formatNumber,
  formatPercent,
  formatSignedPercent,
  formatTradeRatio,
} from './format'

describe('formatNumber', () => {
  it('formats finite numbers with the requested digits', () => {
    expect(formatNumber(1.2345)).toBe('1.23')
    expect(formatNumber(1.2345, 3)).toBe('1.234')
    expect(formatNumber(0, 0)).toBe('0')
    expect(formatNumber(-2.5)).toBe('-2.50')
  })

  it('renders everything that is not a finite number as an em dash', () => {
    expect(formatNumber(null)).toBe('—')
    expect(formatNumber(undefined)).toBe('—')
    expect(formatNumber(Number.NaN)).toBe('—')
    expect(formatNumber(Number.POSITIVE_INFINITY)).toBe('—')
    expect(formatNumber('1.23')).toBe('—')
  })
})

describe('formatPercent', () => {
  it('multiplies a decimal ratio by 100', () => {
    expect(formatPercent(0.1234)).toBe('12.34%')
    expect(formatPercent(0.12345, 1)).toBe('12.3%')
    expect(formatPercent(-0.05)).toBe('-5.00%')
    expect(formatPercent(Number.NaN)).toBe('—')
    expect(formatPercent(null)).toBe('—')
  })
})

describe('formatSignedPercent', () => {
  it('prefixes positive values with a plus sign', () => {
    expect(formatSignedPercent(0.1234)).toBe('+12.34%')
    expect(formatSignedPercent(-0.1234)).toBe('-12.34%')
    expect(formatSignedPercent(0)).toBe('0.00%')
    expect(formatSignedPercent(null)).toBe('—')
  })
})

describe('formatCount', () => {
  it('rounds to an integer', () => {
    expect(formatCount(12.6)).toBe('13')
    expect(formatCount(12.2)).toBe('12')
    expect(formatCount(null)).toBe('—')
  })
})

describe('formatMoney', () => {
  it('renders yuan with thousands separators and two decimals', () => {
    expect(formatMoney(1234567.891)).toBe('¥1,234,567.89')
    expect(formatMoney(0)).toBe('¥0.00')
    expect(formatMoney(null)).toBe('—')
  })
})

describe('formatDuration', () => {
  it('stays in seconds below one minute', () => {
    expect(formatDuration(12.34)).toBe('12.3 秒')
    expect(formatDuration(59.96)).toBe('60.0 秒')
  })

  it('switches to minutes and hours with the documented wording', () => {
    expect(formatDuration(60)).toBe('1 分钟')
    expect(formatDuration(63)).toBe('1 分 3 秒')
    expect(formatDuration(3600)).toBe('1 小时')
    expect(formatDuration(3900)).toBe('1 小时 5 分钟')
  })

  it('rejects negative and missing values', () => {
    expect(formatDuration(-1)).toBe('—')
    expect(formatDuration(null)).toBe('—')
    expect(formatDuration(Number.NaN)).toBe('—')
  })
})

describe('formatTradeRatio', () => {
  it('零成交一律占位符：没亏过不等于赚过', () => {
    expect(formatTradeRatio('profit_factor', { value: 999, totalTrades: 0, loseTrades: 0 })).toBe('—')
    expect(formatTradeRatio('payoff_ratio', { value: null, totalTrades: null, loseTrades: 0 })).toBe('—')
  })

  it('两种「没有亏损」都认：除零给 null 与盈利因子的 999 哨兵', () => {
    expect(formatTradeRatio('payoff_ratio', { value: null, totalTrades: 10, loseTrades: 0 })).toBe('无亏损')
    expect(formatTradeRatio('profit_factor', { value: null, totalTrades: 10, loseTrades: 0 })).toBe('无亏损')
    expect(formatTradeRatio('profit_factor', { value: 999, totalTrades: 10, loseTrades: 0 })).toBe('无亏损')
  })

  it('指标缺失但确有亏损时不谎报', () => {
    expect(formatTradeRatio('payoff_ratio', { value: null, totalTrades: 10, loseTrades: 4 })).toBe('—')
    expect(formatTradeRatio('profit_factor', { value: null, totalTrades: 10, loseTrades: null })).toBe('—')
  })

  it('哨兵只管盈利因子：真实盈亏比再大也照样显示数字', () => {
    expect(formatTradeRatio('payoff_ratio', { value: 1500, totalTrades: 10, loseTrades: 1 })).toBe('1500.00')
    expect(formatTradeRatio('payoff_ratio', { value: 2, totalTrades: 10, loseTrades: 4 })).toBe('2.00')
    expect(formatTradeRatio('profit_factor', { value: 1.6, totalTrades: 10, loseTrades: 4 })).toBe('1.60')
  })
})
