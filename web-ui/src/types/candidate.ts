/** candidate 相关的前端类型。 */

import type { SavedStrategyDetail } from './portfolio'

import type { ScreenCondition } from './screen'


// ---------------------------------------------------------------------------
// 候选库：挖出来的候选条件，带证据快照与采纳状态。
// ---------------------------------------------------------------------------

export type CandidateStatus = 'pending' | 'adopted' | 'rejected' | 'published'


/** 候选在某一段（验证期 / 留出期）上的可比读数。 */
export interface CandidatePhaseSummary {
  trade_count?: number | null
  win_rate?: number | null
  improved_win_rate?: boolean | null
  improved_stock_ratio?: number | null
}


/** 滚动复验：同一批候选在留出期的多个连续窗口里重放的结果。 */
export interface CandidateConsistency {
  requested?: number | null
  used?: number | null
  evaluated?: number | null
  better_than_baseline?: number | null
  note?: string | null
}


/** 入库那一刻的证据快照。它不回指历史记录——历史只留最近 20 条。 */
export interface CandidateEvidence {
  lift: number | null
  support_count: number | null
  instrument_count: number | null
  outcome_rate: number | null
  baseline_rate: number | null
  support_ratio: number | null
  permutation_pvalue: number | null
  /** 三选一：true 通过检验 / false 未通过 / null 这次研究没做检验。 */
  significant: boolean | null
  lift_threshold: number | null
  top_n_outcome_rate: number | null
  top_n_filtered_days: number | null
  median_retention: number | null
  worst_retention: number | null
  passed_constraints: boolean | null
  constraint_failures: string[]
  created_at: string
  validation?: CandidatePhaseSummary | null
  holdout?: CandidatePhaseSummary | null
  consistency?: CandidateConsistency | null
  study: {
    start_date?: string | null
    end_date?: string | null
    scanned_stocks?: number | null
    candidate_count?: number | null
    min_trades?: number | null
    permutations?: number | null
    created_at?: string
  }
}


export interface CandidateLibraryItem {
  candidate_id: string
  source: string
  source_history_id: string
  source_candidate_id: string
  side: 'buy' | 'sell'
  label: string
  condition: ScreenCondition
  evidence: CandidateEvidence
  status: CandidateStatus
  note: string
  published_strategy_id: string | null
  created_at: string
  updated_at: string
}


/** 发布预览：把将要写进策略库的那套配置先摊开给人看。 */
export interface CandidatePublishPreview {
  candidate_id: string
  label: string
  side: string
  status: CandidateStatus
  request: Record<string, unknown>
  /** 配置已通过组合回测的校验。false 时 request 是还没校验的拼装结果。 */
  validated: boolean
  notes: string[]
  blockers: string[]
  blocked: boolean
  ready: boolean
}


export interface CandidatePublishOutcome {
  candidate: CandidateLibraryItem
  strategy: SavedStrategyDetail
  /** 恒为 false：发布这条路径里没有任何地方会把策略上线。 */
  online: boolean
  notes: string[]
  message: string
}
