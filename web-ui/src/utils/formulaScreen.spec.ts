import { describe, expect, it } from 'vitest'

import type { InstrumentBoard, InstrumentType, ScreenCondition, ScreenFormState } from '../types'
import {
  DEFAULT_PRESET_SIGNALS,
  buildScanPayload,
  combineModeLabel,
  createSavedConditionSet,
  deleteConditionSet,
  filterKnownSignals,
  loadSavedConditionSets,
  loadSavedForm,
  numericRuleForSignal,
  resultsToCsv,
  saveConditionSet,
  saveForm,
  validateScreenForm,
} from './formulaScreen'

const baseForm = (): ScreenFormState => ({
  mode: 'preset',
  ruleMode: 'signals',
  selectedSignals: ['indicator_three.prepare_rally', 'indicator_three.accumulation_zone'],
  conditions: [],
  combineMode: 'at_least',
  minimumMatches: 2,
  universe: 'all',
  universeFile: '',
  instrumentTypes: [],
  boards: [],
  workers: 2,
  period: 'daily',
  formulaText: '',
  formulaParameters: {},
})

describe('formula screen form helpers', () => {
  it('provides useful preset defaults and remembers configuration locally', () => {
    expect(DEFAULT_PRESET_SIGNALS).toEqual([
      'indicator_three.prepare_rally',
      'indicator_three.accumulation_zone',
    ])
    saveForm(baseForm())

    expect(loadSavedForm()).toMatchObject({ mode: 'preset', workers: 8 })
  })

  it('rejects an empty selection and invalid minimum matches', () => {
    const form = { ...baseForm(), selectedSignals: [], minimumMatches: 2 }

    expect(validateScreenForm(form)).toMatchObject({ selectedSignals: expect.any(String), minimumMatches: expect.any(String) })
  })

  it('requires a custom file and builds exact backend payload', () => {
    const form = { ...baseForm(), universe: 'custom' as const, universeFile: ' /tmp/stocks.txt ' }

    expect(validateScreenForm(form)).toEqual({})
    expect(buildScanPayload(form)).toEqual({
      selected_signals: form.selectedSignals,
      combine_mode: 'at_least',
      minimum_matches: 2,
      universe: 'custom',
      universe_file: '/tmp/stocks.txt',
      workers: 2,
      period: 'daily',
      formula_text: null,
      formula_parameters: {},
    })
  })

  it('omits minimum for AND and exports escaped CSV values', () => {
    const form = { ...baseForm(), combineMode: 'all' as const, minimumMatches: null }
    expect(buildScanPayload(form).minimum_matches).toBeNull()
    expect(combineModeLabel('any')).toBe('任一满足 OR')
    const csv = resultsToCsv([{
      market: 'SH', code: '600000', name: '浦发银行', signal_date: 20260824, last_close: 12.3,
      matched_signals: ['indicator_one.main_force_entry'], match_count: 1,
      indicator_values: { 'indicator_one.var5': 1.2 },
    }])
    expect(csv).toContain('"matched_signals"')
    expect(csv).toContain('600000')
    expect(csv).toContain('浦发银行')
  })

  it('includes opt-in instrument and board filters while leaving empty filters unrestricted', () => {
    expect(buildScanPayload(baseForm())).not.toHaveProperty('instrument_types')
    expect(buildScanPayload(baseForm())).not.toHaveProperty('boards')

    const filtered = {
      ...baseForm(),
      instrumentTypes: ['stock'] as InstrumentType[],
      boards: ['main', 'chinext'] as InstrumentBoard[],
    }

    expect(buildScanPayload(filtered)).toMatchObject({
      instrument_types: ['stock'],
      boards: ['main', 'chinext'],
    })
  })

  it('builds numeric conditions and persists named condition sets locally', () => {
    const condition: ScreenCondition = {
      left_value: 'indicator_three.varo7',
      operator: 'lt',
      right_value: null,
      right_constant: 10,
    }
    const form = {
      ...baseForm(),
      ruleMode: 'conditions' as const,
      selectedSignals: [],
      conditions: [condition],
      minimumMatches: 1,
    }

    expect(buildScanPayload(form)).toMatchObject({
      selected_signals: [],
      conditions: [condition],
    })
    expect(validateScreenForm(form)).toEqual({})

    const saved = createSavedConditionSet('主力进场低位', form)
    saveConditionSet(saved)
    expect(loadSavedConditionSets()).toEqual([saved])

    deleteConditionSet(saved.id)
    expect(loadSavedConditionSets()).toEqual([])
  })

  it('filters persisted signal ids that are no longer in metadata', () => {
    expect(filterKnownSignals(
      ['removed.signal', 'indicator_three.prepare_rally', 'removed.again'],
      ['indicator_three.prepare_rally'],
    )).toEqual(['indicator_three.prepare_rally'])
  })

  it('converts preset buy and sell outputs into numeric conditions', () => {
    const values = [
      { id: 'indicator_three.var1', display_name: 'VAR1', description: '' },
      { id: 'indicator_three.varo7', display_name: 'VARO7', description: '' },
      { id: 'indicator_two.short_line', display_name: '短期线', description: '' },
      { id: 'indicator_two.mid_line', display_name: '中期线', description: '' },
    ]

    expect(numericRuleForSignal('indicator_three.prepare_rally', values)).toMatchObject({
      signals: [],
      conditions: [{ left_value: 'indicator_three.var1', operator: 'cross_up', right_constant: 8 }],
    })
    expect(numericRuleForSignal('indicator_two.start', values)).toMatchObject({
      conditions: [{ left_value: 'indicator_two.short_line', right_value: 'indicator_two.mid_line' }],
    })
  })
})
