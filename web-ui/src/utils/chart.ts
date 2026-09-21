/** 手写 SVG 折线图共用的一套坐标换算。
 *
 * 项目里没有图表库，曲线都是手写 polyline。两处各写一份换算的结果是：同样的
 * 一组数在两页上画出来不一样高。这里把「多序列共用同一纵轴」这件事收在一处，
 * 顺便保证零线在范围内——少了零线，一条全程为正的曲线看起来像从地板上起飞。
 */

export interface ChartPoint {
  x: number
  y: number
}

export interface ChartTick {
  value: number
  y: number
}

export interface ChartGeometry {
  /** 每条序列一个 polyline 的 points 字符串。 */
  lines: string[]
  min: number
  max: number
  /** 零值所在的 y；不在范围内时为 null。 */
  zeroY: number | null
  ticks: ChartTick[]
  width: number
  height: number
}

export interface ChartGeometryOptions {
  width?: number
  height?: number
  paddingLeft?: number
  paddingRight?: number
  paddingTop?: number
  paddingBottom?: number
  /** 把 0 拉进纵轴范围（收益率曲线必须有零线，否则读不出盈亏方向）。 */
  includeZero?: boolean
  tickCount?: number
}

export function buildChartGeometry(
  series: readonly (readonly ChartPoint[])[],
  options: ChartGeometryOptions = {},
): ChartGeometry | null {
  const width = options.width ?? 820
  const height = options.height ?? 240
  const paddingLeft = options.paddingLeft ?? 46
  const paddingRight = options.paddingRight ?? 12
  const paddingTop = options.paddingTop ?? 12
  const paddingBottom = options.paddingBottom ?? 24
  const tickCount = options.tickCount ?? 4

  let xMin = Number.POSITIVE_INFINITY
  let xMax = Number.NEGATIVE_INFINITY
  let yMin = Number.POSITIVE_INFINITY
  let yMax = Number.NEGATIVE_INFINITY
  let count = 0
  for (const points of series) {
    for (const point of points) {
      if (!Number.isFinite(point.x) || !Number.isFinite(point.y)) continue
      count += 1
      xMin = Math.min(xMin, point.x)
      xMax = Math.max(xMax, point.x)
      yMin = Math.min(yMin, point.y)
      yMax = Math.max(yMax, point.y)
    }
  }
  if (count < 2) return null

  if (options.includeZero) {
    yMin = Math.min(yMin, 0)
    yMax = Math.max(yMax, 0)
  }
  const xSpan = xMax - xMin || 1
  const ySpan = yMax - yMin || 1
  const innerWidth = Math.max(1, width - paddingLeft - paddingRight)
  const innerHeight = Math.max(1, height - paddingTop - paddingBottom)

  const toX = (value: number) => paddingLeft + ((value - xMin) / xSpan) * innerWidth
  const toY = (value: number) => paddingTop + innerHeight - ((value - yMin) / ySpan) * innerHeight

  const lines = series.map((points) =>
    points
      .filter((point) => Number.isFinite(point.x) && Number.isFinite(point.y))
      .map((point) => `${toX(point.x).toFixed(1)},${toY(point.y).toFixed(1)}`)
      .join(' '),
  )
  const ticks: ChartTick[] = []
  for (let index = 0; index <= tickCount; index += 1) {
    const value = yMin + (ySpan * index) / tickCount
    ticks.push({ value, y: toY(value) })
  }
  return {
    lines,
    min: yMin,
    max: yMax,
    zeroY: yMin <= 0 && yMax >= 0 ? toY(0) : null,
    ticks,
    width,
    height,
  }
}

export function formatChartValue(value: number, digits = 2): string {
  if (!Number.isFinite(value)) return '—'
  const magnitude = Math.abs(value)
  if (magnitude >= 1000) return value.toFixed(0)
  if (magnitude >= 10) return value.toFixed(1)
  return value.toFixed(digits)
}
