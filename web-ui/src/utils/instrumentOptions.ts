/**
 * 证券类型与 A 股板块的选项常量。
 *
 * 以前在 6 个页面各定义一份（FormulaScreen / IndividualResearch / MarketData /
 * PortfolioBacktest / StrategyDiscovery / StrategyFitness），内容逐字相同。
 * StrategyPoolPage 的同名常量是另一种类型（StrategyPoolBoard 而非 InstrumentBoard），
 * 语义不同，不在此列。
 */

import type { InstrumentBoard, InstrumentType } from '../types'

export const INSTRUMENT_TYPE_OPTIONS: { key: InstrumentType; label: string }[] = [
  { key: 'stock', label: '股票' },
]

export const BOARD_OPTIONS: { key: InstrumentBoard; label: string }[] = [
  { key: 'main', label: '主板' },
  { key: 'star', label: '科创板' },
  { key: 'chinext', label: '创业板' },
]
