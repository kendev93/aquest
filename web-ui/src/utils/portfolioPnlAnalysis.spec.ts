import { describe, expect, it } from 'vitest'

import type { PortfolioBacktestResult, PortfolioTrade } from '../types'
import { analyzePortfolioPnl, pairRoundTrips } from './portfolioPnlAnalysis'

function trade(
  overrides: Partial<PortfolioTrade> & Pick<PortfolioTrade, 'date' | 'direction' | 'size'>,
): PortfolioTrade {
  return {
    signal_date: overrides.date,
    market: 'SZ',
    code: '000001',
    name: '平安银行',
    price: 10,
    commission: 0,
    stamp_tax: 0,
    slippage: 0,
    pnl: 0,
    cost_basis: 0,
    reason: '排名入选',
    rejected: false,
    ...overrides,
  } as PortfolioTrade
}

const buys: PortfolioTrade[] = [
  trade({ date: '2024-01-02', direction: 'BUY', size: 600 }),
  trade({ date: '2024-02-01', direction: 'BUY', size: 400 }),
]

const sells: PortfolioTrade[] = [
  trade({ date: '2024-03-04', direction: 'SELL', size: 600, pnl: -1500, cost_basis: 6000, reason: '卖出条件' }),
  trade({ date: '2024-03-05', direction: 'SELL', size: 400, pnl: 800, cost_basis: 4000, reason: '止盈' }),
]

function resultWith(trades: PortfolioTrade[]): PortfolioBacktestResult {
  return {
    universe: 'all', total_candidates: 1, processed: 1, skipped: 0, errors: 0, bars: 3,
    start_date: '2024-01-02', end_date: '2024-03-05', max_positions: 1,
    ranking_value: 'indicator.varo7', rank_order: 'desc',
    performance: {},
    equity_curve: [
      { date: '2024-01-02', cash: 0, position_value: 10000, total: 10000, positions_count: 1, drawdown: 0, drawdown_pct: 0 },
      { date: '2024-03-05', cash: 8000, position_value: 2000, total: 10000, positions_count: 1, drawdown: 0, drawdown_pct: 0 },
    ],
    trades,
    states: [{
      date: '2024-03-05', cash: 8000, position_value: 2000, total: 10000, positions_count: 1,
      holdings: [{ market: 'SZ', code: '000001', name: '平安银行', size: 200, entry_price: 10, close: 7.5, unrealized_pnl: -500 }],
    }],
    ranking_events: [],
    failure_reasons: {},
    diagnostic: null,
    risk_controls: { take_profit_pct: 0.2, take_profit_mode: 'pct', stop_loss_pct: null, trailing_stop_pct: null },
    data_fingerprint: 'fingerprint',
    benchmark: null,
  }
}

describe('pairRoundTrips', () => {
  it('matches sells to the oldest lots first and keeps the real holding days', () => {
    const trips = pairRoundTrips([...sells, ...buys])
    expect(trips).toHaveLength(2)
    // 卖出顺序按成交日排，配对时先消耗 1 月那批。
    expect(trips[0].entryDate).toBe('2024-01-02')
    expect(trips[0].holdingDays).toBe(62)
    expect(trips[1].entryDate).toBe('2024-02-01')
    expect(trips[1].holdingDays).toBe(33)
  })

  it('records an unmatched sell without inventing an entry date', () => {
    const trips = pairRoundTrips([trade({ date: '2024-03-05', direction: 'SELL', size: 300, pnl: -300, cost_basis: 3000 })])
    expect(trips).toHaveLength(1)
    expect(trips[0].entryDate).toBe('')
    expect(trips[0].holdingDays).toBe(0)
  })
})

describe('analyzePortfolioPnl', () => {
  it('splits the P&L into winners, losers and their cost', () => {
    const analysis = analyzePortfolioPnl(resultWith([...buys, ...sells]))
    expect(analysis.overview.trips).toBe(2)
    expect(analysis.overview.winners).toBe(1)
    expect(analysis.overview.losers).toBe(1)
    expect(analysis.overview.grossWin).toBe(800)
    expect(analysis.overview.grossLoss).toBe(1500)
    expect(analysis.overview.net).toBe(-700)
    expect(analysis.overview.profitFactor).toBeCloseTo(0.5333, 4)
    expect(analysis.overview.worstPnl).toBe(-1500)
  })

  it('puts the loss in the holding-period and return buckets it belongs to', () => {
    const analysis = analyzePortfolioPnl(resultWith([...buys, ...sells]))
    const longHold = analysis.byHolding.find((row) => row.label === '61–120 日')
    const shortHold = analysis.byHolding.find((row) => row.label === '31–60 日')
    expect(longHold?.count).toBe(1)
    expect(longHold?.loss).toBe(1500)
    expect(shortHold?.loss).toBe(0)

    const deep = analysis.byReturn.find((row) => row.label === '-30% ~ -20%')
    expect(deep?.count).toBe(1)
    // 收益率 -1500 / 6000 = -25%，落在 -30% ~ -20% 这一档。
    expect(analysis.worstTrips[0].returnPct).toBeCloseTo(-0.25, 6)
  })

  it('keeps unrealized loss separate from realized loss', () => {
    const analysis = analyzePortfolioPnl(resultWith([...buys, ...sells]))
    expect(analysis.overview.grossLoss).toBe(1500)
    expect(analysis.unrealized.total).toBe(-500)
    expect(analysis.unrealized.rows[0].pct).toBeCloseTo(-0.25, 6)
  })

  it('flags a run without any stop-loss', () => {
    const analysis = analyzePortfolioPnl(resultWith([...buys, ...sells]))
    expect(analysis.riskControls.map((item) => item.setting)).toEqual([
      '20.0%（固定比例）',
      '未启用',
      '未启用',
    ])
    expect(analysis.riskControls.map((item) => item.label)).toEqual([
      '止盈', '止损', '移动止盈',
    ])
    expect(analysis.noStopLoss).toBe(true)
  })

  it('returns an empty structure instead of throwing on an empty result', () => {
    const analysis = analyzePortfolioPnl(null)
    expect(analysis.overview.trips).toBe(0)
    expect(analysis.overview.profitFactor).toBeNull()
    expect(analysis.unrealized.rows).toEqual([])
    expect(analysis.drawdowns).toEqual([])
  })
})
