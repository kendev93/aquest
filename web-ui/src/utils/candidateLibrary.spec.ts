import { describe, expect, it } from 'vitest'

import type {
  CandidateEvidence,
  CandidateLibraryItem,
  CandidatePublishPreview,
} from '../types'
import {
  availableActions,
  consistencyText,
  evidenceRows,
  formatCount,
  formatLift,
  formatPercent,
  formatPValue,
  importSummaryText,
  previewNeedsExit,
  previewNeedsRanking,
  publishBlockedReason,
  publishWarningText,
  retentionText,
  sideLabel,
  significanceText,
  significanceTone,
  statusLabel,
  statusTone,
  studyRangeText,
  studyScaleText,
} from './candidateLibrary'

function evidence(overrides: Partial<CandidateEvidence> = {}): CandidateEvidence {
  return {
    lift: 1.8,
    support_count: 320,
    instrument_count: 120,
    outcome_rate: 0.62,
    baseline_rate: 0.5,
    support_ratio: 0.08,
    permutation_pvalue: 0.02,
    significant: true,
    lift_threshold: 1.5,
    top_n_outcome_rate: 0.6,
    top_n_filtered_days: 40,
    median_retention: 0.7,
    worst_retention: 0.4,
    passed_constraints: true,
    constraint_failures: [],
    created_at: '2026-09-20 10:00:00',
    study: { start_date: '2024-01-02', end_date: '2026-09-10', scanned_stocks: 5045 },
    ...overrides,
  }
}

function item(overrides: Partial<CandidateLibraryItem> = {}): CandidateLibraryItem {
  return {
    candidate_id: 'abc',
    source: 'condition_research',
    source_history_id: 'study-1',
    source_candidate_id: 'buy-1',
    side: 'buy',
    label: '乖离率20 ≤ -5',
    condition: { left_value: 'technical.bias20', operator: 'lte', right_value: null, right_constant: -5 },
    evidence: evidence(),
    status: 'pending',
    note: '',
    published_strategy_id: null,
    created_at: '2026-09-20 10:00:00',
    updated_at: '2026-09-20 10:00:00',
    ...overrides,
  }
}

function preview(overrides: Partial<CandidatePublishPreview> = {}): CandidatePublishPreview {
  return {
    candidate_id: 'abc',
    label: '乖离率20 ≤ -5',
    side: 'buy',
    status: 'adopted',
    request: {},
    validated: true,
    notes: [],
    blockers: [],
    blocked: false,
    ready: true,
    ...overrides,
  }
}

describe('读数格式', () => {
  it('拿不到就显示占位符，绝不用 0 冒充', () => {
    expect(formatLift(null)).toBe('—')
    expect(formatPercent(null)).toBe('—')
    expect(formatCount(null)).toBe('—')
    expect(formatPValue(null)).toBe('—')
    expect(formatLift(Number.NaN)).toBe('—')
  })

  it('提升倍数写倍数不写涨幅', () => {
    expect(formatLift(1.8)).toBe('1.80 倍')
    expect(formatPercent(0.6234)).toBe('62.3%')
    expect(formatCount(320.4)).toBe('320')
    expect(formatPValue(0.0004)).toBe('< 0.001')
    expect(formatPValue(0.02)).toBe('0.020')
  })
})

describe('显著性只说三句话', () => {
  it('通过 / 未通过 / 没做检验', () => {
    expect(significanceText(evidence({ significant: true }))).toBe('通过置换检验')
    expect(significanceText(evidence({ significant: false }))).toBe('未通过置换检验')
    expect(significanceText(evidence({ significant: null }))).toBe('这次研究没做置换检验')
  })

  it('语气随结论走', () => {
    expect(significanceTone(evidence({ significant: true }))).toBe('up')
    expect(significanceTone(evidence({ significant: false }))).toBe('muted')
    expect(significanceTone(evidence({ significant: null }))).toBe('warn')
  })
})

