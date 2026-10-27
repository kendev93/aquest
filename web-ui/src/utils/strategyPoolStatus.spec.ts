import { describe, expect, it } from 'vitest'

import type {
  HoldingRiskDisplay,
  StrategyPoolCandidate,
  StrategyPoolEntry,
  StrategyPoolSignal,
} from '../types'
import {
  benchmarkReturn,
  entryHoldingRisk,
  entryIsHeld,
  entryStatus,
  formatHoldingDays,
  holdingExpiryText,
  holdingRiskLevelText,
  holdingRiskSummary,
  metricRawValue,
  metricValue,
  objectiveLabel,
  rerunEntryDisabled,
  rerunEntryTitle,
  sortPoolEntriesByMetric,
  sortPoolEntriesByStatus,
  statusLabel,
  statusOrder,
  symbol,
  tradeCount,
  fullPeriodAsOf,
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
    expect(metricValue(row, 'strategy_return')).toBe(0.71)
    expect(metricValue(row, 'annual_return')).toBe(0.19)
    expect(metricValue(row, 'sharpe')).toBe(Number.NEGATIVE_INFINITY)
    expect(metricValue(entry('600000', { created_return: 0.1 }), 'created_return')).toBe(0.1)
    // 取不到值时原始口径给出 null，排序用它把缺值标的沉底；metricValue 仍返回 -Infinity 供表格兜底显示。
    expect(metricRawValue(row, 'sharpe')).toBeNull()
    expect(metricRawValue(row, 'created_return')).toBeNull()
    expect(metricRawValue(row, 'annual_return')).toBe(0.19)
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

  it('indexes the daily states without changing who lands where', () => {
    // 排序改成「先给状态表建索引」，那结果必须和原来逐个 find 完全一致：同一个代码
    // 出现两行时取第一条，没有可回放候选的标的按条目自己的状态。
    const rows = [entry('600001'), entry('600002'), entry('600003', { active_candidate: null })]
    const states = [
      state('600001', 'buy'),
      state('600001', 'sell', 'other-candidate'),
      state('600002', 'no_strategy'),
    ]
    const held = new Set(['SH600003'])
    const naive = [...rows].sort((a, b) => {
      const order = statusOrder(entryStatus(a, states), entryIsHeld(a, held))
        - statusOrder(entryStatus(b, states), entryIsHeld(b, held))
      return order || symbol(a).localeCompare(symbol(b))
    }).map(symbol)

    expect(sortPoolEntriesByStatus(rows, states, held, null).map(symbol)).toEqual(naive)
    // 指标全都取不到值时，指标档退到同一条状态兜底链，结果同样一致。
    expect(sortPoolEntriesByMetric(rows, 'sharpe', 'desc', states, held, null).map(symbol))
      .toEqual(naive)
  })

  it('sorts a metric column in both directions and keeps rows without the metric last', () => {
    const high = entry('600001', { created_return: 0.3 })
    const low = entry('600002', { created_return: 0.1 })
    // 创建至今收益还没初始化（待重跑或更新失败）的标的。
    const waiting = entry('600003', { created_return: null })
    const states = [state('600001', 'buy')]
    const held = new Set<string>()
    const input = [waiting, low, high]
    const orderOf = (direction: 'asc' | 'desc') => sortPoolEntriesByMetric(
      input, 'created_return', direction, states, held, 'annual_return',
    ).map(symbol)

    expect(orderOf('desc')).toEqual(['SH600001', 'SH600002', 'SH600003'])
    // 升序只翻转指标高低，缺值仍然沉底：否则第一页全是还没有策略的票。
    expect(orderOf('asc')).toEqual(['SH600002', 'SH600001', 'SH600003'])
    // 同值时仍按状态优先级兜底，与 status 档共用一条兜底链。
    expect(sortPoolEntriesByMetric(
      [entry('600010', { created_return: 0.3 }), high],
      'created_return', 'asc', states, held, 'annual_return',
    ).map(symbol)).toEqual(['SH600001', 'SH600010'])
    expect(input.map(symbol)).toEqual(['SH600003', 'SH600002', 'SH600001'])
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

describe('持仓风控展示', () => {
  const holding: HoldingRiskDisplay = {
    stop_loss_price: 9,
    trailing_stop_price: null,
    take_profit_price: null,
    risk_level_as_of: '2026-10-16',
    pending_exit_reason: null,
    pending_exit_signal_date: null,
    pending_exit_risk_trigger: null,
    max_hold_days: null,
    max_hold_days_left: null,
    max_hold_expiry_date: null,
  }

  it('只列出真正生效的线，并说明判定用的是哪一天', () => {
    expect(holdingRiskLevelText(holding)).toBe('止损 9.00（截至 2026-10-16 收盘）')
    expect(holdingRiskLevelText({ ...holding, trailing_stop_price: 11.4, take_profit_price: 12 })).toBe(
      '止损 9.00 · 移动止盈 11.40 · 止盈 12.00（截至 2026-10-16 收盘）',
    )
    // 未启用的规则不能显示成 0，那会被读成「跌到 0 才止损」。
    expect(holdingRiskLevelText({ ...holding, stop_loss_price: null })).toBe('未设置风控线')
    expect(holdingRiskLevelText({ ...holding, risk_level_as_of: null })).toBe('止损 9.00')
  })

  it('到期日没有行情依据时只报剩余交易日', () => {
    expect(holdingExpiryText(holding)).toBe('')
    expect(
      holdingExpiryText({ ...holding, max_hold_days: 20, max_hold_days_left: 3, max_hold_expiry_date: '2026-11-06' }),
    ).toBe('2026-11-06 到期 · 还剩 3 个交易日')
    expect(holdingExpiryText({ ...holding, max_hold_days: 20, max_hold_days_left: 9 })).toBe('还剩 9 个交易日到期')
    expect(
      holdingExpiryText({ ...holding, max_hold_days: 20, max_hold_days_left: 0, max_hold_expiry_date: '2026-11-06' }),
    ).toBe('今日收盘满持有期限 · 下一交易日开盘卖出')
  })

  it('把风控线和持有到期拼成一行', () => {
    expect(holdingRiskSummary(holding)).toBe('止损 9.00（截至 2026-10-16 收盘）')
    expect(holdingRiskSummary({ ...holding, max_hold_days: 20, max_hold_days_left: 9 })).toBe(
      '止损 9.00（截至 2026-10-16 收盘） · 还剩 9 个交易日到期',
    )
  })

  it('全区间指标的截至日只在有最新口径时给出', () => {
    expect(fullPeriodAsOf(entry('600000'))).toBeNull()
    expect(fullPeriodAsOf(entry('600001', { full_period_as_of_date: '2026-10-27' }))).toBe(
      '2026-10-27',
    )
  })

  it('从条目的跟踪快照里取当前持仓行', () => {
    expect(entryHoldingRisk(entry('600000'))).toBeNull()
    const tracked = entry('600001', {
      tracking: { summary: { holding: [{ ...holding, market: 'SH', code: '600001' }] } },
    })
    expect(entryHoldingRisk(tracked)?.stop_loss_price).toBe(9)
    expect(entryHoldingRisk(tracked)?.pending_exit_risk_trigger).toBeNull()
    expect(entryHoldingRisk(entry('600002', { tracking: { summary: { holding: [] } } }))).toBeNull()
  })
})
