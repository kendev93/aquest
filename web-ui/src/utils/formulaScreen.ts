import type {
  CombineMode,
  ConditionOperator,
  CustomFormulaMetadata,
  FormulaScreenMetadata,
  InstrumentBoard,
  InstrumentType,
  SavedConditionSet,
  ScreenCondition,
  ScanPayload,
  ScreenFormState,
  RuleMode,
  RuleGroupPayload,
  ScreenResult,
  ValueDefinition,
} from '../types'

export const DEFAULT_PRESET_SIGNALS = [
  'indicator_three.prepare_rally',
  'indicator_three.accumulation_zone',
]

const FORM_STORAGE_KEY = 'indicator-lab.form.v2'
const LEGACY_FORM_STORAGE_KEY = 'indicator-lab.form.v1'
const CONDITION_SETS_STORAGE_KEY = 'indicator-lab.condition-sets.v1'

const CONDITION_OPERATORS = new Set<ConditionOperator>([
  'gt', 'gte', 'lt', 'lte', 'eq', 'neq', 'cross_up', 'cross_down',
])

function parseCondition(value: unknown): ScreenCondition | null {
  if (!value || typeof value !== 'object') return null
  const record = value as Record<string, unknown>
  const rightValue = record.right_value
  const rightConstant = record.right_constant
  if (
    typeof record.left_value !== 'string'
    || !CONDITION_OPERATORS.has(record.operator as ConditionOperator)
    || (rightValue !== null && typeof rightValue !== 'string')
    || (rightConstant !== null && typeof rightConstant !== 'number')
  ) return null
  const hasValue = typeof rightValue === 'string' && rightValue.trim().length > 0
  const hasConstant = typeof rightConstant === 'number' && Number.isFinite(rightConstant)
  if (hasValue === hasConstant) return null
  return {
    left_value: record.left_value.trim(),
    operator: record.operator as ConditionOperator,
    right_value: hasValue ? rightValue as string : null,
    right_constant: hasConstant ? rightConstant as number : null,
  }
}

function parseConditions(value: unknown): ScreenCondition[] {
  if (!Array.isArray(value)) return []
  return value.map(parseCondition).filter((condition): condition is ScreenCondition => condition !== null)
}

function cloneCondition(condition: ScreenCondition): ScreenCondition {
  return { ...condition }
}

export function loadSavedForm(): Partial<ScreenFormState> {
  if (typeof window === 'undefined') return {}
  try {
    const raw = window.localStorage.getItem(FORM_STORAGE_KEY)
      ?? window.localStorage.getItem(LEGACY_FORM_STORAGE_KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw) as Record<string, unknown>
    if (!parsed || typeof parsed !== 'object') return {}
    const safe: Partial<ScreenFormState> = {}
    if (parsed.mode === 'preset' || parsed.mode === 'custom') safe.mode = parsed.mode
    if (parsed.ruleMode === 'signals' || parsed.ruleMode === 'conditions') safe.ruleMode = parsed.ruleMode
    if (Array.isArray(parsed.selectedSignals) && parsed.selectedSignals.every((value) => typeof value === 'string')) {
      safe.selectedSignals = parsed.selectedSignals as string[]
    }
    if (parsed.combineMode === 'all' || parsed.combineMode === 'any' || parsed.combineMode === 'at_least') {
      safe.combineMode = parsed.combineMode
    }
    if (typeof parsed.minimumMatches === 'number' && Number.isFinite(parsed.minimumMatches)) safe.minimumMatches = parsed.minimumMatches
    if (parsed.universe === 'all' || parsed.universe === 'sh' || parsed.universe === 'sz' || parsed.universe === 'custom') {
      safe.universe = parsed.universe
    }
    if (typeof parsed.universeFile === 'string') safe.universeFile = parsed.universeFile
    if (Array.isArray(parsed.instrumentTypes)) {
      const values = parsed.instrumentTypes.filter((value): value is InstrumentType => (
        value === 'stock' || value === 'fund' || value === 'index' || value === 'bond'
      ))
      safe.instrumentTypes = values
    }
    if (Array.isArray(parsed.boards)) {
      const values = parsed.boards.filter((value): value is InstrumentBoard => (
        value === 'main' || value === 'star' || value === 'chinext' || value === 'b_share'
        || value === 'fund' || value === 'index' || value === 'bond'
      ))
      safe.boards = values
    }
    if (typeof parsed.workers === 'number' && Number.isFinite(parsed.workers)) {
      safe.workers = parsed.workers === 2 ? 8 : Math.min(Math.max(Math.round(parsed.workers), 1), 8)
    }
    if (parsed.period === 'daily') safe.period = parsed.period
    if (typeof parsed.formulaText === 'string') safe.formulaText = parsed.formulaText
    if (Array.isArray(parsed.conditions)) safe.conditions = parseConditions(parsed.conditions)
    if (parsed.formulaParameters && typeof parsed.formulaParameters === 'object') {
      const parameters = Object.fromEntries(
        Object.entries(parsed.formulaParameters).filter(([, value]) => typeof value === 'number' && Number.isFinite(value)),
      ) as Record<string, number>
      safe.formulaParameters = parameters
    }
    return safe
  } catch {
    return {}
  }
}

