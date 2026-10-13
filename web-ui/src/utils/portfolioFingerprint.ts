/**
 * 风险寻优候选能否应用到当前表单的判定依据：基础配置指纹。
 *
 * 寻优跑的是「提交那一刻的基础回测配置 + max_trials」。之后用户改了买卖规则、
 * 排序或费用，榜单上的候选就不再对应眼前这份配置——把基础配置（剔除风险参数
 * 与日期区间）做稳定序列化指纹，前后一比就知道表单有没有被改过。
 * exclude_st 单独算一份：它被排除在指纹之外，用于「排除 ST 与否」的宽松比对。
 */

const RISK_PARAMETER_KEYS = new Set([
  'stop_loss_mode', 'stop_loss_pct', 'stop_loss_atr_multiplier', 'stop_loss_cooldown_days',
  'take_profit_mode', 'take_profit_pct', 'take_profit_atr_multiplier', 'take_profit_cooldown_days',
  'trailing_stop_mode', 'trailing_stop_pct', 'trailing_stop_atr_multiplier', 'trailing_stop_cooldown_days',
  'max_hold_days',
])

/** 键排序后的确定性 JSON 序列化：同一份配置无论字段顺序如何，指纹一致。 */
function stableStringify(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableStringify).join(',')}]`
  if (value && typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left.localeCompare(right))
    return `{${entries.map(([key, item]) => `${JSON.stringify(key)}:${stableStringify(item)}`).join(',')}}`
  }
  return JSON.stringify(value) ?? 'null'
}

export function portfolioBaseFingerprint(
  payload: unknown,
  ignoreExcludeSt = false,
): string {
  const fixed = Object.fromEntries(
    Object.entries((payload ?? {}) as Record<string, unknown>).filter(([key]) => (
      !RISK_PARAMETER_KEYS.has(key)
      && key !== 'date_mode'
      && key !== 'start_date'
      && key !== 'end_date'
      && !(ignoreExcludeSt && key === 'exclude_st')
    )),
  )
  return stableStringify(fixed)
}
