export function formatTradingRule(rule: Record<string, unknown> | null | undefined, name: (id: string) => string): string {
  if (!rule) return '—'
  const operators: Record<string, string> = { gt: '>', gte: '≥', lt: '<', lte: '≤', eq: '=', neq: '≠', cross_up: '上穿', cross_down: '下穿' }
  const conditions = (items: unknown) => Array.isArray(items) ? items.flatMap((item) => {
    if (!item || typeof item !== 'object') return []
    const c = item as Record<string, unknown>
    const right = typeof c.right_value === 'string' ? name(c.right_value) : String(c.right_constant ?? '')
    return [`${name(String(c.left_value ?? ''))} ${operators[String(c.operator)] ?? c.operator} ${right}`]
  }) : []
  const signals = Array.isArray(rule.signals) ? rule.signals.filter((id): id is string => typeof id === 'string').map(name) : []
  const parts = [...signals, ...conditions(rule.conditions)]
  const required = conditions(rule.required_conditions)
  const text = rule.combine_mode === 'at_least'
    ? `至少满足 ${rule.minimum_matches} 项：${parts.join('；')}`
    : parts.join(rule.combine_mode === 'any' ? ' 或 ' : ' 且 ')
  return [...(required.length ? [`必选：${required.join(' 且 ')}`] : []), ...(text ? [text] : [])].join('；') || '—'
}

export function formatRiskControls(config: Record<string, unknown>): string {
  return [ ['止损','stop_loss'], ['止盈','take_profit'], ['移动止盈','trailing_stop'] ].map(([label, key]) => {
    const pct = config[`${key}_pct`]
    const atr = config[`${key}_atr_multiplier`]
    const mode = config[`${key}_mode`] ?? 'pct'
    const cooldown = config[`${key}_cooldown_days`]
    const suffix = typeof cooldown === 'number' && cooldown > 0 ? `/冷却${cooldown}日` : ''
    if (mode === 'atr' && typeof atr === 'number') return `${label} ATR×${atr}（${config.atr_period ?? 14}日）${suffix}`
    if (mode !== 'atr' && typeof pct === 'number') return `${label} ${Number((pct * 100).toFixed(6))}%${suffix}`
    return `${label}关闭`
  }).join(' · ')
}