describe('状态与动作', () => {
  it('状态名与语气', () => {
    expect(statusLabel('pending')).toBe('待定')
    expect(statusLabel('published')).toBe('已发布')
    expect(statusTone('adopted')).toBe('adopted')
    expect(statusTone('rejected')).toBe('rejected')
    expect(statusTone('published')).toBe('published')
    expect(statusTone('pending')).toBe('flat')
  })

  it('待定只能采纳或否决，已发布只能撤回', () => {
    expect(availableActions('pending')).toEqual(['adopted', 'rejected'])
    expect(availableActions('adopted')).toEqual(['pending', 'rejected'])
    expect(availableActions('rejected')).toEqual(['pending'])
    expect(availableActions('published')).toEqual(['adopted'])
  })

  it('方向标签', () => {
    expect(sideLabel('buy')).toBe('买入过滤')
    expect(sideLabel('sell')).toBe('卖出触发')
    expect(sideLabel('other')).toBe('—')
  })
})

describe('发布前的拦与放', () => {
  it('没采纳就不许发布，理由是给人看的', () => {
    expect(publishBlockedReason(item({ status: 'pending' }), preview())).toContain('先采纳')
    expect(publishBlockedReason(item({ status: 'rejected' }), preview())).toContain('已被否决')
    expect(publishBlockedReason(item({ status: 'published' }), preview())).toContain('撤回发布')
  })

  it('已采纳且配置没问题时可以发布', () => {
    expect(publishBlockedReason(item({ status: 'adopted' }), preview())).toBe('')
  })

  it('配置缺东西时把第一条拦着的原因说出来', () => {
    const blocked = preview({ blocked: true, ready: false, blockers: ['缺排序规则'] })
    expect(publishBlockedReason(item({ status: 'adopted' }), blocked)).toBe('缺排序规则')
  })

  it('没有候选时也有说法', () => {
    expect(publishBlockedReason(null, null)).toContain('请先选择')
  })

  it('区分「缺排序」和「缺离场」', () => {
    expect(previewNeedsRanking(preview({ blocked: true, blockers: ['缺排序规则'] }))).toBe(true)
    expect(previewNeedsExit(preview({ blocked: true, blockers: ['缺排序规则'] }))).toBe(false)
    expect(previewNeedsExit(preview({ blocked: true, blockers: ['不设离场规则就永远不卖'] }))).toBe(true)
    expect(previewNeedsRanking(preview())).toBe(false)
    expect(previewNeedsExit(null)).toBe(false)
  })

  it('警告里明确写不会上线', () => {
    expect(publishWarningText()).toContain('不会上线')
  })
})

describe('证据行', () => {
  it('有值才出这一行，没有就不硬凑', () => {
    const rows = evidenceRows(item())
    expect(rows.map((row) => row.label)).toContain('提升倍数')
    expect(rows.map((row) => row.label)).toContain('显著性')

    const bare = evidenceRows(
      item({
        evidence: evidence({
          validation: null,
          holdout: null,
          consistency: null,
          median_retention: null,
        }),
      }),
    )
    expect(bare.map((row) => row.label)).not.toContain('验证期胜率')
    expect(bare.map((row) => row.label)).not.toContain('滚动复验')
  })

  it('滚动复验写成几窗中几窗优于基线', () => {
    expect(
      consistencyText(evidence({ consistency: { evaluated: 3, better_than_baseline: 2 } })),
    ).toBe('3 窗中 2 窗优于基线')
    expect(consistencyText(evidence({ consistency: { evaluated: 0, better_than_baseline: 0 } }))).toBe('')
    expect(consistencyText(evidence())).toBe('')
  })

  it('敏感性写成保留了多少', () => {
    expect(retentionText(0.7)).toBe('阈值挪一档仍保留 70%')
    expect(retentionText(null)).toBe('')
  })

  it('溯源区间与规模', () => {
    expect(studyRangeText(evidence())).toBe('2024-01-02 ~ 2026-09-10')
    expect(studyScaleText(evidence())).toBe('扫过 5045 只')
    expect(studyRangeText(evidence({ study: {} }))).toBe('')
    expect(studyScaleText(evidence({ study: {} }))).toBe('')
  })
})

describe('入库结果提示', () => {
  it('分别说清入库几条、跳过几条', () => {
    expect(importSummaryText(2, 0)).toContain('已入库 2 条')
    expect(importSummaryText(0, 2)).toContain('没有重复入库')
    expect(importSummaryText(1, 1)).toContain('跳过 1 条')
    expect(importSummaryText(0, 0)).toContain('没有候选被入库')
  })
})
