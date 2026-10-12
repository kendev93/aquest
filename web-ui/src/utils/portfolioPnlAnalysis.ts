/**
 * 组合回测的盈亏结构分析（重点是亏损）。
 *
 * 口径与 `scripts/analyze_portfolio_losses.py` 保持一致，两边改任何一边都要看另一边：
 *   * 以 **FIFO 平仓配对** 为单位，而不是成交行——一次建仓可能分几次卖出，
 *     只有拆到配对上，持有天数才是真的。
 *   * 收益率 = 盈亏 / 成本基数（含买入成本、卖出费税），可以直接对上"跌了百分之几"。
 *   * 期末仍持有的仓位没有平仓，单独作为未实现浮亏，不混进已实现。
 *   * 名称只是标签，不参与任何统计，也不做 ST/退市之类的归因。
 */

import type {
  PortfolioBacktestResult,
  PortfolioEquityPoint,
  PortfolioHolding,
  PortfolioTrade,
} from '../types'


export interface PnlRoundTrip {
  market: string
  code: string
  name: string
  /** 空字符串表示配对时找不到买入记录（结果被截断或数据缺行）。 */
  entryDate: string
  exitDate: string
  holdingDays: number
  size: number
  pnl: number
  costBasis: number
  reason: string
  returnPct: number
  label: string
}


export interface PnlBucketRow {
  label: string
  count: number
  lossCount: number
  net: number
  /** 亏损额，正数（0 表示这一段没有亏损）。 */
  loss: number
}


export interface PnlOverview {
  trips: number
  winners: number
  losers: number
  net: number
  grossWin: number
  /** 亏损总额，正数。 */
  grossLoss: number
  profitFactor: number | null
  avgWin: number | null
  avgLoss: number | null
  worstPnl: number
  /** 佣金 + 印花税 + 滑点。 */
  costs: number
}


export interface PnlStreak {
  /** 最长连亏段数。 */
  longest: number
  /** 连亏期间最大的累计亏损，正数。 */
  worstAmount: number
  /** 结尾还在连亏的段数（0 表示最后一段是盈利）。 */
  trailing: number
}


export interface PnlDrawdownEpisode {
  start: string
  end: string
  bars: number
  /** 回撤深度，负数。 */
  depth: number
  /** 同期基准涨跌，没有基准时为 null。 */
  benchmarkChange: number | null
}


export interface PnlRiskControl {
  label: string
  setting: string
  enabled: boolean
}


export interface PnlUnrealizedRow {
  label: string
  entry: number
  close: number
  pct: number | null
  pnl: number
}


export interface PnlAnalysis {
  overview: PnlOverview
  byYear: PnlBucketRow[]
  byHolding: PnlBucketRow[]
  byReturn: PnlBucketRow[]
  byReason: PnlBucketRow[]
  worstTrips: PnlRoundTrip[]
  streak: PnlStreak
  drawdowns: PnlDrawdownEpisode[]
  unrealized: { rows: PnlUnrealizedRow[]; total: number }
  riskControls: PnlRiskControl[]
  /** 既没有固定止损也没有移动止盈：亏损深度只能由卖出条件决定。 */
  noStopLoss: boolean
}


export const HOLDING_BUCKETS: { low: number; high: number; label: string }[] = [
  { low: 0, high: 11, label: '≤10 日' },
  { low: 11, high: 31, label: '11–30 日' },
  { low: 31, high: 61, label: '31–60 日' },
  { low: 61, high: 121, label: '61–120 日' },
  { low: 121, high: Number.POSITIVE_INFINITY, label: '>120 日' },
]

