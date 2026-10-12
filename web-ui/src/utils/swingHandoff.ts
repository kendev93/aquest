import type { RankOrder, RuleGroupPayload } from '../types'

/** 波段研究把结论递给买卖条件研究的临时钥匙（页间跳转用，读一次就删）。 */
export const SWING_PLAN_STORAGE_KEY = 'swing-research-plan'

export interface SwingPlanHandoff {
  buy_rule: RuleGroupPayload | null
  sell_rule: RuleGroupPayload | null
  ranking: { value: string; order: RankOrder; display_name: string } | null
}

export function saveSwingPlan(plan: SwingPlanHandoff): void {
  try {
    sessionStorage.setItem(SWING_PLAN_STORAGE_KEY, JSON.stringify(plan))
  } catch {
    // 隐私模式下 sessionStorage 可能不可用，此时用户只能手动复制规则，不影响其他功能。
  }
}

export function consumeSwingPlan(): SwingPlanHandoff | null {
  try {
    const raw = sessionStorage.getItem(SWING_PLAN_STORAGE_KEY)
    if (!raw) return null
    sessionStorage.removeItem(SWING_PLAN_STORAGE_KEY)
    const parsed = JSON.parse(raw) as Partial<SwingPlanHandoff>
    return {
      buy_rule: parsed.buy_rule ?? null,
      sell_rule: parsed.sell_rule ?? null,
      ranking: parsed.ranking ?? null,
    }
  } catch {
    return null
  }
}
