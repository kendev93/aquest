import type {
  CombineMode,
  ConditionOperator,
  FormulaMode,
  InstrumentBoard,
  InstrumentType,
  RuleMode,
  SavedConditionSet,
  ScreenCondition,
  ScreenFormState,
  ScreenPresetPayload,
  Universe,
} from '../types'

const MODES = new Set<FormulaMode>(['preset', 'custom'])
const RULE_MODES = new Set<RuleMode>(['signals', 'conditions'])
const COMBINE_MODES = new Set<CombineMode>(['all', 'any', 'at_least'])
const UNIVERSES = new Set<Universe>(['all', 'sh', 'sz', 'custom'])
const OPERATORS = new Set<ConditionOperator>([
  'gt', 'gte', 'lt', 'lte', 'eq', 'neq', 'cross_up', 'cross_down',
])
const BOARDS = new Set<InstrumentBoard>(['main', 'star', 'chinext'])
const INSTRUMENT_TYPES = new Set<InstrumentType>(['stock'])

export interface PresetAvailability {
  /** 当前元数据里可用的指标输出 id。方案里的条件对不上就算失效。 */
  valueIds: string[]
  signalIds: string[]
}

export interface PresetLoadResult {
  state: ScreenFormState
  /** 引用了当前不存在的指标的条件条数。这些条件会被替换掉，必须讲出来。 */
  staleConditions: number
  /** 引用了当前不存在的信号条数。 */
  staleSignals: number
}

function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value.filter((item): item is string => typeof item === 'string')
}

function parseCondition(value: unknown): ScreenCondition | null {
  if (!value || typeof value !== 'object') return null
  const record = value as Record<string, unknown>
  const left = typeof record.left_value === 'string' ? record.left_value.trim() : ''
  const operator = record.operator as ConditionOperator
  if (!left || !OPERATORS.has(operator)) return null
  const rightValue = typeof record.right_value === 'string' ? record.right_value.trim() : null
  const rawConstant = record.right_constant
  const rightConstant =
    typeof rawConstant === 'number' && Number.isFinite(rawConstant) ? rawConstant : null
  if ((rightValue === null) === (rightConstant === null)) return null
  return {
    left_value: left,
    operator,
    right_value: rightValue,
    right_constant: rightConstant,
  }
}

function parseConditions(value: unknown): ScreenCondition[] {
  if (!Array.isArray(value)) return []
  return value
    .map(parseCondition)
    .filter((condition): condition is ScreenCondition => condition !== null)
}

function numberMap(value: unknown): Record<string, number> {
  if (!value || typeof value !== 'object') return {}
  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>).filter(
      ([, item]) => typeof item === 'number' && Number.isFinite(item),
    ),
  ) as Record<string, number>
}

/** 表单 → 方案内容。只取输入，结果 / 任务状态一律不进方案。 */
export function buildPresetPayload(form: ScreenFormState): ScreenPresetPayload {
  return {
    mode: form.mode,
    rule_mode: form.ruleMode,
    selected_signals: [...form.selectedSignals],
    conditions: form.conditions.map((condition) => ({ ...condition })),
    combine_mode: form.combineMode,
    minimum_matches: form.minimumMatches,
    universe: form.universe,
    universe_file: form.universeFile,
    instrument_types: [...form.instrumentTypes],
    boards: [...form.boards],
    workers: form.workers,
    period: 'daily',
    formula_text: form.formulaText,
    formula_parameters: { ...form.formulaParameters },
    ...(form.rankingValue?.trim()
      ? {
          ranking_value: form.rankingValue.trim(),
          rank_order: form.rankingOrder === 'asc' ? 'asc' as const : 'desc' as const,
        }
      : {}),
  }
}

/**
 * 方案内容 → 表单。任何对不上的字段都退回默认值，并且把「引用了当前不存在的
 * 指标」的条件数算出来——页面会静默把它们换成别的指标，不讲出来用户会以为加载的
 * 还是原来那套条件。
 */
