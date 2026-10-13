/**
 * 组合回测页的所有跨页桥接与回填：
 * 策略实验室候选、买卖条件研究、波段研究、自选股池的带入，
 * 以及「把买入条件带到公式选股」的反向输出。
 *
 * 这一层的共同点是深度写 form —— 每个来源的 payload 形状不同，但都要翻译成
 * 页面表单字段，翻译规则（哪些字段认、哪些字段保留页面现值、缺省怎么措辞）
 * 全在这里。复盘比对（compareAppliedCandidate）也在这层：它回答「应用候选后
 * 重跑，结果是否与策略发现时一致」。
 */

import type { Ref } from 'vue'

import { parseStoredRuleGroup } from '../utils/formulaScreen'
import { BOARD_OPTIONS, INSTRUMENT_TYPE_OPTIONS } from '../utils/instrumentOptions'
import type {
  AppliedDiscoveryCandidate,
  InstrumentBoard,
  InstrumentType,
  PortfolioBacktestResult,
  PortfolioFormState,
  PortfolioReplayComparison,
  PortfolioTrade,
  ValueDefinition,
} from '../types'
import type { PortfolioBacktestHandoff } from '../utils/portfolioBacktestHandoff'

export interface PortfolioHandoffDeps {
  form: PortfolioFormState
  /** 校验错误集合：回填成功后清空，避免旧错误挂在改过的字段上。 */
  errors: Ref<Record<string, string>>
  /** 页面顶部的消息条。 */
  message: Ref<string>
  /** 当前可用的指标输出，用于判断 handoff 带来的排序指标是否 rankable。 */
  availableValues: Ref<ValueDefinition[]>
  /** 自定义公式解析（带回研究配置时先解析再回填）。 */
  parseCustomFormula: () => Promise<void>
  /** 自定义公式解析结果；未解析成功时应用候选要降级措辞。 */
  customMetadata: Ref<import('../types').CustomFormulaMetadata | null>
  /** 已应用的发现候选标注（compareAppliedCandidate 的输入）。 */
  appliedDiscoveryCandidate: Ref<AppliedDiscoveryCandidate | null>
  /** 复现比对结论（compareAppliedCandidate 的输出）。 */
  replayComparison: Ref<PortfolioReplayComparison | null>
}

function finiteNumber(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback
}

function enumValue<T extends string>(value: unknown, allowed: readonly T[], fallback: T): T {
  return typeof value === 'string' && allowed.includes(value as T) ? value as T : fallback
}

