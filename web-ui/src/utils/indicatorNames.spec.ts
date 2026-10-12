import { describe, expect, it } from 'vitest'

import type { FormulaScreenMetadata } from '../types'
import { buildIndicatorNameMap, indicatorDisplayName } from './indicatorNames'

const metadata: FormulaScreenMetadata = {
  indicators: [
    {
      id: 'technical',
      display_name: '技术指标',
      minimum_bars: 1,
      recommended_bars: 1,
      signals: [],
      values: [
        { id: 'technical.macd', display_name: 'MACD', description: 'MACD 指标' },
        { id: 'technical.macd_signal', display_name: 'MACD信号线', description: 'MACD 信号线' },
      ],
    },
    {
      id: 'indicator_two',
      display_name: '庄家顶底',
      minimum_bars: 1,
      recommended_bars: 1,
      signals: [{ id: 'indicator_two.end', display_name: '终', description: '短期线大于 90' }],
      values: [],
    },
  ],
  combine_modes: [],
  supported_markets: [],
  supported_universe: [],
  periods: [],
  data_directory_help: '',
}

describe('indicatorNames', () => {
  it('translates values and makes generic signal names meaningful', () => {
    const names = buildIndicatorNameMap(metadata)

    expect(indicatorDisplayName(names, 'technical.macd')).toBe('MACD')
    expect(indicatorDisplayName(names, 'indicator_two.end')).toBe('庄家顶底 · 终（短期线大于 90）')
    expect(indicatorDisplayName(names, 'custom.unknown')).toBe('custom.unknown')
  })
})