export const RETURN_BUCKETS: { low: number; high: number; label: string }[] = [
  { low: -Number.POSITIVE_INFINITY, high: -0.3, label: '≤ -30%' },
  { low: -0.3, high: -0.2, label: '-30% ~ -20%' },
  { low: -0.2, high: -0.1, label: '-20% ~ -10%' },
  { low: -0.1, high: 0, label: '-10% ~ 0%' },
  { low: 0, high: 0.1, label: '0% ~ 10%' },
  { low: 0.1, high: 0.2, label: '10% ~ 20%' },
  { low: 0.2, high: 0.5, label: '20% ~ 50%' },
  { low: 0.5, high: Number.POSITIVE_INFINITY, label: '> 50%' },
]


function dateKey(value: string): number {
  return Number(String(value).replace(/-/g, ''))
}


function daysBetween(start: string, end: string): number {
  if (!start || !end) return 0
  const from = Date.parse(`${start}T00:00:00Z`)
  const to = Date.parse(`${end}T00:00:00Z`)
  if (!Number.isFinite(from) || !Number.isFinite(to)) return 0
  return Math.max(0, Math.round((to - from) / 86_400_000))
}


function toTrip(
  trade: PortfolioTrade,
  entryDate: string,
  size: number,
  pnl: number,
  basis: number,
): PnlRoundTrip {
  return {
    market: trade.market,
    code: trade.code,
    name: trade.name ?? '',
    entryDate,
    exitDate: trade.date,
    holdingDays: daysBetween(entryDate, trade.date),
    size,
    pnl,
    costBasis: basis,
    reason: trade.reason || '未标注',
    returnPct: basis > 0 ? pnl / basis : 0,
    label: `${trade.market}${trade.code} ${trade.name ?? ''}`.trim(),
  }
}


/**
 * 按标的把买入和卖出配成完整的持仓段。
 *
 * 卖出量超过已记录买入量时（回测被截断、或数据缺行）按整笔记一次，
 * 持有天数记 0 而不是编一个数。
 */
export function pairRoundTrips(trades: PortfolioTrade[]): PnlRoundTrip[] {
  const ordered = [...trades].sort((left, right) => dateKey(left.date) - dateKey(right.date))
  const lots = new Map<string, { date: string; size: number }[]>()
  const trips: PnlRoundTrip[] = []
  for (const trade of ordered) {
    const key = `${trade.market}.${trade.code}`
    let queue = lots.get(key)
    if (!queue) {
      queue = []
      lots.set(key, queue)
    }
    if (trade.direction === 'BUY') {
      if (trade.size > 0) queue.push({ date: trade.date, size: trade.size })
      continue
    }
    if (trade.direction !== 'SELL' || trade.size <= 0) continue
    let remaining = trade.size
    while (remaining > 1e-9 && queue.length) {
      const lot = queue[0]
      const used = Math.min(lot.size, remaining)
      const share = used / trade.size
      trips.push(toTrip(trade, lot.date, used, trade.pnl * share, trade.cost_basis * share))
      remaining -= used
      lot.size -= used
      if (lot.size <= 1e-9) queue.shift()
    }
    if (remaining > 1e-9) {
      const share = remaining / trade.size
      trips.push(toTrip(trade, '', remaining, trade.pnl * share, trade.cost_basis * share))
    }
  }
  return trips.sort((left, right) => dateKey(left.exitDate) - dateKey(right.exitDate))
}


function bucketize(
  trips: PnlRoundTrip[],
  buckets: { low: number; high: number; label: string }[],
  valueOf: (trip: PnlRoundTrip) => number,
): PnlBucketRow[] {
  return buckets.map((bucket) => {
    const picked = trips.filter((trip) => {
      const value = valueOf(trip)
      return value >= bucket.low && value < bucket.high
    })
    const losses = picked.filter((trip) => trip.pnl < 0)
    return {
      label: bucket.label,
      count: picked.length,
      lossCount: losses.length,
      net: picked.reduce((sum, trip) => sum + trip.pnl, 0),
      // 用「0 - 亏损」而不是「-(亏损之和)」：空桶会得到 -0，界面上就是个负号。
      loss: losses.reduce((sum, trip) => sum - trip.pnl, 0),
    }
  })
}