export function presetToFormState(
  payload: unknown,
  available: PresetAvailability,
): PresetLoadResult {
  const record = (payload && typeof payload === 'object' ? payload : {}) as Record<string, unknown>
  const mode: FormulaMode = MODES.has(record.mode as FormulaMode) ? (record.mode as FormulaMode) : 'preset'
  const ruleMode: RuleMode = RULE_MODES.has(record.rule_mode as RuleMode)
    ? (record.rule_mode as RuleMode)
    : 'signals'
  const combineMode: CombineMode = COMBINE_MODES.has(record.combine_mode as CombineMode)
    ? (record.combine_mode as CombineMode)
    : 'all'
  const universe: Universe = UNIVERSES.has(record.universe as Universe)
    ? (record.universe as Universe)
    : 'all'
  const conditions = parseConditions(record.conditions)
  const selectedSignals = stringList(record.selected_signals)
  const rawMinimum = record.minimum_matches
  const minimumMatches =
    typeof rawMinimum === 'number' && Number.isFinite(rawMinimum) ? Math.round(rawMinimum) : null
  const rawWorkers = record.workers
  const workers =
    typeof rawWorkers === 'number' && Number.isFinite(rawWorkers)
      ? Math.min(Math.max(Math.round(rawWorkers), 1), 8)
      : 8

  const knownValues = new Set(available.valueIds)
  const knownSignals = new Set(available.signalIds)
  // 元数据还没加载完时不能判失效：宁可什么都不说，也不要把整套条件报成失效。
  const canCheck = knownValues.size > 0
  const staleConditions = canCheck
    ? conditions.filter(
        (condition) =>
          !knownValues.has(condition.left_value)
          || (condition.right_value !== null && !knownValues.has(condition.right_value)),
      ).length
    : 0
  const staleSignals = knownSignals.size > 0
    ? selectedSignals.filter((signal) => !knownSignals.has(signal)).length
    : 0

  return {
    state: {
      mode,
      ruleMode,
      selectedSignals,
      conditions,
      combineMode,
      minimumMatches: combineMode === 'at_least' ? minimumMatches : null,
      universe,
      universeFile: typeof record.universe_file === 'string' ? record.universe_file : '',
      instrumentTypes: stringList(record.instrument_types).filter((item): item is InstrumentType =>
        INSTRUMENT_TYPES.has(item as InstrumentType),
      ),
      boards: stringList(record.boards).filter((item): item is InstrumentBoard =>
        BOARDS.has(item as InstrumentBoard),
      ),
      workers,
      period: 'daily',
      formulaText: typeof record.formula_text === 'string' ? record.formula_text : '',
      formulaParameters: numberMap(record.formula_parameters),
      ...(typeof record.ranking_value === 'string'
        ? { rankingValue: record.ranking_value }
        : {}),
      ...(record.rank_order === 'asc' || record.rank_order === 'desc'
        ? { rankingOrder: record.rank_order }
        : {}),
    },
    staleConditions,
    staleSignals,
  }
}

export interface ResearchHandoffResult {
  state: ScreenFormState
  /** 搬过来时丢掉或改掉的东西。必须讲，否则「已带入」读起来像是原样还原。 */
  notes: string[]
}

const COMBINE_LABELS: Record<CombineMode, string> = {
  all: '全部满足',
  any: '任一满足',
  at_least: '至少若干条',
}

/**
 * 买卖条件研究的买入规则 → 选股表单。
 *
 * 两边规则结构同构（信号 / 指标条件 / 组合方式 / 最少命中数），但语义不同：研究
 * 评的是「什么时候进、什么时候出」，选股只回答「某一天是否满足」。所以只搬买入
 * 侧，并且把差异讲出来——不做任何猜测。
 */