export function saveForm(form: ScreenFormState): void {
  if (typeof window === 'undefined') return
  try {
    window.localStorage.setItem(FORM_STORAGE_KEY, JSON.stringify(form))
  } catch {
    // Private browsing and restricted storage should not block scanning.
  }
}

export function valueOptionLabel(value: ValueDefinition): string {
  return value.display_name
}

function numericRule(
  leftValue: string,
  operator: ConditionOperator,
  rightValue: string | null,
  rightConstant: number | null,
  values: ValueDefinition[],
): RuleGroupPayload {
  const available = new Set(values.map((value) => value.id))
  if (!available.has(leftValue) || (rightValue !== null && !available.has(rightValue))) {
    return { signals: [], conditions: [], combine_mode: 'all', minimum_matches: null }
  }
  return {
    signals: [],
    conditions: [{ left_value: leftValue, operator, right_value: rightValue, right_constant: rightConstant }],
    combine_mode: 'all',
    minimum_matches: null,
  }
}

/** Convert a legacy preset signal default into a numeric indicator rule. */
export function numericRuleForSignal(
  signalId: string,
  values: ValueDefinition[],
): RuleGroupPayload {
  const known: Record<string, [string, ConditionOperator, string | null, number | null]> = {
    'indicator_two.start': ['indicator_two.short_line', 'cross_up', 'indicator_two.mid_line', null],
    'indicator_two.end': ['indicator_two.short_line', 'gt', null, 90],
    'indicator_three.prepare_rally': ['indicator_three.var1', 'cross_up', null, 8],
    'indicator_three.suppress_main': ['indicator_three.var1', 'lte', null, 8],
    'indicator_three.accumulation_zone': ['indicator_three.varo7', 'lt', null, 10],
    'indicator_three.begin_zone': ['indicator_three.var1', 'lt', null, 10],
    'indicator_three.end_zone': ['indicator_three.var1', 'gt', null, 90],
  }
  const mapped = known[signalId]
  if (mapped) {
    const rule = numericRule(...mapped, values)
    if (rule.conditions.length) return rule
  }
  if (values.some((value) => value.id === signalId)) {
    return numericRule(signalId, 'gt', null, 0, values)
  }
  if (signalId.trim()) {
    return { signals: [signalId], conditions: [], combine_mode: 'all', minimum_matches: null }
  }
  return { signals: [], conditions: [], combine_mode: 'all', minimum_matches: null }
}

const COMBINE_MODES = new Set<CombineMode>(['all', 'any', 'at_least'])

/**
 * Rebuild a rule group saved inside a history record.
 *
 * Stored payloads come back as plain JSON, so every field has to be re-validated
 * before it reaches an editor: a rule with neither a right operand nor a
 * constant, or one carrying both, would render as a condition the user cannot
 * fix. ``null`` means "nothing usable here", which callers read as "keep the
 * current rule".
 */
export function parseStoredRuleGroup(value: unknown): RuleGroupPayload | null {
  if (!value || typeof value !== 'object') return null
  const record = value as Record<string, unknown>
  const conditions = parseConditions(record.conditions)
  const requiredConditions = parseConditions(record.required_conditions)
  const signals = Array.isArray(record.signals)
    ? record.signals.filter(
      (item): item is string => typeof item === 'string' && item.trim().length > 0,
    )
    : []
  if (!signals.length && !conditions.length && !requiredConditions.length) return null
  const generatedCount = signals.length + conditions.length
  const requested = COMBINE_MODES.has(record.combine_mode as CombineMode)
    ? record.combine_mode as CombineMode
    : 'all'
  // A single item has nothing to combine, so force the plainest mode back.
  const combineMode = generatedCount < 2 ? 'all' : requested
  const rawMinimum = record.minimum_matches
  const minimumMatches = combineMode === 'at_least'
    && typeof rawMinimum === 'number'
    && Number.isFinite(rawMinimum)
    ? Math.min(Math.max(Math.trunc(rawMinimum), 1), generatedCount)
    : null
  return {
    signals,
    conditions,
    required_conditions: requiredConditions,
    combine_mode: combineMode,
    minimum_matches: minimumMatches,
  }
}

