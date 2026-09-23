/**
 * 买卖条件研究页的展示口径。
 *
 * 这里全是纯函数：不碰响应式状态，需要的上下文（可选输出列表、报告、检验次数）
 * 一律显式传参。页面和结果区的子组件共用同一份，避免「表格里这么说、卡片里
 * 那么说」。
 */

import { valueOptionLabel } from './formulaScreen'
import { formatChinaTimestamp } from './time'
import type {
  ConditionResearchCandidate,
  ConditionResearchDuplicate,
  ConditionResearchPayload,
  ConditionResearchReport,
  ConditionResearchSensitivityPoint,
  ConditionResearchTrials,
  InstrumentBoard,
  ScreenCondition,
  ValueDefinition,
} from '../types'

/** 历史列表里的一条记录：后端只给摘要，完整输入要单独再取一次。 */
export interface ResearchHistorySummary {
  history_id: string
  created_at: string
  finished_at?: string
  duration_seconds?: number
  summary?: Record<string, unknown>
}

export const OPERATOR_LABELS: Record<string, string> = {
  gt: '>',
  gte: '≥',
  lt: '<',
  lte: '≤',
  eq: '=',
  neq: '≠',
  cross_up: '上穿',
  cross_down: '下穿',
}

export const RANKING_CONFIDENCE_LABELS: Record<string, string> = {
  high: '证据充分',
  medium: '可用，建议先复验',
  low: '证据不足',
  none: '暂无法判断',
}

export const STOCK_BOARD_OPTIONS: { key: InstrumentBoard; label: string }[] = [
  { key: 'main', label: '主板' },
  { key: 'star', label: '科创板' },
  { key: 'chinext', label: '创业板' },
]

export function formatPercent(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : (value * 100).toFixed(2) + '%'
}

export function formatNumber(value: number | null | undefined, digits = 2): string {
  return value === null || value === undefined ? '—' : value.toFixed(digits)
}

export function winRateDelta(stock: {
  win_rate: number | null
  baseline_win_rate?: number | null
}): string {
  if (stock.win_rate === null || stock.baseline_win_rate === undefined || stock.baseline_win_rate === null) {
    return '—'
  }
  const delta = (stock.win_rate - stock.baseline_win_rate) * 100
  return (delta > 0 ? '+' : '') + delta.toFixed(2) + ' pp'
}

export function formatScreenConditionText(
  values: ValueDefinition[],
  condition: ScreenCondition,
): string {
  const operator = OPERATOR_LABELS[condition.operator] ?? condition.operator
  const leftValue = values.find((value) => value.id === condition.left_value)
  const rightValue = condition.right_value
    ? values.find((value) => value.id === condition.right_value)
    : null
  const leftLabel = leftValue ? valueOptionLabel(leftValue) : condition.left_value
  const rightLabel = rightValue
    ? valueOptionLabel(rightValue)
    : formatNumber(condition.right_constant, 3)
  return leftLabel + ' ' + operator + ' ' + rightLabel
}

export function formatConditionText(
  values: ValueDefinition[],
  candidate: ConditionResearchCandidate,
): string {
  return formatScreenConditionText(values, candidate.condition)
}

/** 被这条候选合并掉的同义条件：同一个发现的不同写法，多数只是阈值差一点。 */
export function mergedDuplicates(
  candidate: ConditionResearchCandidate,
): ConditionResearchDuplicate[] {
  return candidate.mining.duplicates ?? []
}

export function duplicateTitle(
  values: ValueDefinition[],
  candidate: ConditionResearchCandidate,
): string {
  const items = mergedDuplicates(candidate)
  if (!items.length) return ''
  return (
    '判定为同一个发现而合并掉的条件（相关性 / 训练 lift / 样本数）：\n'
    + items
      .map(
        (item) =>
          `· ${formatScreenConditionText(values, item.condition)} — ρ=${item.correlation.toFixed(3)}`
          + `，lift=${item.lift.toFixed(3)}，样本 ${item.support_count}`,
      )
      .join('\n')
  )
}

// 买卖两侧的样本口径不同：买样本 = 每次入场 1 条，卖样本 = 持仓期内每个
// 提前离场决策点 1 条（同一笔交易可贡献多条）。因此卖出候选不能拿成交笔数
// 当分母，否则命中率会被高估甚至超过 100%。
export function hitRateDenominator(
  report: ConditionResearchReport | null,
  candidate: ConditionResearchCandidate,
): number {
  const side = candidate.side === 'sell' ? 'sell' : 'buy'
  const sampleCount = report?.sample_totals?.[side]?.count
  if (typeof sampleCount === 'number' && sampleCount > 0) return sampleCount
  // 兼容没有 sample_totals 的历史结果。
  return report?.baseline.train.pooled_trades.closed_trades ?? 0
}