export function researchToFormState(
  payload: unknown,
  available: PresetAvailability,
): ResearchHandoffResult {
  const record = (payload && typeof payload === 'object' ? payload : {}) as Record<string, unknown>
  const mode: FormulaMode = MODES.has(record.mode as FormulaMode)
    ? (record.mode as FormulaMode)
    : 'preset'
  const combineMode: CombineMode = COMBINE_MODES.has(record.combine_mode as CombineMode)
    ? (record.combine_mode as CombineMode)
    : 'all'
  const universe: Universe = UNIVERSES.has(record.universe as Universe)
    ? (record.universe as Universe)
    : 'all'

  const knownSignals = new Set(available.signalIds)
  const knownValues = new Set(available.valueIds)
  const requestedRankingValue = typeof record.ranking_value === 'string'
    ? record.ranking_value.trim()
    : ''
  const rankingValue = requestedRankingValue
  const rankingOrder = record.rank_order === 'asc' ? 'asc' : 'desc'
  const signals = stringList(record.signals)
  const selectedSignals = knownSignals.size > 0
    ? signals.filter((signal) => knownSignals.has(signal))
    : signals
  const staleSignals = knownSignals.size > 0
    ? signals.length - selectedSignals.length
    : 0

  const baseConditions = parseConditions(record.conditions)
  const requiredConditions = parseConditions(record.required_conditions)
  const conditions = [...baseConditions, ...requiredConditions]
  const canCheckValues = knownValues.size > 0
  const staleConditions = canCheckValues
    ? conditions.filter(
        (condition) =>
          !knownValues.has(condition.left_value)
          || (condition.right_value !== null && !knownValues.has(condition.right_value)),
      ).length
    : 0

  // 没有信号可用时只能走指标条件模式：空着进去扫描会直接报「至少选一个信号」。
  const ruleMode: RuleMode = selectedSignals.length > 0 ? 'signals' : 'conditions'
  const rawMinimum = record.minimum_matches
  const parsedMinimum =
    typeof rawMinimum === 'number' && Number.isFinite(rawMinimum) ? Math.round(rawMinimum) : null
  let minimumMatches: number | null = combineMode === 'at_least' ? parsedMinimum : null
  if (minimumMatches !== null) {
    const ceiling = ruleMode === 'signals' ? selectedSignals.length : conditions.length
    if (ceiling > 0) minimumMatches = Math.min(Math.max(minimumMatches, 1), ceiling)
  }

  const notes: string[] = []
  if (requiredConditions.length > 0) {
    notes.push(
      combineMode === 'all'
        ? `${requiredConditions.length} 条「必须满足」的条件已并入普通条件——在「全部满足」下两者等价。`
        : `${requiredConditions.length} 条「必须满足」的条件已并入普通条件，但组合方式是「${COMBINE_LABELS[combineMode]}」：研究里它们无条件生效，这里变成参与计数，请核对后再扫描。`,
    )
  }
  if (staleSignals > 0) notes.push(`${staleSignals} 个信号当前不可用，已移除。`)
  if (staleConditions > 0) notes.push(`${staleConditions} 条条件引用了当前不存在的指标，请核对。`)
  notes.push('卖出条件没有带过来：选股只回答「某一天是否满足」，不涉及什么时候卖。')

  return {
    state: {
      mode,
      ruleMode,
      selectedSignals,
      conditions: conditions.length > 0 ? conditions : [],
      combineMode,
      minimumMatches,
      universe,
      universeFile: '',
      instrumentTypes: stringList(record.instrument_types).filter(
        (item): item is InstrumentType => INSTRUMENT_TYPES.has(item as InstrumentType),
      ),
      boards: stringList(record.boards).filter((item): item is InstrumentBoard =>
        BOARDS.has(item as InstrumentBoard),
      ),
      workers: 8,
      period: 'daily',
      formulaText: typeof record.formula_text === 'string' ? record.formula_text : '',
      formulaParameters: numberMap(record.formula_parameters),
      ...(requestedRankingValue ? { rankingValue } : {}),
      ...(record.rank_order === 'asc' || record.rank_order === 'desc'
        ? { rankingOrder }
        : {}),
    },
    notes,
  }
}

/** 加载后的提示。有失效项必须先说失效，否则「已加载」读起来像是原样还原。 */
export function presetLoadMessage(name: string, result: PresetLoadResult): string {
  const parts = [`已载入方案「${name}」，结果需要重新扫描。`]
  if (result.staleConditions > 0) {
    parts.push(
      `有 ${result.staleConditions} 条条件引用了当前不存在的指标，已被替换成可用指标——请核对后再扫描。`,
    )
  }
  if (result.staleSignals > 0) {
    parts.push(`有 ${result.staleSignals} 个信号已不存在，已从选择中移除。`)
  }
  return parts.join(' ')
}

/**
 * 旧「指标组合」（存在浏览器本地，只存规则部分）转成方案内容。
 * 标的池 / 板块 / 并发这些它没存过，按默认值留空——不猜。
 */
export function conditionSetToPayload(set: SavedConditionSet): ScreenPresetPayload {
  return {
    mode: set.mode,
    rule_mode: set.ruleMode,
    selected_signals: [...set.selectedSignals],
    conditions: set.conditions.map((condition) => ({ ...condition })),
    combine_mode: set.combineMode,
    minimum_matches: set.minimumMatches,
    universe: 'all',
    universe_file: '',
    instrument_types: [],
    boards: [],
    workers: 8,
    period: 'daily',
    formula_text: set.formulaText,
    formula_parameters: { ...set.formulaParameters },
  }
}