export function usePortfolioHandoff(deps: PortfolioHandoffDeps) {
  const { form } = deps

  // 组合回测 → 公式选股的交接键。这个方向只有本模块在用，键就定义在这。
  const FORMULA_SCREEN_HANDOFF_KEY = 'aquest.condition-research.to-screen'

  /** 策略实验室候选 → 表单。覆盖规则、排序、风控与成本；日期区间不动。 */
  function applyStoredCandidate(raw: unknown): void {
  if (!raw || typeof raw !== 'object') return
  const config = raw as Record<string, unknown>
  const universe = enumValue(config.universe, ['all', 'sh', 'sz', 'custom', 'codes'] as const, form.universe)
  form.universe = universe
  form.universeFile = typeof config.universe_file === 'string' ? config.universe_file : ''
  form.codesText = Array.isArray(config.codes) ? config.codes.filter((item): item is string => typeof item === 'string').join('\n') : ''
  form.positionMode = enumValue(config.position_mode, ['full', 'fixed'] as const, form.positionMode)
  form.fixedSize = Math.round(finiteNumber(config.fixed_size, form.fixedSize))
  form.excludeSt = typeof config.exclude_st === 'boolean' ? config.exclude_st : true
  form.instrumentTypes = Array.isArray(config.instrument_types)
    ? config.instrument_types.filter((item): item is InstrumentType => INSTRUMENT_TYPE_OPTIONS.some((option) => option.key === item))
    : []
  form.boards = Array.isArray(config.boards)
    ? config.boards.filter((item): item is InstrumentBoard => BOARD_OPTIONS.some((option) => option.key === item))
    : []
  form.startDate = typeof config.start_date === 'string' ? config.start_date : form.startDate
  form.endDate = typeof config.end_date === 'string' ? config.end_date : form.endDate
  const maxPositions = finiteNumber(config.max_positions, form.maxPositions)
  if (Number.isInteger(maxPositions) && maxPositions >= 1 && maxPositions <= 100) form.maxPositions = maxPositions
  if (typeof config.ranking_value === 'string' && config.ranking_value.trim()) form.rankingValue = config.ranking_value
  form.rankOrder = enumValue(config.rank_order, ['asc', 'desc'] as const, form.rankOrder)
  form.rebalanceFrequency = enumValue(config.rebalance_frequency, ['daily', 'weekly', 'monthly'] as const, form.rebalanceFrequency)
  const buyRule = parseStoredRuleGroup(config.buy_rule)
  const sellRule = parseStoredRuleGroup(config.sell_rule)
  if (buyRule) form.buyRule = buyRule
  if (sellRule) form.sellRule = sellRule

  const applyRisk = (prefix: 'stop_loss' | 'take_profit' | 'trailing_stop', enabledKey: 'stopLossEnabled' | 'takeProfitEnabled' | 'trailingStopEnabled', modeKey: 'stopLossMode' | 'takeProfitMode' | 'trailingStopMode', pctKey: 'stopLossPct' | 'takeProfitPct' | 'trailingStopPct', atrKey: 'stopLossAtrMultiplier' | 'takeProfitAtrMultiplier' | 'trailingStopAtrMultiplier', cooldownKey: 'stopLossCooldownDays' | 'takeProfitCooldownDays' | 'trailingStopCooldownDays'): void => {
    const pct = finiteNumber(config[`${prefix}_pct`], Number.NaN)
    const atr = finiteNumber(config[`${prefix}_atr_multiplier`], Number.NaN)
    const mode = enumValue(config[`${prefix}_mode`], ['pct', 'atr'] as const, Number.isFinite(atr) ? 'atr' : 'pct')
    form[modeKey] = mode
    form[pctKey] = Number.isFinite(pct) ? pct * 100 : form[pctKey]
    form[atrKey] = Number.isFinite(atr) ? atr : form[atrKey]
    form[cooldownKey] = Math.max(0, Math.trunc(finiteNumber(config[`${prefix}_cooldown_days`], 0)))
    form[enabledKey] = Number.isFinite(pct) || Number.isFinite(atr)
  }
  applyRisk('stop_loss', 'stopLossEnabled', 'stopLossMode', 'stopLossPct', 'stopLossAtrMultiplier', 'stopLossCooldownDays')
  applyRisk('take_profit', 'takeProfitEnabled', 'takeProfitMode', 'takeProfitPct', 'takeProfitAtrMultiplier', 'takeProfitCooldownDays')
  applyRisk('trailing_stop', 'trailingStopEnabled', 'trailingStopMode', 'trailingStopPct', 'trailingStopAtrMultiplier', 'trailingStopCooldownDays')
  form.atrPeriod = 14
  const maxHoldDays = finiteNumber(config.max_hold_days, Number.NaN)
  form.maxHoldDaysEnabled = Number.isFinite(maxHoldDays) && maxHoldDays >= 1
  if (form.maxHoldDaysEnabled) form.maxHoldDays = Math.round(maxHoldDays)
  form.initialCash = finiteNumber(config.initial_cash, form.initialCash)
  form.commission = finiteNumber(config.commission, form.commission)
  form.minCommission = finiteNumber(config.min_commission, form.minCommission)
  form.stampTax = finiteNumber(config.stamp_tax, form.stampTax)
  form.slippage = finiteNumber(config.slippage, form.slippage)
  form.execution = enumValue(config.execution, ['next_open', 'next_close'] as const, form.execution)
  deps.errors.value = {}
  deps.message.value = '已载入策略实验室候选配置，请确认后开始普通组合回测。'
}

/**
 * 从买卖条件研究页过来：规则 + 排序 + 槽位。
 *
 * 自定义公式必须先解析再回填 —— parseCustomFormula 会把买卖规则和排序值
 * 重置成公式的默认输出，顺序反了用户填的东西就没了（踩过一次）。
 */
async function applyStoredResearch(raw: unknown, notice?: string): Promise<void> {
  if (!raw || typeof raw !== 'object') return
  const config = raw as Record<string, unknown>
  const formulaText = typeof config.formula_text === 'string' ? config.formula_text.trim() : ''
  if (formulaText) {
    form.mode = 'custom'
    form.formulaText = formulaText
    const parameters = config.formula_parameters
    if (parameters && typeof parameters === 'object') {
      const entries = Object.entries(parameters as Record<string, unknown>)
        .filter((entry): entry is [string, number] => typeof entry[1] === 'number' && Number.isFinite(entry[1]))
      form.formulaParameters = Object.fromEntries(entries)
    }
    await deps.parseCustomFormula()
  } else if (form.mode !== 'preset') {
    form.mode = 'preset'
  }

  if (typeof config.universe === 'string') {
    // 'codes' 也要认：条件研究不会传它，但策略库保存的配置可能就是指定标的的。
    form.universe = enumValue(config.universe, ['all', 'sh', 'sz', 'custom', 'codes'] as const, form.universe)
  }
  if (Array.isArray(config.codes)) {
    form.codesText = config.codes.filter((item): item is string => typeof item === 'string').join('\n')
  }
  form.excludeSt = typeof config.exclude_st === 'boolean' ? config.exclude_st : true
  form.instrumentTypes = Array.isArray(config.instrument_types)
    ? config.instrument_types.filter((item): item is InstrumentType => INSTRUMENT_TYPE_OPTIONS.some((option) => option.key === item))
    : form.instrumentTypes
  form.boards = Array.isArray(config.boards)
    ? config.boards.filter((item): item is InstrumentBoard => BOARD_OPTIONS.some((option) => option.key === item))
    : form.boards
  const maxPositions = finiteNumber(config.max_positions, form.maxPositions)
  if (Number.isInteger(maxPositions) && maxPositions >= 1 && maxPositions <= 100) {
    form.maxPositions = maxPositions
  }
  if (typeof config.ranking_value === 'string' && config.ranking_value.trim()) {
    form.rankingValue = config.ranking_value
  }
  form.rankOrder = enumValue(config.rank_order, ['asc', 'desc'] as const, form.rankOrder)
  // 日期要连 mode 一起还原：只回填绝对日期会让下拉仍显示「最近五年」，
  // 而研究跑的其实是全历史。
  const dateMode = enumValue(config.date_mode, ['recent5', 'full', 'custom'] as const, form.dateMode)
  if (dateMode === 'custom') {
    const start = typeof config.start_date === 'string' ? config.start_date : ''
    const end = typeof config.end_date === 'string' ? config.end_date : ''
    if (start && end) {
      form.dateMode = 'custom'
      form.startDate = start
      form.endDate = end
    }
  } else {
    form.dateMode = dateMode
  }
  const buyRule = parseStoredRuleGroup(config.buy_rule)
  if (buyRule) form.buyRule = buyRule
  const sellRule = parseStoredRuleGroup(config.sell_rule)
  if (sellRule) form.sellRule = sellRule
  // 单边研究（持续满仓 / 持有到期末）会传 null，此时不能默默沿用组合页默认
  // 规则 —— 否则用户以为在复现研究基线，实际用的是默认卖出条件 + 8% 止损。
  const missing: string[] = []
  if (config.buy_rule === null) missing.push('买入')
  if (config.sell_rule === null) missing.push('卖出')
  // 策略库加载时由调用方给整句话：策略场景不该出现"研究未设条件"这种措辞。
  deps.message.value = notice ?? (missing.length
    ? `已载入研究的规则与排序建议；研究未设${missing.join('、')}条件，该侧已保留当前默认，请确认后再回测。`
    : '已载入买卖条件研究的规则与排序建议，请确认后开始组合回测。')
}

function applyHandoffRanking(valueId: string | null, order: 'asc' | 'desc'): string | null {
  if (!valueId) return null
  const available = deps.availableValues.value.find((value) => value.id === valueId && value.rankable)
  if (!available) return `排序指标 ${valueId} 在当前组合回测模式下不可用，已保留原排序。`
  form.rankingValue = valueId
  form.rankOrder = order
  return `排序已设为${available.display_name} · ${order === 'asc' ? '从小到大' : '从大到小'}`
}

function screenSignalDateText(signalDate: number | null): string {
  if (!signalDate || !Number.isInteger(signalDate)) return ''
  const date = String(signalDate).padStart(8, '0')
  return date.length === 8 ? `${date.slice(0, 4)}-${date.slice(4, 6)}-${date.slice(6, 8)}` : ''
}

async function applyStockPoolHandoff(handoff: Extract<PortfolioBacktestHandoff, { kind: 'stock-pool' }>): Promise<void> {
  const codes = [...new Set(handoff.codes.map((code) => code.trim()).filter(Boolean))]
  if (!codes.length) return

  let formulaNotice = ''
  const formulaText = handoff.formula_text?.trim() ?? ''
  const previousRankingValue = form.rankingValue
  const previousRankOrder = form.rankOrder
  if (formulaText && (form.mode !== 'custom' || form.formulaText.trim() !== formulaText)) {
    const previousBuyRule = form.buyRule
    const previousSellRule = form.sellRule
    form.mode = 'custom'
    form.formulaText = formulaText
    form.formulaParameters = { ...handoff.formula_parameters }
    await deps.parseCustomFormula()
    form.buyRule = previousBuyRule
    form.sellRule = previousSellRule
    if (!deps.customMetadata.value) formulaNotice = '自定义公式未能解析，请检查公式后再使用该排序指标。'
  }

  form.universe = 'codes'
  form.codesText = codes.join('\n')
  const rankingNotice = applyHandoffRanking(handoff.ranking_value, handoff.rank_order)
  if (
    !handoff.ranking_value
    || !deps.availableValues.value.some((value) => value.id === handoff.ranking_value && value.rankable)
  ) {
    form.rankingValue = previousRankingValue
    form.rankOrder = previousRankOrder
  }
  deps.errors.value = { ...deps.errors.value }
  delete deps.errors.value.rankingValue
  const dateText = screenSignalDateText(handoff.signal_date)
  const details = [
    `${handoff.source_label}已作为 ${codes.length} 只标的的固定候选池带入`,
    rankingNotice,
    dateText ? `选股信号日期 ${dateText}` : '',
    formulaNotice,
  ].filter(Boolean)
  deps.message.value = `${details.join('；')}。当前买卖规则、持仓数、回测区间和退出风控保持本页设置。固定代码池不会重建历史逐日选股结果；回测起始日早于选股日期时，请注意未来信息偏差。`
}

async function applyPortfolioBacktestHandoff(handoff: PortfolioBacktestHandoff): Promise<void> {
  if (handoff.kind === 'stock-pool') {
    await applyStockPoolHandoff(handoff)
    return
  }

  if (handoff.kind === 'rules') {
    const applied: string[] = []
    const unchanged: string[] = []
    const buyRule = parseStoredRuleGroup(handoff.buy_rule)
    const sellRule = parseStoredRuleGroup(handoff.sell_rule)
    if (buyRule) {
      form.buyRule = buyRule
      applied.push('买入规则')
    } else unchanged.push('波段研究没有买入结论，当前买入规则保留')
    if (sellRule) {
      form.sellRule = sellRule
      applied.push('卖出规则')
    } else unchanged.push('波段研究没有卖出结论，当前卖出规则保留')
    const rankingNotice = applyHandoffRanking(handoff.ranking_value, handoff.rank_order)
    if (rankingNotice) applied.push(rankingNotice)
    deps.errors.value = {}
    const unchangedNotice = unchanged.length ? `${unchanged.join('；')}。` : ''
    deps.message.value = `${handoff.source_label}已带入${applied.length ? `：${applied.join('、')}` : '，没有可应用的规则或排序'}。${unchangedNotice}回测区间、标的范围、持仓数、风控和交易成本保持本页设置，请确认后运行。`
    return
  }

  const rankingNotice = applyHandoffRanking(handoff.value_id, handoff.rank_order)
  deps.message.value = rankingNotice
    ? `${handoff.source_label}：${rankingNotice}。${handoff.evidence}；只修改排序，其他回测设置保持不变，请确认后运行。`
    : `${handoff.source_label}的指标 ${handoff.value_id} 当前不可作为组合排序，已保留原排序。${handoff.evidence}`
}

/** 从 sessionStorage 的原始 JSON 里恢复发现候选标注；字段缺失视为没有。 */
function readAppliedDiscoveryMeta(raw: unknown): AppliedDiscoveryCandidate | null {
  if (!raw || typeof raw !== 'object') return null
  const value = raw as Record<string, unknown>
  if (typeof value.candidate_hash !== 'string' || !value.candidate_hash) return null
  const metrics = value.validation_metrics
  return {
    candidate_hash: value.candidate_hash,
    data_fingerprint: typeof value.data_fingerprint === 'string' ? value.data_fingerprint : null,
    validation_metrics: metrics && typeof metrics === 'object'
      ? metrics as Record<string, number | string | boolean | null>
      : null,
  }
}

const TRADE_FINGERPRINT_FIELDS = [
  'board', 'code', 'commission', 'cooldown_days', 'cost_basis', 'date', 'direction', 'instrument_type', 'market', 'pnl', 'price',
  'reason', 'risk_trigger', 'signal_date', 'size', 'slippage', 'stamp_tax',
] as const

async function tradeFingerprint(trades: PortfolioTrade[]): Promise<string | null> {
  if (!globalThis.crypto?.subtle) return null
  const rows = trades.map((trade) => {
    const source = trade as unknown as Record<string, unknown>
    const row: Record<string, string | null> = {}
    for (const field of TRADE_FINGERPRINT_FIELDS) {
      const value = source[field]
      row[field] = value === null || value === undefined
        ? null
        : typeof value === 'number'
          ? Number.isFinite(value) ? String(value) : null
          : String(value)
    }
    return row
  })
  const bytes = new TextEncoder().encode(JSON.stringify(rows))
  const digest = await globalThis.crypto.subtle.digest('SHA-256', bytes)
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('')
}

/**
 * 应用候选后重跑回测，验证「榜单上的候选是否真的复现」：
 * ① 关键指标与策略发现时的验证段指标是否一致（1e-9 容差）；
 * ② 逐笔成交的 SHA-256 指纹是否一致（Web Crypto 不可用时跳过）；
 * ③ 数据指纹是否一致。
 */
async function compareAppliedCandidate(backtest: PortfolioBacktestResult): Promise<void> {
  const applied = deps.appliedDiscoveryCandidate.value
  if (!applied) return
  const expected = applied.validation_metrics
  const metricKeys = ['total_return', 'annual_return', 'sharpe', 'max_drawdown', 'total_trades', 'win_rate', 'turnover']
  const comparable = metricKeys.filter((key) => typeof expected?.[key] === 'number' && typeof backtest.performance[key] === 'number')
  const performanceMatches = comparable.length > 0 && comparable.every((key) => {
    const expectedValue = expected?.[key]
    const actualValue = backtest.performance[key]
    return typeof expectedValue === 'number' && typeof actualValue === 'number'
      && Math.abs(expectedValue - actualValue) <= 1e-9
  })
  let actualTradeFingerprint: string | null = null
  try {
    actualTradeFingerprint = await tradeFingerprint(backtest.trades)
  } catch {
    // Keep the performance and data checks visible when Web Crypto is unavailable.
  }
  deps.replayComparison.value = {
    candidateHash: applied.candidate_hash,
    tradeRecords: applied.validation_metrics?.trade_fingerprint && actualTradeFingerprint
      ? applied.validation_metrics.trade_fingerprint === actualTradeFingerprint ? '一致' : '不一致'
      : '未提供',
    performance: performanceMatches ? '一致' : comparable.length ? '不一致' : '未提供',
    dataFingerprint: applied.data_fingerprint && backtest.data_fingerprint
      ? applied.data_fingerprint === backtest.data_fingerprint ? '一致' : '不一致'
      : '未提供',
  }
}

function applyBuyRuleToFormulaScreen(): void {
  const rule = form.buyRule
  const requiredConditions = rule.required_conditions ?? []
  const hasSignals = rule.signals.length > 0
  const hasConditions = rule.conditions.length > 0 || requiredConditions.length > 0
  if (!hasSignals && !hasConditions) {
    deps.message.value = '当前组合配置没有买入筛选条件可带入公式选股。'
    return
  }
  if (form.universe === 'codes') {
    deps.message.value = '公式选股暂不支持带入组合回测的指定代码列表；请先改用市场范围或自定义列表文件。'
    return
  }
  if (hasSignals && hasConditions) {
    deps.message.value = '当前买入规则同时包含快捷信号和指标条件，公式选股暂时不能原样表达；请简化为其中一种后再带入。'
    return
  }
  if (requiredConditions.length > 0 && rule.combine_mode !== 'all') {
    deps.message.value = '含有"必须满足"条件的买入规则只有在"全部满足"模式下才能原样带入公式选股。'
    return
  }
  if (form.universe === 'custom' && !form.universeFile.trim()) {
    deps.message.value = '自定义股票列表文件为空，无法带入公式选股。'
    return
  }
  if (form.mode === 'custom' && !deps.customMetadata.value) {
    deps.message.value = '请先解析自定义公式，再带入公式选股。'
    return
  }
  if (!deps.availableValues.value.some((value) => value.id === form.rankingValue && value.rankable)) {
    deps.message.value = '当前排序指标不是可跨标的比较的 rankable 输出，不能带入公式选股。'
    return
  }

  sessionStorage.setItem(FORMULA_SCREEN_HANDOFF_KEY, JSON.stringify({
    source_label: '组合回测',
    signals: [...rule.signals],
    conditions: [...rule.conditions],
    required_conditions: [...requiredConditions],
    combine_mode: rule.combine_mode,
    minimum_matches: rule.minimum_matches,
    universe: form.universe,
    universe_file: form.universe === 'custom' ? form.universeFile.trim() : null,
    boards: [...form.boards],
    instrument_types: [...form.instrumentTypes],
    mode: form.mode,
    formula_text: form.mode === 'custom' ? form.formulaText.trim() : null,
    formula_parameters: form.mode === 'custom' ? { ...form.formulaParameters } : {},
    ranking_value: form.rankingValue,
    rank_order: form.rankOrder,
  }))
  if (import.meta.env.MODE !== 'test') window.location.assign('/aquest')
}

return {
  applyStoredCandidate,
  applyStoredResearch,
  applyPortfolioBacktestHandoff,
  readAppliedDiscoveryMeta,
  compareAppliedCandidate,
  applyBuyRuleToFormulaScreen,
}
}
