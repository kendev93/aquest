import { describe, expect, it } from 'vitest'

import { equityChartIndices, equityChartPoints, equityChartRange } from './portfolioChart'

describe('equityChartIndices', () => {
  it('returns nothing for curves too short to draw', () => {
    expect(equityChartIndices(0)).toEqual([])
    expect(equityChartIndices(1)).toEqual([])
  })

  it('keeps every point for short curves', () => {
    expect(equityChartIndices(4)).toEqual([0, 1, 2, 3])
  })

  it('thins long curves but always keeps the final point', () => {
    // 抽稀阈值是 120 点，短曲线逐点保留。
    expect(equityChartIndices(10)).toEqual([0, 1, 2, 3, 4, 5, 6, 7, 8, 9])
    const sparse = equityChartIndices(601)
    expect(sparse[0]).toBe(0)
    expect(sparse[sparse.length - 1]).toBe(600)
    expect(sparse.length).toBeLessThanOrEqual(121)
  })
})

describe('equityChartRange', () => {
  it('spans both the strategy and the benchmark curves', () => {
    const range = equityChartRange([1, 2, 3], [0.5, 2.5, 4])
    expect(range.minimum).toBe(0.5)
    expect(range.span).toBeCloseTo(3.5)
  })

  it('skips missing benchmark values and falls back to a unit span when flat', () => {
    expect(equityChartRange([2, 2], [undefined, undefined])).toEqual({ minimum: 2, span: 1 })
    expect(equityChartRange([], [])).toEqual({ minimum: 0, span: 1 })
  })
})

describe('equityChartPoints', () => {
  it('maps values onto the 800x220 canvas with margins', () => {
    const points = equityChartPoints([0, 1], 2, { minimum: 0, span: 1 })
    expect(points).toBe('0.0,220.0 800.0,30.0')
  })

  it('skips undefined positions so a shorter benchmark leaves no tail points', () => {
    const points = equityChartPoints([1, undefined, 0], 3, { minimum: 0, span: 1 })
    expect(points).toBe('0.0,30.0 800.0,220.0')
  })
})
