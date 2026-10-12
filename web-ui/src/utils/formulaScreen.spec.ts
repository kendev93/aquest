import { beforeEach, describe, expect, it, vi } from 'vitest'

import type {
  CustomFormulaMetadata,
  FormulaScreenMetadata,
  InstrumentBoard,
  InstrumentType,
  ScreenCondition,
  ScreenFormState,
  ScreenResult,
  ValueDefinition,
} from '../types'
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
  parseStoredRuleGroup,
  resultsToCsv,
  saveConditionSet,
  saveForm,
  signalDisplayName,
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

describe('formula screen defensive parsing and fallbacks', () => {
  const value = (id: string): ValueDefinition => ({ id, display_name: id.toUpperCase(), description: '' })
  const FORM_KEY = 'aquest.form.v2'
  const LEGACY_KEY = 'aquest.form.v1'
  const SETS_KEY = 'aquest.condition-sets.v1'

  beforeEach(() => {
    localStorage.clear()
    vi.restoreAllMocks()
  })

  it('ignores malformed persisted form payloads', () => {
    localStorage.setItem(FORM_KEY, '{not json')
    expect(loadSavedForm()).toEqual({})

    localStorage.setItem(FORM_KEY, 'null')
    expect(loadSavedForm()).toEqual({})

    localStorage.setItem(FORM_KEY, '"text"')
    expect(loadSavedForm()).toEqual({})

    localStorage.setItem(FORM_KEY, JSON.stringify({
      mode: 'broken',
      ruleMode: 'broken',
      universe: 'broken',
      instrumentTypes: ['stock', 'crypto'],
      boards: ['main', 'crypto', 'b_share'],
      workers: 99,
      conditions: 'not-an-array',
    }))
    const saved = loadSavedForm()
    expect(saved.mode).toBeUndefined()
    expect(saved.ruleMode).toBeUndefined()
    expect(saved.universe).toBeUndefined()
    expect(saved.instrumentTypes).toEqual(['stock'])
    expect(saved.boards).toEqual(['main'])
    expect(saved.workers).toBe(8)
    expect(saved.conditions).toBeUndefined()
  })

  it('clamps worker counts and falls back to the legacy storage key', () => {
    localStorage.setItem(LEGACY_KEY, JSON.stringify({ workers: 0, universe: 'sz', mode: 'custom' }))
    expect(loadSavedForm()).toMatchObject({ workers: 1, universe: 'sz', mode: 'custom' })
  })

  it('drops conditions that fail validation and keeps the valid ones', () => {
    localStorage.setItem(FORM_KEY, JSON.stringify({
      conditions: [
        null,
        42,
        { left_value: 5, operator: 'gt', right_value: null, right_constant: 1 },
        { left_value: 'a.b', operator: 'nope', right_value: null, right_constant: 1 },
        { left_value: 'a.b', operator: 'gt', right_value: null, right_constant: 'x' },
        { left_value: 'a.b', operator: 'gt', right_value: 'c.d', right_constant: 1 },
        { left_value: 'a.b', operator: 'gt', right_value: null, right_constant: null },
        { left_value: ' a.b ', operator: 'gt', right_value: null, right_constant: 1.5 },
      ],
    }))
    expect(loadSavedForm().conditions).toEqual([
      { left_value: 'a.b', operator: 'gt', right_value: null, right_constant: 1.5 },
    ])
  })

  it('falls back to a signal rule when the numeric mapping is unavailable', () => {
    expect(numericRuleForSignal('indicator_two.start', [value('indicator_two.short_line')])).toEqual({
      signals: ['indicator_two.start'],
      conditions: [],
      combine_mode: 'all',
      minimum_matches: null,
    })
    expect(numericRuleForSignal('indicator_three.var1', [value('indicator_three.var1')])).toMatchObject({
      conditions: [{ left_value: 'indicator_three.var1', operator: 'gt', right_constant: 0 }],
    })
    expect(numericRuleForSignal('   ', [])).toEqual({
      signals: [],
      conditions: [],
      combine_mode: 'all',
      minimum_matches: null,
    })
  })

  it('reports custom formula problems', () => {
    const customForm = { ...baseForm(), mode: 'custom' as const }
    expect(validateScreenForm(customForm)).toMatchObject({ formulaText: '请输入通达信公式。' })
    expect(validateScreenForm({ ...customForm, formulaText: 'A:B;' })).toMatchObject({
      formulaText: '请先点击“解析公式”，确认参数和输出信号。',
    })

    const metadata: CustomFormulaMetadata = {
      parameters: [{ name: 'N', default: 5, minimum: 2, maximum: 10, step: 1 }],
      signals: [],
      minimum_bars: 1,
      warnings: [],
    }
    expect(validateScreenForm({ ...customForm, formulaText: 'A:B;', formulaParameters: { N: 99 } }, metadata))
      .toMatchObject({ formulaParameters: expect.stringContaining('必须在 2 到 10 之间') })
    expect(validateScreenForm({ ...customForm, formulaText: 'A:B;', formulaParameters: { N: 5.5 } }, metadata))
      .toMatchObject({ formulaParameters: expect.stringContaining('必须是整数') })
  })

  it('validates rule conditions and minimum matches', () => {
    const conditionsForm = {
      ...baseForm(),
      ruleMode: 'conditions' as const,
      selectedSignals: [],
      conditions: [] as ScreenCondition[],
    }
    expect(validateScreenForm(conditionsForm)).toMatchObject({ conditions: expect.any(String) })

    expect(validateScreenForm({
      ...conditionsForm,
      conditions: [{ left_value: '  ', operator: 'gt', right_value: null, right_constant: 1 }],
    })).toMatchObject({ 'condition-0': '请选择左侧指标。' })

    expect(validateScreenForm({
      ...conditionsForm,
      conditions: [{ left_value: 'a.b', operator: 'gt', right_value: 'c.d', right_constant: 1 }],
    })).toMatchObject({ 'condition-0': '每条条件必须设置一个右值。' })

    expect(validateScreenForm({ ...baseForm(), minimumMatches: null })).toMatchObject({
      minimumMatches: expect.any(String),
    })
  })

  it('normalises custom universe and formula fields in the payload', () => {
    const blank = buildScanPayload({
      ...baseForm(),
      universe: 'custom' as const,
      universeFile: '   ',
      mode: 'custom' as const,
      formulaText: '   ',
      instrumentTypes: ['stock' as InstrumentType],
      boards: ['main' as InstrumentBoard],
    })
    expect(blank.universe_file).toBeNull()
    expect(blank.formula_text).toBeNull()
    expect(blank.formula_parameters).toEqual({})
    expect(blank.instrument_types).toEqual(['stock'])
    expect(blank.boards).toEqual(['main'])

    const filled = buildScanPayload({
      ...baseForm(),
      universe: 'custom' as const,
      universeFile: ' /tmp/stocks.txt ',
      mode: 'custom' as const,
      formulaText: ' A:B; ',
      formulaParameters: { N: 5 },
    })
    expect(filled.universe_file).toBe('/tmp/stocks.txt')
    expect(filled.formula_text).toBe('A:B;')
    expect(filled.formula_parameters).toEqual({ N: 5 })
  })

  it('discards malformed persisted condition sets', () => {
    expect(loadSavedConditionSets()).toEqual([])

    localStorage.setItem(SETS_KEY, '{bad json')
    expect(loadSavedConditionSets()).toEqual([])

    localStorage.setItem(SETS_KEY, '{"not":"an array"}')
    expect(loadSavedConditionSets()).toEqual([])

    localStorage.setItem(SETS_KEY, JSON.stringify([
      null,
      'text',
      { id: 7, name: 'G', ruleMode: 'signals', mode: 'preset', combineMode: 'all', selectedSignals: [] },
      { id: '1', name: 'A', ruleMode: 'nope', mode: 'preset', combineMode: 'all', selectedSignals: [] },
      { id: '2', name: 'B', ruleMode: 'signals', mode: 'nope', combineMode: 'all', selectedSignals: [] },
      { id: '3', name: 'C', ruleMode: 'signals', mode: 'preset', combineMode: 'nope', selectedSignals: [] },
      { id: '4', name: 'D', ruleMode: 'signals', mode: 'preset', combineMode: 'all', selectedSignals: 'nope' },
      { id: '5', name: 'E', ruleMode: 'signals', mode: 'preset', combineMode: 'all', selectedSignals: ['x', 7] },
      {
        id: '6',
        name: 'F',
        ruleMode: 'conditions',
        mode: 'custom',
        combineMode: 'at_least',
        selectedSignals: [],
        conditions: [],
        minimumMatches: 3,
        formulaParameters: { keep: 1, drop: 'x', nan: Number.NaN },
      },
    ]))
    expect(loadSavedConditionSets()).toEqual([{
      id: '6',
      name: 'F',
      ruleMode: 'conditions',
      selectedSignals: [],
      conditions: [],
      combineMode: 'at_least',
      minimumMatches: 3,
      mode: 'custom',
      formulaText: '',
      formulaParameters: { keep: 1 },
    }])
  })

  it('keeps working when localStorage writes are rejected', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('QuotaExceededError')
    })
    const saved = createSavedConditionSet('集合', baseForm())

    expect(() => saveForm(baseForm())).not.toThrow()
    expect(saveConditionSet(saved)).toEqual([saved])
    expect(deleteConditionSet(saved.id)).toEqual([])
  })

  it('falls back to a generated id when crypto.randomUUID is unavailable', () => {
    vi.stubGlobal('crypto', {})
    try {
      expect(createSavedConditionSet('集合', baseForm()).id).toMatch(/^condition-set-\d+-/)
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('resolves signal display names across custom and built-in metadata', () => {
    const custom: CustomFormulaMetadata = {
      parameters: [],
      signals: [{ id: 'custom.sig', display_name: '自定义信号', description: '' }],
      minimum_bars: 1,
      warnings: [],
    }
    const builtIn: FormulaScreenMetadata = {
      indicators: [{
        id: 'ind',
        display_name: '指标',
        minimum_bars: 1,
        recommended_bars: 20,
        signals: [{ id: 'builtin.sig', display_name: '内置信号', description: '' }],
        values: [],
      }],
      combine_modes: [{ value: 'all', label: 'AND' }],
      supported_markets: ['sh'],
      supported_universe: [{ value: 'all', label: '全部' }],
      periods: [{ value: 'daily', label: '日线' }],
      data_directory_help: '',
    }

    expect(signalDisplayName('custom.sig', builtIn, custom)).toBe('自定义信号')
    expect(signalDisplayName('builtin.sig', builtIn, custom)).toBe('内置信号')
    expect(signalDisplayName('gone.sig', builtIn, custom)).toBe('gone.sig')
    expect(signalDisplayName(null, builtIn, custom)).toBe('组合规则')
  })

  it('escapes csv fields and tolerates missing names', () => {
    const results: ScreenResult[] = [
      {
        market: 'sh',
        code: '600000',
        name: null,
        instrument_type: 'stock',
        board: 'main',
        signal_date: 20240101,
        last_close: 10,
        matched_signals: [],
        match_count: 0,
        indicator_values: {},
      },
      {
        market: 'sz',
        code: '000001',
        name: '平安"银行"',
        signal_date: 20240102,
        last_close: 11,
        matched_signals: ['a'],
        match_count: 1,
        indicator_values: { ma: 1 },
      },
    ]

    const [header, first, second] = resultsToCsv(results).split('\n')
    expect(header).toContain('"name"')
    expect(first).toContain('""')
    expect(second).toContain('"平安""银行"""')
  })
  it('rebuilds a rule group saved in a history record', () => {
    const restored = parseStoredRuleGroup({
      signals: [],
      conditions: [
        { left_value: 'technical.rsi14', operator: 'gte', right_value: null, right_constant: 55 },
      ],
      required_conditions: [],
      combine_mode: 'all',
      minimum_matches: null,
    })
    expect(restored).toEqual({
      signals: [],
      conditions: [
        { left_value: 'technical.rsi14', operator: 'gte', right_value: null, right_constant: 55 },
      ],
      required_conditions: [],
      combine_mode: 'all',
      minimum_matches: null,
    })
  })

  it('drops unusable conditions and normalises at_least thresholds', () => {
    // 左右操作数同时缺失或同时存在都无法在编辑器里表达，必须整条丢弃。
    const restored = parseStoredRuleGroup({
      conditions: [
        // 右操作数缺失 / 两侧都有值 / 运算符非法，三种都无法在编辑器里表达。
        { left_value: 'technical.rsi14', operator: 'gt', right_value: null, right_constant: null },
        { left_value: 'technical.rsi14', operator: 'gt', right_value: 'technical.ma20', right_constant: 1 },
        { left_value: 'technical.rsi14', operator: 'nope', right_value: null, right_constant: 1 },
        { left_value: 'technical.rsi14', operator: 'lt', right_value: null, right_constant: 30 },
        { left_value: 'technical.rsi14', operator: 'gte', right_value: null, right_constant: 55 },
      ],
      combine_mode: 'at_least',
      minimum_matches: 9,
    })
    expect(restored?.conditions).toEqual([
      { left_value: 'technical.rsi14', operator: 'lt', right_value: null, right_constant: 30 },
      { left_value: 'technical.rsi14', operator: 'gte', right_value: null, right_constant: 55 },
    ])
    // 阈值被夹到实际条目数，避免出现「至少满足 9 / 2 条」。
    expect(restored?.minimum_matches).toBe(2)
  })

  it('returns null when nothing usable is stored', () => {
    expect(parseStoredRuleGroup(null)).toBeNull()
    expect(parseStoredRuleGroup('rule')).toBeNull()
    expect(parseStoredRuleGroup({ conditions: [] })).toBeNull()
    expect(parseStoredRuleGroup({ conditions: [{ left_value: 'x' }] })).toBeNull()
  })

  it('forces the plain combine mode when only one item survives', () => {
    const restored = parseStoredRuleGroup({
      signals: ['indicator_one.buy'],
      conditions: [],
      combine_mode: 'any',
      minimum_matches: 3,
    })
    expect(restored?.combine_mode).toBe('all')
    expect(restored?.minimum_matches).toBeNull()
  })
})
