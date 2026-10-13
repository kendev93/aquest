/**
 * 组合回测页净值曲线的几何计算：抽稀、纵轴范围、SVG 点串。
 *
 * 从 PortfolioBacktestPage 拆出来的纯函数。抽稀上限 120 点、画布 800×220
 * （上下各留 15px 边距）这三件事决定了输出的形状，改动前先确认图表没有
 * 变形。策略线和基准线共用同一批下标，两条线才对得上同一天。
 */

export interface EquityChartRange {
  minimum: number
  span: number
}

/** 抽稀后的曲线下标：点太多时均匀取样，但最后一个点永远保留。 */
export function equityChartIndices(curveLength: number): number[] {
  if (curveLength < 2) return []
  const step = curveLength > 120 ? Math.ceil(curveLength / 120) : 1
  const indices: number[] = []
  for (let index = 0; index < curveLength; index += step) indices.push(index)
  if (indices[indices.length - 1] !== curveLength - 1) indices.push(curveLength - 1)
  return indices
}

/**
 * 纵轴范围要同时装下策略与基准，否则基准跑出画框，看着像"策略完胜"。
 * 传入的两组值按下标一一对应；基准里超出策略长度的部分以 undefined 表示并跳过。
 */
export function equityChartRange(
  strategyTotals: number[],
  benchmarkTotals: (number | undefined)[],
): EquityChartRange {
  const values: number[] = []
  for (const value of strategyTotals) values.push(value)
  for (const value of benchmarkTotals) {
    if (value !== undefined) values.push(value)
  }
  if (!values.length) return { minimum: 0, span: 1 }
  const minimum = Math.min(...values)
  const maximum = Math.max(...values)
  return { minimum, span: maximum - minimum || 1 }
}

/** 数值序列 → polyline 点串；undefined 的位置跳过（基准比策略短时尾部缺失）。 */
export function equityChartPoints(
  values: (number | undefined)[],
  pointCount: number,
  range: EquityChartRange,
): string {
  return values
    .map((value, position) => {
      if (value === undefined) return null
      const x = (position / Math.max(pointCount - 1, 1)) * 800
      const y = 220 - ((value - range.minimum) / range.span) * 190
      return `${x.toFixed(1)},${y.toFixed(1)}`
    })
    .filter((point): point is string => point !== null)
    .join(' ')
}
