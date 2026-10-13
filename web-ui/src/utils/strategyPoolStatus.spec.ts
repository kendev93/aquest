import { describe, expect, it } from 'vitest'

import type { StrategyPoolCandidate, StrategyPoolEntry, StrategyPoolSignal } from '../types'
import {
  benchmarkReturn,
  entryIsHeld,
  entryStatus,
  formatHoldingDays,
  metricValue,
  objectiveLabel,
  rerunEntryDisabled,
  rerunEntryTitle,
  sortPoolEntriesByStatus,
  statusLabel,
  statusOrder,
  symbol,
  tradeCount,
} from './strategyPoolStatus'

const candidate = {
  candidate_id: 'candidate-600000',
  buy_rule: { signals: ['technical.macd'] },
  sell_rule: { signals: ['technical.rsi14'] },
  config: { stop_loss_pct: 0.08 },
  metrics: {
    validation: { annual_return: 0.24, sharpe: 1.35, closed_trades: 12 },
    holdout: { total_trades: 4 },
    full_period: { total_return: 0.71, annual_return: 0.19, avg_holding_days: 12.5 },
  },
} as unknown as StrategyPoolCandidate

function entry(code: string, overrides: Partial<StrategyPoolEntry> = {}): StrategyPoolEntry {
  return {
    market: 'SH',
    code,
    name: `测试${code}`,
    automatic_candidates: { annual_return: candidate, sharpe: candidate },
    manual_overrides: { annual_return: null, sharpe: null },
    active_candidate: candidate,
    active_source: 'automatic',
    status: 'ready',
    benchmark: { full_period: { total_return: 0.5 } },
    ...overrides,
  } as StrategyPoolEntry
}

function state(code: string, status: string, candidateId = candidate.candidate_id): StrategyPoolSignal {
  return { symbol: `SH${code}`, status, candidate_id: candidateId } as StrategyPoolSignal
}

describe('strategyPoolStatus', () => {
  it('builds the market+code key and the status labels', () => {
    expect(symbol(entry('600000'))).toBe('SH600000')
    expect(statusLabel('buy_sell')).toBe('买卖均触发')
    expect(statusLabel('unknown_state')).toBe('unknown_state')
    expect(objectiveLabel('sharpe')).toBe('夏普')
    expect(objectiveLabel('annual_return')).toBe('年化收益')
    expect(formatHoldingDays(12.53)).toBe('12.5 天')
    expect(formatHoldingDays(Number.NaN)).toBe('—')
  })

  it('orders actionable statuses before waiting ones', () => {
    expect(statusOrder('buy')).toBeLessThan(statusOrder('sell'))
    expect(statusOrder('sell')).toBeLessThan(statusOrder('flat'))
    // 持仓中没有买卖信号的标的单独成档，排在「今日无买卖信号」之前。
    expect(statusOrder('flat', true)).toBe(2)
    expect(statusOrder('flat')).toBe(3)
    expect(statusOrder('no_strategy', true)).toBe(5)
    expect(statusOrder('error')).toBe(6)
  })

  it('marks a symbol stale when the cached signal row belongs to another candidate', () => {
    const row = entry('600000')
    expect(entryStatus(row, [state('600000', 'buy')])).toBe('buy')
    expect(entryStatus(row, [state('600000', 'held')])).toBe('flat')
    expect(entryStatus(row, [state('600000', 'buy', 'other')])).toBe('signal_stale')
    expect(entryStatus(row, [])).toBe('signal_stale')
    // 没有可回放候选时，状态回到条目自身。
    expect(entryStatus(
      entry('600000', { active_candidate: null }),
      [state('600000', 'buy')],
    )).toBe('no_strategy')
    expect(entryStatus(
      entry('600000', { active_candidate: null, status: 'insufficient_history' }),
      [],
    )).toBe('insufficient_history')
  })

  it('reads full-period metrics and the buy-and-hold baseline', () => {
    const row = entry('600000')
    expect(metricValue(row, 'strategy_return', 'annual_return')).toBe(0.71)
    expect(metricValue(row, 'annual_return', 'annual_return')).toBe(0.19)
    expect(metricValue(row, 'sharpe', 'annual_return')).toBe(Number.NEGATIVE_INFINITY)
    expect(metricValue(entry('600000', { created_return: 0.1 }), 'created_return', 'sharpe')).toBe(0.1)
    expect(benchmarkReturn(row)).toBe(0.5)
    expect(benchmarkReturn(entry('600000', { benchmark: null }))).toBeNull()
    expect(tradeCount(candidate, 'validation')).toBe(12)
    expect(tradeCount(candidate, 'holdout')).toBe(4)
  })

  it('sorts by status first, then by the objective metric, then by symbol', () => {
    const buy = entry('600001')
    const stale = entry('600002')
    const states = [state('600001', 'buy')]
    const held = new Set(['SH600002'])

    const input = [stale, buy]
    const sorted = sortPoolEntriesByStatus(input, states, held, 'annual_return')

    expect(sorted.map(symbol)).toEqual(['SH600001', 'SH600002'])
    expect(entryIsHeld(stale, held)).toBe(true)
    expect(entryIsHeld(buy, held)).toBe(false)
    // 不改动入参数组：调用方还在用同一份 entries。
    expect(input.map(symbol)).toEqual(['SH600002', 'SH600001'])
  })

  it('gates and labels the per-symbol rerun button', () => {
    const row = entry('600000')
    const idle = { queuedSymbol: '', busy: false, busyIntent: '' }
    expect(rerunEntryDisabled(idle)).toBe(false)
    expect(rerunEntryTitle(row, idle)).toBe('重跑 SH 600000')

    expect(rerunEntryDisabled({ ...idle, queuedSymbol: 'SH600000' })).toBe(true)
    expect(rerunEntryTitle(row, { ...idle, queuedSymbol: 'SH600001' })).toBe('已有逐股重跑在排队中')

    expect(rerunEntryDisabled({ ...idle, busy: true, busyIntent: 'signals' })).toBe(false)
    expect(rerunEntryTitle(row, { ...idle, busy: true, busyIntent: 'signals' }))
      .toBe('点击后排在当天信号扫描之后重跑')

    expect(rerunEntryDisabled({ ...idle, busy: true, busyIntent: 'full' })).toBe(true)
    expect(rerunEntryTitle(row, { ...idle, busy: true, busyIntent: 'full' }))
      .toBe('当前全市场研究或组合回测结束后可重跑')
  })
})