export function conditionHitRate(
  report: ConditionResearchReport | null,
  candidate: ConditionResearchCandidate,
): string {
  const denominator = hitRateDenominator(report, candidate)
  if (!denominator) return '—'
  return ((candidate.mining.support_count / denominator) * 100).toFixed(3) + '%'
}

export function hitRateTitle(candidate: ConditionResearchCandidate): string {
  return candidate.side === 'sell'
    ? '该条件在持仓期提前离场决策点上的命中比例（全市场合并）'
    : '该条件在基础规则入场日上的命中比例（全市场合并）'
}

/** 前 N 名口径胜率：只统计每个信号日排序后真会成交的那批样本。 */
export function topNWinRate(candidate: ConditionResearchCandidate): string {
  const rate = candidate.mining.top_n_outcome_rate
  return rate === null || rate === undefined ? '—' : formatPercent(rate)
}

export function topNTitle(
  report: ConditionResearchReport | null,
  candidate: ConditionResearchCandidate,
): string {
  const slots = report?.ranking?.top_n
  const days = candidate.mining.top_n_filtered_days ?? 0
  if (candidate.mining.top_n_outcome_rate === null || candidate.mining.top_n_outcome_rate === undefined) {
    // 卖出候选本就不参与该诊断（槽位竞争只发生在入场，卖出是触发式的），
    // 不能统一说成「没解析出排序指标」—— 上方卡片明明给了推荐。
    if (candidate.side === 'sell') {
      return '槽位竞争只发生在入场，卖出是触发式的，没有「先卖谁」这回事，因此卖出候选不做前 N 名诊断。'
    }
    return '这次研究没有解析出排序指标，无法按前 N 名口径评估。'
  }
  if (days === 0) {
    return `没有任何一天的命中数超过槽位数（${slots ?? '—'} 个），排序没起作用，这一列等于全池胜率。`
  }
  return (
    `有 ${days} 个交易日的命中数超过槽位数（${slots ?? '—'} 个），` +
    '这一列只统计这些日子里排序靠前、真正会成交的样本。'
  )
}

/** 噪声门槛：随机打乱标签重挖若干次，看纯运气能把「最好的那个条件」推到多高的 lift。 */
export function significanceTitle(
  candidate: ConditionResearchCandidate,
  permutations: number,
  trials?: ConditionResearchTrials,
): string {
  const threshold = candidate.mining.lift_threshold
  if (threshold === null || threshold === undefined) {
    return '这次研究没有做置换检验，无法判断这个提升是不是运气。'
  }
  const pvalue = candidate.mining.permutation_pvalue
  const p = pvalue === null || pvalue === undefined ? '—' : pvalue.toFixed(3)
  // 门槛是「随机重挖里最好成绩」分布的 95 分位，不是那批随机重挖的最好成绩
  // 本身 —— 后者要更极端一些，早期文案把它说成「纯运气最好的一次」是错的。
  let text =
    `把交易结果随机打乱重新挖掘 ${permutations} 次，` +
    `其中 95% 的随机重挖里「表现最好的那个条件」lift 都超不过 ${threshold.toFixed(3)}。` +
    `这个候选的 lift 是 ${candidate.mining.lift.toFixed(3)}，随机重挖里有 p=${p} 的比例达到或超过它。`
  // 上面那个 p 只回答「本次候选池里运气能推多高」。再往上还有一层：你在几段
  // 不同的数据上做过研究，最后记住的是那几轮里最好的一个。跑得越多，同一个 p
  // 越不值钱，这一层只能靠记账来补。
  const adjusted = candidate.mining.adjusted_pvalue
  if (trials?.adjusted && adjusted !== null && adjusted !== undefined) {
    text +=
      `你在 ${trials.trial_count} 段不同的数据上做过研究，同样的 p 撞出来的机会是 ${trials.trial_count} 倍，` +
      `校正后 p=${adjusted.toFixed(3)}（${adjusted > 0.05 ? '判为疑似噪声' : '仍判显著'}）。` +
      '徽标用的是校正后的 p。'
  } else {
    text += 'p 大于 0.05 就判为疑似噪声 —— 徽标和这里的 p 值是同一个判断，不会互相矛盾。'
  }
  return text
}