export function validateScreenForm(
  form: ScreenFormState,
  customMetadata: CustomFormulaMetadata | null = null,
): Record<string, string> {
  const errors: Record<string, string> = {}
  if (form.mode === 'custom') {
    if (!form.formulaText.trim()) {
      errors.formulaText = '请输入通达信公式。'
    } else if (!customMetadata) {
      errors.formulaText = '请先点击“解析公式”，确认参数和输出信号。'
    }
    if (customMetadata) {
      for (const parameter of customMetadata.parameters) {
        const value = form.formulaParameters[parameter.name]
        if (!Number.isFinite(value) || value < parameter.minimum || value > parameter.maximum) {
          errors.formulaParameters = `参数 ${parameter.name} 必须在 ${parameter.minimum} 到 ${parameter.maximum} 之间。`
          break
        }
        if (parameter.step === 1 && !Number.isInteger(value)) {
          errors.formulaParameters = `参数 ${parameter.name} 必须是整数。`
          break
        }
      }
    }
  }
  const conditionCount = form.ruleMode === 'conditions'
    ? form.conditions.length
    : form.selectedSignals.length
  if (form.ruleMode === 'conditions') {
    if (form.conditions.length === 0) {
      errors.conditions = '至少设置一条指标条件。'
    }
    form.conditions.forEach((condition, index) => {
      if (!condition.left_value.trim()) errors[`condition-${index}`] = '请选择左侧指标。'
      const hasValue = Boolean(condition.right_value?.trim())
      const hasConstant = typeof condition.right_constant === 'number' && Number.isFinite(condition.right_constant)
      if (hasValue === hasConstant) errors[`condition-${index}`] = '每条条件必须设置一个右值。'
    })
  } else if (form.selectedSignals.length === 0) {
    errors.selectedSignals = '至少选择一个选股条件。'
  }
  if (form.combineMode === 'at_least') {
    if (form.minimumMatches === null || !Number.isInteger(form.minimumMatches)) {
      errors.minimumMatches = '“至少满足 N 个”模式需要填写整数 N。'
    } else if (form.minimumMatches < 1 || form.minimumMatches > conditionCount) {
      errors.minimumMatches = 'N 必须在 1 和已选择条件数量之间。'
    }
  }
  if (form.universe === 'custom' && !form.universeFile.trim()) {
    errors.universeFile = '自定义范围需要提供股票列表文件。'
  }
  return errors
}

export function buildScanPayload(form: ScreenFormState): ScanPayload {
  return {
    selected_signals: form.ruleMode === 'conditions' ? [] : [...form.selectedSignals],
    combine_mode: form.combineMode,
    minimum_matches: form.combineMode === 'at_least' ? form.minimumMatches : null,
    universe: form.universe,
    universe_file: form.universe === 'custom' ? form.universeFile.trim() || null : null,
    workers: form.workers,
    period: form.period,
    formula_text: form.mode === 'custom' ? form.formulaText.trim() || null : null,
    formula_parameters: form.mode === 'custom' ? { ...form.formulaParameters } : {},
    ...(form.ruleMode === 'conditions'
      ? { conditions: form.conditions.map(cloneCondition) }
      : {}),
    ...(form.instrumentTypes.length > 0 ? { instrument_types: [...form.instrumentTypes] } : {}),
    ...(form.boards.length > 0 ? { boards: [...form.boards] } : {}),
  }
}

