import type { RuleGroupPayload } from '../types'

export const PORTFOLIO_BACKTEST_HANDOFF_KEY = 'indicator-lab.portfolio-backtest.handoff'

export type PortfolioBacktestHandoff =
  | {
      kind: 'stock-pool'
      source: 'formula-screen'
      source_label: string
      codes: string[]
      ranking_value: string | null
      rank_order: 'asc' | 'desc'
      mode: 'preset' | 'custom'
      formula_text: string | null
      formula_parameters: Record<string, number>
      signal_date: number | null
    }
  | {
      kind: 'rules'
      source: 'swing-research'
      source_label: string
      buy_rule: RuleGroupPayload | null
      sell_rule: RuleGroupPayload | null
      ranking_value: string | null
      rank_order: 'asc' | 'desc'
    }
  | {
      kind: 'ranking'
      source: 'factor-test' | 'factor-attribution'
      source_label: string
      value_id: string
      rank_order: 'asc' | 'desc'
      evidence: string
    }

/** Store a one-shot handoff for the portfolio page. */
export function savePortfolioBacktestHandoff(handoff: PortfolioBacktestHandoff): boolean {
  try {
    sessionStorage.setItem(PORTFOLIO_BACKTEST_HANDOFF_KEY, JSON.stringify(handoff))
    return true
  } catch {
    return false
  }
}

/** Read and clear a handoff so refreshing the destination cannot reapply it. */
export function consumePortfolioBacktestHandoff(): PortfolioBacktestHandoff | null {
  let raw: string | null = null
  try {
    raw = sessionStorage.getItem(PORTFOLIO_BACKTEST_HANDOFF_KEY)
    if (!raw) return null
    const value = JSON.parse(raw) as Partial<PortfolioBacktestHandoff>
    if (!value || typeof value !== 'object') return null
    if (value.kind === 'stock-pool' && value.source === 'formula-screen') {
      if (!Array.isArray(value.codes) || !value.codes.some((code) => typeof code === 'string' && code.trim())) {
        return null
      }
      return value as PortfolioBacktestHandoff
    }
    if (value.kind === 'rules' && value.source === 'swing-research') {
      return value as PortfolioBacktestHandoff
    }
    if (
      value.kind === 'ranking'
      && (value.source === 'factor-test' || value.source === 'factor-attribution')
      && typeof value.value_id === 'string'
      && value.value_id.trim()
    ) {
      return value as PortfolioBacktestHandoff
    }
    return null
  } catch {
    return null
  } finally {
    if (raw) {
      try {
        sessionStorage.removeItem(PORTFOLIO_BACKTEST_HANDOFF_KEY)
      } catch {
        // Storage may be unavailable in privacy-restricted browser contexts.
      }
    }
  }
}
