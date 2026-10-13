/**
 * 数值展示的统一出口。
 *
 * 以前 16 个文件各自定义 formatNumber / formatPercent / formatMoney / formatDuration：
 * 逻辑相同但细节互相漂移——有的查 isFinite（NaN 会渲染成 "NaN"），有的只查 null；
 * 「百分比」这个名字下混着三种语义（乘 100、带 + 号、不乘 100）；时长格式化抄了 4 份。
 * 这里收敛成一个出口：所有函数对「不是有限数字」的输入统一返回 '—'。
 *
 * 语义备忘：
 * - formatPercent：输入是小数比例（0.1234 → "12.34%"）；
 * - formatSignedPercent：同上，但正数带 +（涨幅、差异类场景）；
 * - formatMoney：人民币两位小数带千分位；
 * - formatDuration：秒 → 「12.3 秒 / 5 分 3 秒 / 2 小时 5 分钟」。
 *
 * 参数用 unknown 是刻意的：这组函数的本职就是防御性渲染——后端 summary 里的
 * 字段经常以 unknown 形态出现（StrategyPoolPage 就是这么用的），把「是不是数」
 * 的判断收进来，调用方就不用先做一遍类型窄化。
 * 例外：SwingResearchPage 有个同名 formatPercent 输入已经是百分数
 * （12.3 → "12.3%"），语义不同，保留在页面本地并加注释。
 */

const PLACEHOLDER = '—'

function asFiniteNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

export function formatNumber(value: unknown, digits = 2): string {
  const finite = asFiniteNumber(value)
  return finite === null ? PLACEHOLDER : finite.toFixed(digits)
}

export function formatPercent(value: unknown, digits = 2): string {
  const finite = asFiniteNumber(value)
  return finite === null ? PLACEHOLDER : `${(finite * 100).toFixed(digits)}%`
}

export function formatSignedPercent(value: unknown, digits = 2): string {
  const finite = asFiniteNumber(value)
  if (finite === null) return PLACEHOLDER
  return `${finite > 0 ? '+' : ''}${(finite * 100).toFixed(digits)}%`
}

export function formatCount(value: unknown): string {
  const finite = asFiniteNumber(value)
  return finite === null ? PLACEHOLDER : String(Math.round(finite))
}

export function formatMoney(value: unknown): string {
  const finite = asFiniteNumber(value)
  return finite === null
    ? PLACEHOLDER
    : `¥${finite.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds < 0) {
    return PLACEHOLDER
  }
  if (seconds < 60) return `${seconds.toFixed(1)} 秒`
  const totalSeconds = Math.round(seconds)
  const minutes = Math.floor(totalSeconds / 60)
  const remainingSeconds = totalSeconds % 60
  if (minutes < 60) {
    return remainingSeconds ? `${minutes} 分 ${remainingSeconds} 秒` : `${minutes} 分钟`
  }
  const hours = Math.floor(minutes / 60)
  const remainingMinutes = minutes % 60
  return remainingMinutes ? `${hours} 小时 ${remainingMinutes} 分钟` : `${hours} 小时`
}