export function loadSavedConditionSets(): SavedConditionSet[] {
  if (typeof window === 'undefined') return []
  try {
    const raw = window.localStorage.getItem(CONDITION_SETS_STORAGE_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw) as unknown
    if (!Array.isArray(parsed)) return []
    return parsed.flatMap((value): SavedConditionSet[] => {
      if (!value || typeof value !== 'object') return []
      const record = value as Record<string, unknown>
      const conditions = parseConditions(record.conditions)
      if (
        typeof record.id !== 'string'
        || typeof record.name !== 'string'
        || (record.ruleMode !== 'signals' && record.ruleMode !== 'conditions')
        || (record.mode !== 'preset' && record.mode !== 'custom')
        || (record.combineMode !== 'all' && record.combineMode !== 'any' && record.combineMode !== 'at_least')
        || !Array.isArray(record.selectedSignals)
        || !record.selectedSignals.every((signal) => typeof signal === 'string')
      ) return []
      return [{
        id: record.id,
        name: record.name,
        ruleMode: record.ruleMode as RuleMode,
        selectedSignals: [...record.selectedSignals] as string[],
        conditions,
        combineMode: record.combineMode as CombineMode,
        minimumMatches: typeof record.minimumMatches === 'number' ? record.minimumMatches : null,
        mode: record.mode,
        formulaText: typeof record.formulaText === 'string' ? record.formulaText : '',
        formulaParameters: record.formulaParameters && typeof record.formulaParameters === 'object'
          ? Object.fromEntries(
            Object.entries(record.formulaParameters).filter(([, item]) => (
              typeof item === 'number' && Number.isFinite(item)
            )),
          ) as Record<string, number>
          : {},
      }]
    })
  } catch {
    return []
  }
}

export function createSavedConditionSet(name: string, form: ScreenFormState): SavedConditionSet {
  const id = typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : `condition-set-${Date.now()}-${Math.random().toString(36).slice(2)}`
  return {
    id,
    name: name.trim(),
    ruleMode: form.ruleMode,
    selectedSignals: [...form.selectedSignals],
    conditions: form.conditions.map(cloneCondition),
    combineMode: form.combineMode,
    minimumMatches: form.minimumMatches,
    mode: form.mode,
    formulaText: form.formulaText,
    formulaParameters: { ...form.formulaParameters },
  }
}

export function saveConditionSet(conditionSet: SavedConditionSet): SavedConditionSet[] {
  const next = [
    conditionSet,
    ...loadSavedConditionSets().filter((item) => (
      item.id !== conditionSet.id && item.name !== conditionSet.name
    )),
  ]
  if (typeof window !== 'undefined') {
    try {
      window.localStorage.setItem(CONDITION_SETS_STORAGE_KEY, JSON.stringify(next))
    } catch {
      // Restricted storage should not block the current scan configuration.
    }
  }
  return next
}

export function deleteConditionSet(id: string): SavedConditionSet[] {
  const next = loadSavedConditionSets().filter((item) => item.id !== id)
  if (typeof window !== 'undefined') {
    try {
      window.localStorage.setItem(CONDITION_SETS_STORAGE_KEY, JSON.stringify(next))
    } catch {
      // Restricted storage should not block the current scan configuration.
    }
  }
  return next
}

export function filterKnownSignals(
  selectedSignals: readonly string[],
  availableSignalIds: readonly string[],
): string[] {
  const available = new Set(availableSignalIds)
  return selectedSignals.filter((signalId) => available.has(signalId))
}

export function signalDisplayName(
  signalId: string | null | undefined,
  metadata: FormulaScreenMetadata | null,
  customMetadata: CustomFormulaMetadata | null = null,
): string {
  const customSignal = customMetadata?.signals.find((candidate) => candidate.id === signalId)
  if (customSignal) return customSignal.display_name
  for (const indicator of metadata?.indicators ?? []) {
    const signal = indicator.signals.find((candidate) => candidate.id === signalId)
    if (signal) return signal.display_name
  }
  return signalId ?? '组合规则'
}

export function resultsToCsv(results: ScreenResult[]): string {
  const header = ['market', 'code', 'name', 'instrument_type', 'board', 'signal_date', 'last_close', 'matched_signals', 'match_count', 'indicator_values']
  const escape = (value: string): string => `"${value.replace(/"/g, '""')}"`
  const rows = results.map((result) => [
    result.market,
    result.code,
    result.name ?? '',
    result.instrument_type ?? '',
    result.board ?? '',
    String(result.signal_date),
    String(result.last_close),
    result.matched_signals.join(', '),
    String(result.match_count),
    JSON.stringify(result.indicator_values),
  ].map(escape).join(','))
  return [header.map(escape).join(','), ...rows].join('\n')
}

export function combineModeLabel(mode: CombineMode): string {
  return { all: '全部满足 AND', any: '任一满足 OR', at_least: '至少满足 N 个' }[mode]
}
