/** 候选库的展示逻辑。所有"该说什么、该怎么说"的判断都在这里，页面只负责摆。 */

import type {
  CandidateEvidence,
  CandidateLibraryItem,
  CandidatePublishPreview,
  CandidateStatus,
} from '../types'

const PLACEHOLDER = '—'

export const STATUS_LABELS: Record<CandidateStatus, string> = {
  pending: '待定',
  adopted: '已采纳',
  rejected: '已否决',
  published: '已发布',
}

export function statusLabel(status: CandidateStatus | string): string {
  return STATUS_LABELS[status as CandidateStatus] ?? status
}

export function statusTone(status: CandidateStatus | string): 'flat' | 'adopted' | 'rejected' | 'published' {
  if (status === 'adopted') return 'adopted'
  if (status === 'rejected') return 'rejected'
  if (status === 'published') return 'published'
  return 'flat'
}

export function sideLabel(side: string): string {
  if (side === 'buy') return '买入过滤'
  if (side === 'sell') return '卖出触发'
  return PLACEHOLDER
}

/** 提升倍数：直接写倍数，不折算成"涨了百分之多少"——lift 是比值不是涨幅。 */
export function formatLift(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return PLACEHOLDER
  return `${value.toFixed(2)} 倍`
}

export function formatPercent(value: number | null, digits = 1): string {
  if (value === null || !Number.isFinite(value)) return PLACEHOLDER
  return `${(value * 100).toFixed(digits)}%`
}

export function formatCount(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return PLACEHOLDER
  return String(Math.round(value))
}

export function formatPValue(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return PLACEHOLDER
  if (value < 0.001) return '< 0.001'
  return value.toFixed(3)
}

/**
 * 显著性只说三句话。
 *
 * 不写"有效"这种模糊词：通过了置换检验只说明"不像碰巧押中某段行情"，不说明
 * 它值得交易，更不说明它在别的票上还成立。
 */
export function significanceText(evidence: CandidateEvidence): string {
  if (evidence.significant === true) return '通过置换检验'
  if (evidence.significant === false) return '未通过置换检验'
  return '这次研究没做置换检验'
}

export function significanceTone(evidence: CandidateEvidence): 'up' | 'muted' | 'warn' {
  if (evidence.significant === true) return 'up'
  if (evidence.significant === false) return 'muted'
  return 'warn'
}

/** 滚动复验：几窗里几窗优于基线。样本不够的窗后端已经剔除，直接读 evaluated。 */
export function consistencyText(evidence: CandidateEvidence): string {
  const consistency = evidence.consistency
  if (!consistency) return ''
  const better = consistency.better_than_baseline ?? null
  const evaluated = consistency.evaluated ?? null
  if (better === null || evaluated === null || evaluated === 0) return ''
  const parts = [`${evaluated} 窗中 ${better} 窗优于基线`]
  if (consistency.note) parts.push(consistency.note)
  return parts.join('；')
}

/** 参数敏感性保持率：阈值挪一点，优势还剩多少。 */
export function retentionText(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return ''
  return `阈值挪一档仍保留 ${formatPercent(value, 0)}`
}

export function studyRangeText(evidence: CandidateEvidence): string {
  const start = evidence.study?.start_date
  const end = evidence.study?.end_date
  if (!start || !end) return ''
  return `${start} ~ ${end}`
}

export function studyScaleText(evidence: CandidateEvidence): string {
  const scanned = evidence.study?.scanned_stocks ?? null
  if (scanned === null) return ''
  return `扫过 ${formatCount(scanned)} 只`
}

/** 卡片上的证据行：拿不到就显示「—」，绝不用 0 冒充。 */
export function evidenceRows(item: CandidateLibraryItem): { label: string; value: string }[] {
  const evidence = item.evidence
  const rows = [
    { label: '提升倍数', value: formatLift(evidence.lift) },
    { label: '显著性', value: significanceText(evidence) },
    { label: '置换 p 值', value: formatPValue(evidence.permutation_pvalue) },
    { label: '命中样本 / 覆盖股票', value: `${formatCount(evidence.support_count)} / ${formatCount(evidence.instrument_count)}` },
    { label: '命中率 vs 基线', value: `${formatPercent(evidence.outcome_rate)} vs ${formatPercent(evidence.baseline_rate)}` },
  ]
  if (evidence.validation?.win_rate !== null && evidence.validation?.win_rate !== undefined) {
    rows.push({ label: '验证期胜率', value: formatPercent(evidence.validation.win_rate) })
  }
  if (evidence.holdout?.win_rate !== null && evidence.holdout?.win_rate !== undefined) {
    rows.push({ label: '留出期胜率', value: formatPercent(evidence.holdout.win_rate) })
  }
  const consistency = consistencyText(evidence)
  if (consistency) rows.push({ label: '滚动复验', value: consistency })
  const retention = retentionText(evidence.median_retention)
  if (retention) rows.push({ label: '参数敏感性', value: retention })
  return rows
}

/** 这条候选当下能做什么。返回的是动作，不是"可以让它做什么"的暗示。 */
export function availableActions(status: CandidateStatus): CandidateStatus[] {
  if (status === 'pending') return ['adopted', 'rejected']
  if (status === 'adopted') return ['pending', 'rejected']
  if (status === 'rejected') return ['pending']
  return ['adopted']
}

/**
 * 发布按钮为什么是灰的。
 *
 * 返回空串表示可以发布。理由要写给人看（"先采纳"而不是"状态不符"），否则用户
 * 只会以为按钮坏了。
 */
export function publishBlockedReason(
  item: CandidateLibraryItem | null,
  preview: CandidatePublishPreview | null,
): string {
  if (!item) return '请先选择一条候选'
  if (item.status === 'pending') return '发布前必须先采纳：通过检验是统计结论，采纳是你的表态'
  if (item.status === 'rejected') return '这条已被否决，先恢复成待定再决定'
  if (item.status === 'published') return '已发布过。要换配置请先撤回发布，再重新发布'
  if (preview && preview.blocked) return preview.blockers[0] ?? '这套配置还不能发布'
  return ''
}

/** 这套配置差一个排序规则：得让人选，不能替他填一个。 */
export function previewNeedsRanking(preview: CandidatePublishPreview | null): boolean {
  if (!preview?.blocked) return false
  return preview.blockers.some((item) => item.includes('排序'))
}

/** 这套配置没有离场规则：止损/止盈必须由人给，否则回测只买不卖。 */
export function previewNeedsExit(preview: CandidatePublishPreview | null): boolean {
  if (!preview?.blocked) return false
  return preview.blockers.some((item) => item.includes('离场'))
}

/** 发布确认区顶部那句最重要的话：发布不等于上线。 */
export function publishWarningText(): string {
  return '发布只会把这条条件变成策略库里的一条策略，不会上线、不会自动开始观察。上线要你自己到策略追踪页启动——因为上线日会冻结研究段基线。'
}

export function importSummaryText(added: number, skipped: number): string {
  if (added && skipped) return `入库 ${added} 条，跳过 ${skipped} 条。`
  if (added) return `已入库 ${added} 条候选，状态均为「待定」。`
  if (skipped) return `这 ${skipped} 条已经在库里或这次研究里找不到，没有重复入库。`
  return '没有候选被入库。'
}