function summarizeReasons(trips: PnlRoundTrip[]): PnlBucketRow[] {
  const groups = new Map<string, PnlRoundTrip[]>()
  for (const trip of trips) {
    const key = trip.reason || '未标注'
    const bucket = groups.get(key)
    if (bucket) bucket.push(trip)
    else groups.set(key, [trip])
  }
  return [...groups.entries()]
    .map(([label, picked]) => ({
      label,
      count: picked.length,
      lossCount: picked.filter((trip) => trip.pnl < 0).length,
      net: picked.reduce((sum, trip) => sum + trip.pnl, 0),
      loss: picked.filter((trip) => trip.pnl < 0).reduce((sum, trip) => sum - trip.pnl, 0),
    }))
    .sort((left, right) => left.net - right.net)
}


function summarizeStreak(trips: PnlRoundTrip[]): PnlStreak {
  let current = 0
  let currentLoss = 0
  let longest = 0
  let worstAmount = 0
  for (const trip of trips) {
    if (trip.pnl < 0) {
      current += 1
      currentLoss += trip.pnl
      longest = Math.max(longest, current)
      worstAmount = Math.max(worstAmount, -currentLoss)
      continue
    }
    current = 0
    currentLoss = 0
  }
  let trailing = 0
  for (let index = trips.length - 1; index >= 0; index -= 1) {
    if (trips[index].pnl < 0) trailing += 1
    else break
  }
  return { longest, worstAmount, trailing }
}


function drawdownEpisodes(curve: PortfolioEquityPoint[]): { start: string; end: string; bars: number; depth: number }[] {
  const episodes: { start: string; end: string; bars: number; depth: number }[] = []
  let peakDate = ''
  let peakValue = 0
  let troughDate = ''
  let troughValue = 0
  let bars = 0
  for (const point of curve) {
    if (point.total >= peakValue) {
      if (troughValue > 0 && troughValue < peakValue) {
        episodes.push({ start: peakDate, end: troughDate, bars, depth: troughValue / peakValue - 1 })
      }
      peakValue = point.total
      peakDate = point.date
      troughValue = 0
      troughDate = ''
      bars = 0
      continue
    }
    bars += 1
    if (!troughValue || point.total < troughValue) {
      troughValue = point.total
      troughDate = point.date
    }
  }
  if (troughValue > 0 && troughValue < peakValue) {
    episodes.push({ start: peakDate, end: troughDate, bars, depth: troughValue / peakValue - 1 })
  }
  return episodes.sort((left, right) => left.depth - right.depth).slice(0, 3)
}


function benchmarkChange(
  result: PortfolioBacktestResult,
  start: string,
  end: string,
): number | null {
  const benchmark = result.benchmark
  if (!benchmark?.dates.length || benchmark.totals.length !== benchmark.dates.length) return null
  const startKey = dateKey(start)
  const endKey = dateKey(end)
  let begin: number | null = null
  let finish: number | null = null
  benchmark.dates.forEach((date, index) => {
    if (begin === null && date >= startKey) begin = benchmark.totals[index]
    if (date <= endKey) finish = benchmark.totals[index]
  })
  if (begin === null || finish === null || begin <= 0) return null
  return finish / begin - 1
}


function describeRiskControl(risk: Record<string, number | string | null> | undefined, prefix: string): PnlRiskControl {
  const labels: Record<string, string> = {
    take_profit: '止盈',
    stop_loss: '止损',
    trailing_stop: '移动止盈',
  }
  const pct = Number(risk?.[`${prefix}_pct`] ?? 0)
  const atr = Number(risk?.[`${prefix}_atr_multiplier`] ?? 0)
  if (pct > 0) return { label: labels[prefix] ?? prefix, setting: `${(pct * 100).toFixed(1)}%（固定比例）`, enabled: true }
  if (atr > 0) return { label: labels[prefix] ?? prefix, setting: `${atr} × ATR`, enabled: true }
  return { label: labels[prefix] ?? prefix, setting: '未启用', enabled: false }
}