export function significanceLabel(candidate: ConditionResearchCandidate): string {
  if (candidate.mining.significant === true) return '显著'
  if (candidate.mining.significant === false) return '疑似噪声'
  return ''
}

export function validationTrades(
  report: ConditionResearchReport | null,
  candidate: ConditionResearchCandidate,
): string {
  const trades = candidate.validation.pooled_trades.closed_trades
  const threshold = report?.min_trades
  return threshold ? trades + ' / ' + threshold : String(trades)
}

export function constraintText(candidate: ConditionResearchCandidate): string {
  if (candidate.passed_constraints) return '通过验证'
  const failures = candidate.constraint_failures ?? []
  return failures.length ? failures.join(' · ') : '未入围'
}

/** 保持率的颜色：优势打反是红（危险），保持得多是深绿，未计入的是灰。 */
export function retentionClass(point: ConditionResearchSensitivityPoint): string {
  if (point.insufficient || point.degenerate) return 'sens-excluded'
  if (point.retention === null) return 'sens-excluded'
  if (point.retention < 0) return 'sens-flipped'
  if (point.retention >= 0.8) return 'sens-good'
  if (point.retention >= 0.5) return 'sens-mid'
  return 'sens-weak'
}

export function retentionText(value: number | null | undefined): string {
  return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(2) : '—'
}

export function sensitivityTitle(point: ConditionResearchSensitivityPoint): string {
  const direction =
    point.offset < 0 ? '更宽松' : point.offset > 0 ? '更严格' : '基点'
  const parts = [
    `分位偏移 ${(point.offset * 100).toFixed(0)}%（${direction}）`,
    `阈值 ${point.threshold.toFixed(4)}`,
    `命中样本 ${point.support_count}`,
  ]
  if (point.lift !== null) {
    parts.push(`胜率 ${formatPercent(point.outcome_rate)} / 基线 ${formatPercent(point.baseline_rate)}`)
    parts.push(`lift ${point.lift.toFixed(3)}`)
  }
  if (point.retention !== null) {
    parts.push(`保持率 ${point.retention.toFixed(2)}`)
  }
  if (point.degenerate) parts.push('阈值退化到恒真，不计入统计')
  if (point.insufficient) parts.push('命中样本不足，不计入统计')
  return parts.join(' · ')
}

export function rollingNote(candidate: ConditionResearchCandidate): string {
  const consistency = candidate.consistency
  if (!consistency) return ''
  const parts = [
    `${consistency.better_than_baseline}/${consistency.evaluated} 个窗口优于基线`,
  ]
  if (consistency.skipped > 0) {
    parts.push(`${consistency.skipped} 个窗口成交数不足，未计入`)
  }
  return parts.join('；')
}

export function rollingWindowBaseline(
  windows: { index: number; win_rate: number | null }[],
  index: number,
): number | null {
  return windows.find((item) => item.index === index)?.win_rate ?? null
}

export function summaryNumber(item: ResearchHistorySummary, key: string): number | null {
  const value = item.summary?.[key]
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

export function summaryText(item: ResearchHistorySummary, key: string): string {
  const value = item.summary?.[key]
  return typeof value === 'string' && value ? value : ''
}

/** 摘要里的条件只有 id，还原成和结果表一致的中文写法。 */
export function summaryConditionText(
  values: ValueDefinition[],
  item: ResearchHistorySummary,
): string {
  const raw = item.summary?.top_condition
  if (!raw || typeof raw !== 'object') return '—'
  const condition = raw as Record<string, unknown>
  const operator = OPERATOR_LABELS[String(condition.operator ?? '')] ?? String(condition.operator ?? '')
  const leftId = String(condition.left_value ?? '')
  const left = values.find((value) => value.id === leftId)
  const leftLabel = left ? valueOptionLabel(left) : leftId.replace('technical.', '')
  const rightId = typeof condition.right_value === 'string' ? condition.right_value : ''
  const right = rightId ? values.find((value) => value.id === rightId) : null
  const constant = typeof condition.right_constant === 'number' ? condition.right_constant : null
  return `${leftLabel} ${operator} ${right ? valueOptionLabel(right) : formatNumber(constant, 3)}`
}

export function historyRangeText(item: ResearchHistorySummary): string {
  const start = summaryText(item, 'start_date')
  const end = summaryText(item, 'end_date')
  if (!start && !end) return '区间未知'
  return `${start || '起点'} → ${end || '最新'}`
}

export function historyUniverseText(item: ResearchHistorySummary): string {
  const universe = summaryText(item, 'universe')
  if (universe === 'sh') return '仅上海'
  if (universe === 'sz') return '仅深圳'
  if (universe === 'all') return '沪深全部'
  return '范围未知'
}

export function historySideText(item: ResearchHistorySummary): string {
  const side = summaryText(item, 'search_sides')
  if (side === 'buy') return '只搜买入'
  if (side === 'sell') return '只搜卖出'
  if (side === 'both') return '买卖两侧'
  return '方向未知'
}

export function historyDurationText(item: ResearchHistorySummary): string {
  const seconds = item.duration_seconds
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) return ''
  if (seconds < 60) return `耗时 ${Math.round(seconds)} 秒`
  return `耗时 ${Math.round(seconds / 60)} 分钟`
}

