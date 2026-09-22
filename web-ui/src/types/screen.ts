/** screen 相关的前端类型。 */

import type { CombineMode, ConditionOperator, ConditionOperatorDefinition, FormulaMode, IndicatorDefinition, InstrumentBoard, InstrumentType, RuleMode, Universe, ValueDefinition } from './common'


export interface FormulaScreenMetadata {
  indicators: IndicatorDefinition[]
  combine_modes: { value: CombineMode; label: string }[]
  condition_operators?: ConditionOperatorDefinition[]
  supported_markets: string[]
  supported_universe: { value: Universe; label: string }[]
  periods: { value: 'daily'; label: string }[]
  data_directory_help: string
  default_vipdoc_path?: string
}


export interface CustomParameterDefinition {
  name: string
  default: number
  minimum: number
  maximum: number
  step: number
}


export interface CustomSignalDefinition {
  id: string
  display_name: string
  description: string
}


export interface CustomFormulaMetadata {
  parameters: CustomParameterDefinition[]
  signals: CustomSignalDefinition[]
  values?: ValueDefinition[]
  minimum_bars: number
  warnings: string[]
}


export interface ScreenFormState {
  mode: FormulaMode
  ruleMode: RuleMode
  selectedSignals: string[]
  conditions: ScreenCondition[]
  combineMode: CombineMode
  minimumMatches: number | null
  universe: Universe
  universeFile: string
  instrumentTypes: InstrumentType[]
  boards: InstrumentBoard[]
  workers: number
  period: 'daily'
  formulaText: string
  formulaParameters: Record<string, number>
}


export interface ScreenCondition {
  left_value: string
  operator: ConditionOperator
  right_value: string | null
  right_constant: number | null
}


export interface RuleGroupPayload {
  signals: string[]
  conditions: ScreenCondition[]
  required_conditions?: ScreenCondition[]
  combine_mode: CombineMode
  minimum_matches: number | null
}


export interface SavedConditionSet {
  id: string
  name: string
  ruleMode: RuleMode
  selectedSignals: string[]
  conditions: ScreenCondition[]
  combineMode: CombineMode
  minimumMatches: number | null
  mode: FormulaMode
  formulaText: string
  formulaParameters: Record<string, number>
}


/** 筛选方案存的内容：整套可复现的输入，不含任何结果。字段用 snake_case，与接口一致。 */
export interface ScreenPresetPayload {
  mode: FormulaMode
  rule_mode: RuleMode
  selected_signals: string[]
  conditions: ScreenCondition[]
  combine_mode: CombineMode
  minimum_matches: number | null
  universe: Universe
  universe_file: string
  instrument_types: InstrumentType[]
  boards: InstrumentBoard[]
  workers: number
  period: 'daily'
  formula_text: string
  formula_parameters: Record<string, number>
}


export interface ScreenPresetSummary {
  preset_id: string
  name: string
  note: string
  created_at: string
  updated_at: string
}


export interface ScreenPreset extends ScreenPresetSummary {
  payload: ScreenPresetPayload
  /** 服务端裁剪时改动了什么。只有写操作会带。 */
  notes?: string[]
}


export interface ScanPayload {
  selected_signals: string[]
  conditions?: ScreenCondition[]
  combine_mode: CombineMode
  minimum_matches: number | null
  universe: Universe
  universe_file: string | null
  workers: number
  period: 'daily'
  formula_text: string | null
  formula_parameters: Record<string, number>
  instrument_types?: InstrumentType[]
  boards?: InstrumentBoard[]
}


export interface ScreenResult {
  market: string
  code: string
  name?: string | null
  instrument_type?: InstrumentType
  board?: InstrumentBoard
  signal_date: number
  last_close: number
  matched_signals: string[]
  match_count: number
  indicator_values: Record<string, number | null>
}


export interface ResultsMeta {
  total_candidates: number
  total_scanned: number
  total_signals: number
  errors: number
  skipped: number
  failure_reasons: Record<string, number>
  skip_reasons: Record<string, number>
}


export interface ScreenHistorySummary {
  history_id: string
  created_at: string
  total_candidates: number
  total_scanned: number
  total_signals: number
  errors: number
  skipped: number
  result_count: number
}


export interface ScreenHistoryRecord {
  history_id: string
  created_at: string
  meta: ResultsMeta
  results: ScreenResult[]
}