function unrealizedRows(holdings: PortfolioHolding[]): { rows: PnlUnrealizedRow[]; total: number } {
  const rows = [...holdings]
    .sort((left, right) => left.unrealized_pnl - right.unrealized_pnl)
    .map((holding) => ({
      label: `${holding.market}${holding.code} ${holding.name ?? ''}`.trim(),
      entry: holding.entry_price,
      close: holding.close,
      pct: holding.entry_price > 0 ? holding.close / holding.entry_price - 1 : null,
      pnl: holding.unrealized_pnl,
    }))
  return { rows, total: rows.reduce((sum, row) => sum + row.pnl, 0) }
}


/** 把一次回测结果拆成盈亏结构的各个切面。结果为空时返回全零结构，调用方不必判空。 */
export function analyzePortfolioPnl(result: PortfolioBacktestResult | null): PnlAnalysis {
  const trades = result?.trades ?? []
  const trips = pairRoundTrips(trades)
  const winners = trips.filter((trip) => trip.pnl > 0)
  const losers = trips.filter((trip) => trip.pnl < 0)
  const grossWin = winners.reduce((sum, trip) => sum + trip.pnl, 0)
  const grossLoss = losers.reduce((sum, trip) => sum - trip.pnl, 0)
  const costs = trades.reduce(
    (sum, trade) => sum + trade.commission + trade.stamp_tax + trade.slippage,
    0,
  )

  const years = new Map<string, PnlRoundTrip[]>()
  for (const trip of trips) {
    const key = trip.exitDate.slice(0, 4) || '—'
    const bucket = years.get(key)
    if (bucket) bucket.push(trip)
    else years.set(key, [trip])
  }
  const byYear = [...years.entries()]
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([label, picked]) => ({
      label,
      count: picked.length,
      lossCount: picked.filter((trip) => trip.pnl < 0).length,
      net: picked.reduce((sum, trip) => sum + trip.pnl, 0),
      loss: picked.filter((trip) => trip.pnl < 0).reduce((sum, trip) => sum - trip.pnl, 0),
    }))

  const episodes = result ? drawdownEpisodes(result.equity_curve ?? []) : []
  const risk = result?.risk_controls
  const riskControls = ['take_profit', 'stop_loss', 'trailing_stop'].map((prefix) =>
    describeRiskControl(risk, prefix),
  )
  const lastHoldings = result?.states?.at(-1)?.holdings ?? []

  return {
    overview: {
      trips: trips.length,
      winners: winners.length,
      losers: losers.length,
      net: trips.reduce((sum, trip) => sum + trip.pnl, 0),
      grossWin,
      grossLoss,
      profitFactor: grossLoss > 0 ? grossWin / grossLoss : null,
      avgWin: winners.length ? grossWin / winners.length : null,
      avgLoss: losers.length ? grossLoss / losers.length : null,
      worstPnl: trips.length ? Math.min(...trips.map((trip) => trip.pnl)) : 0,
      costs,
    },
    byYear,
    byHolding: bucketize(trips, HOLDING_BUCKETS, (trip) => trip.holdingDays),
    byReturn: bucketize(trips, RETURN_BUCKETS, (trip) => trip.returnPct),
    byReason: summarizeReasons(trips),
    worstTrips: [...trips].sort((left, right) => left.pnl - right.pnl).slice(0, 10),
    streak: summarizeStreak(trips),
    drawdowns: episodes.map((episode) => ({
      ...episode,
      benchmarkChange: result ? benchmarkChange(result, episode.start, episode.end) : null,
    })),
    unrealized: unrealizedRows(lastHoldings),
    riskControls,
    noStopLoss:
      !riskControls.find((item) => item.label === '止损')?.enabled &&
      !riskControls.find((item) => item.label === '移动止盈')?.enabled,
  }
}