// 历史记录的 created_at 存的是朴素 UTC 时间，直接渲染会比北京时间慢 8 小时，
// 让人误以为刚跑完的研究没保存。
export function formatHistoryDate(value: string): string {
  return formatChinaTimestamp(value)
}

export function phaseCount(
  state: { metadata?: Record<string, unknown> } | null,
  key: 'phase_current' | 'phase_total',
): number {
  const value = state?.metadata?.[key]
  return typeof value === 'number' && Number.isFinite(value) ? value : 0
}

/** 判断方案里的引用是否失效所需的上下文：当前元数据里真正存在的 id。 */
export interface ResearchPresetAvailability {
  valueIds: string[]
  signalIds: string[]
}

function ruleGroups(payload: Record<string, unknown>): Record<string, unknown>[] {
  const groups: Record<string, unknown>[] = []
  for (const key of ['buy_rule', 'sell_rule']) {
    const rule = payload[key]
    if (rule && typeof rule === 'object') groups.push(rule as Record<string, unknown>)
  }
  return groups
}

function conditionList(rule: Record<string, unknown>): ScreenCondition[] {
  const items: ScreenCondition[] = []
  for (const key of ['conditions', 'required_conditions']) {
    const value = rule[key]
    if (!Array.isArray(value)) continue
    for (const item of value) {
      if (item && typeof item === 'object') items.push(item as ScreenCondition)
    }
  }
  return items
}

/**
 * 方案里「引用了当前不存在的指标 / 信号」的项数。
 *
 * 加载方案时这些引用会被静默换成可用的指标，不讲出来用户会以为加载的还是原来
 * 那套条件。元数据还没加载完时一律返回 0 —— 那时根本没有判断依据，宁可什么都不
 * 说，也不要把整套条件报成失效。
 */
export function stalePresetReferences(
  payload: unknown,
  available: ResearchPresetAvailability,
): number {
  if (!payload || typeof payload !== 'object') return 0
  const record = payload as Record<string, unknown>
  const knownValues = new Set(available.valueIds)
  const knownSignals = new Set(available.signalIds)
  let stale = 0
  for (const rule of ruleGroups(record)) {
    const signals = Array.isArray(rule.signals) ? rule.signals : []
    if (knownSignals.size > 0) {
      stale += signals.filter(
        (item) => typeof item === 'string' && !knownSignals.has(item),
      ).length
    }
    if (knownValues.size === 0) continue
    for (const condition of conditionList(rule)) {
      const left = typeof condition.left_value === 'string' ? condition.left_value : ''
      const right = typeof condition.right_value === 'string' ? condition.right_value : null
      const broken = (left !== '' && !knownValues.has(left))
        || (right !== null && right !== '' && !knownValues.has(right))
      if (broken) stale += 1
    }
  }
  return stale
}

/** 加载方案后的提示。有失效项必须先说失效，否则「已载入」读起来像是原样还原。 */
export function presetLoadMessage(name: string, stale: number): string {
  if (stale > 0) {
    return `已载入研究方案「${name}」，但有 ${stale} 项引用了当前不存在的指标或信号、已被替换，请先核对条件再运行。`
  }
  return `已载入研究方案「${name}」，请确认后重新运行。`
}

/** 这套配置能不能存成方案：方案里不能带结果，只带输入。 */
export function buildPresetPayload(payload: ConditionResearchPayload): ConditionResearchPayload {
  return JSON.parse(JSON.stringify(payload)) as ConditionResearchPayload
}
