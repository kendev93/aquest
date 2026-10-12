import { describe, expect, it } from 'vitest'

import {
  buildPresetPayload,
  conditionSetToPayload,
  presetLoadMessage,
  presetToFormState,
  researchToFormState,
} from './screenPresets'
import type { SavedConditionSet, ScreenFormState, ScreenPresetPayload } from '../types'

const form: ScreenFormState = {
  mode: 'preset',
  ruleMode: 'conditions',
  selectedSignals: ['indicator_three.prepare_rally'],
  conditions: [
    { left_value: 'indicator_three.var1', operator: 'lt', right_value: null, right_constant: 5 },
  ],
  combineMode: 'at_least',
  minimumMatches: 1,
  universe: 'sh',
  universeFile: '',
  instrumentTypes: ['stock'],
  boards: ['main'],
  workers: 4,
  period: 'daily',
  formulaText: 'N:=5;',
  formulaParameters: { N: 5 },
}

const available = {
  valueIds: ['indicator_three.var1', 'indicator_three.var2'],
  signalIds: ['indicator_three.prepare_rally'],
}

describe('screenPresets', () => {
  it('round-trips a form through the payload', () => {
    const payload = buildPresetPayload(form)

    expect(payload.rule_mode).toBe('conditions')
    expect(payload.universe).toBe('sh')
    expect(payload.workers).toBe(4)

    const result = presetToFormState(payload, available)
    expect(result.state.ruleMode).toBe('conditions')
    expect(result.state.universe).toBe('sh')
    expect(result.state.workers).toBe(4)
    expect(result.state.conditions).toHaveLength(1)
    expect(result.staleConditions).toBe(0)
  })

  it('does not keep a reference to the live form', () => {
    const payload = buildPresetPayload(form)
    form.conditions.push({
      left_value: 'indicator_three.var2',
      operator: 'gt',
      right_value: null,
      right_constant: 1,
    })

    expect(payload.conditions).toHaveLength(1)
  })

  it('falls back to defaults for a broken payload', () => {
    const result = presetToFormState({ mode: 'nope', workers: 'many' }, available)

    expect(result.state.mode).toBe('preset')
    expect(result.state.ruleMode).toBe('signals')
    expect(result.state.combineMode).toBe('all')
    expect(result.state.universe).toBe('all')
    expect(result.state.workers).toBe(8)
    expect(result.state.conditions).toEqual([])
    expect(result.state.minimumMatches).toBeNull()
  })

  it('drops conditions that cannot be evaluated', () => {
    const payload: ScreenPresetPayload = {
      ...buildPresetPayload(form),
      conditions: [
        { left_value: 'a', operator: 'lt', right_value: null, right_constant: 1 },
        { left_value: 'a', operator: 'lt', right_value: null, right_constant: null },
        { left_value: 'a', operator: 'lt', right_value: 'b', right_constant: 1 },
        { left_value: 'a', operator: 'lt', right_value: 'indicator_three.var1', right_constant: null },
      ],
    }

    expect(presetToFormState(payload, available).state.conditions).toHaveLength(2)
  })

  it('counts conditions that reference missing indicators', () => {
    const payload: ScreenPresetPayload = {
      ...buildPresetPayload(form),
      conditions: [
        { left_value: 'indicator_three.gone', operator: 'lt', right_value: null, right_constant: 1 },
        { left_value: 'indicator_three.var1', operator: 'lt', right_value: null, right_constant: 1 },
      ],
    }

    const result = presetToFormState(payload, available)
    expect(result.staleConditions).toBe(1)
    expect(presetLoadMessage('旧方案', result)).toContain('1 条条件引用了当前不存在的指标')
  })

  it('does not cry stale before the metadata has arrived', () => {
    const payload: ScreenPresetPayload = {
      ...buildPresetPayload(form),
      conditions: [
        { left_value: 'indicator_three.gone', operator: 'lt', right_value: null, right_constant: 1 },
      ],
    }

    const result = presetToFormState(payload, { valueIds: [], signalIds: [] })
    expect(result.staleConditions).toBe(0)
    expect(result.staleSignals).toBe(0)
  })

  it('counts signals that no longer exist', () => {
    const payload: ScreenPresetPayload = {
      ...buildPresetPayload(form),
      selected_signals: ['indicator_three.prepare_rally', 'indicator_three.gone'],
    }

    const result = presetToFormState(payload, available)
    expect(result.staleSignals).toBe(1)
    expect(presetLoadMessage('旧方案', result)).toContain('1 个信号已不存在')
  })

  it('clears the minimum match count unless the mode is at_least', () => {
    const payload: ScreenPresetPayload = { ...buildPresetPayload(form), combine_mode: 'all' }

    expect(presetToFormState(payload, available).state.minimumMatches).toBeNull()
  })

  it('keeps only known instrument types and boards', () => {
    const payload = {
      ...buildPresetPayload(form),
      instrument_types: ['stock', 'etf'],
      boards: ['main', 'nowhere'],
    }

    const result = presetToFormState(payload, available)
    expect(result.state.instrumentTypes).toEqual(['stock'])
    expect(result.state.boards).toEqual(['main'])
  })

  it('always says a reload needs a fresh scan', () => {
    const result = presetToFormState(buildPresetPayload(form), available)

    expect(presetLoadMessage('甲', result)).toBe('已载入方案「甲」，结果需要重新扫描。')
  })

  it('turns a legacy condition set into a payload without guessing the universe', () => {
    const set: SavedConditionSet = {
      id: 'legacy-1',
      name: '旧组合',
      ruleMode: 'conditions',
      selectedSignals: ['indicator_three.prepare_rally'],
      conditions: form.conditions,
      combineMode: 'all',
      minimumMatches: null,
      mode: 'preset',
      formulaText: '',
      formulaParameters: {},
    }

    const payload = conditionSetToPayload(set)

    expect(payload.universe).toBe('all')
    expect(payload.instrument_types).toEqual([])
    expect(payload.boards).toEqual([])
    expect(payload.selected_signals).toEqual(['indicator_three.prepare_rally'])
  })

  it('maps a research buy rule onto the screening form', () => {
    const result = researchToFormState(
      {
        signals: ['indicator_three.prepare_rally'],
        conditions: [],
        combine_mode: 'all',
        minimum_matches: null,
        universe: 'sh',
        boards: ['main'],
        instrument_types: ['stock'],
      },
      available,
    )

    expect(result.state.ruleMode).toBe('signals')
    expect(result.state.selectedSignals).toEqual(['indicator_three.prepare_rally'])
    expect(result.state.universe).toBe('sh')
    expect(result.state.boards).toEqual(['main'])
  })

  it('merges required conditions and says so when the combine mode changes their meaning', () => {
    const required = {
      left_value: 'indicator_three.var1',
      operator: 'lt',
      right_value: null,
      right_constant: 3,
    }
    const loose = researchToFormState(
      { signals: [], conditions: [], required_conditions: [required], combine_mode: 'any' },
      available,
    )
    const strict = researchToFormState(
      { signals: [], conditions: [], required_conditions: [required], combine_mode: 'all' },
      available,
    )

    // 没有信号时可走的只有指标条件模式，否则扫描会直接报「至少选一个信号」。
    expect(loose.state.ruleMode).toBe('conditions')
    expect(loose.state.conditions).toHaveLength(1)
    // 「任一满足」下强制条件被并入计数，语义变了，必须讲出来。
    expect(loose.notes.join()).toContain('参与计数')
    expect(strict.notes.join()).toContain('等价')
    expect(loose.notes.join()).toContain('卖出条件没有带过来')
  })

  it('drops signals the metadata no longer offers', () => {
    const result = researchToFormState(
      { signals: ['indicator_three.prepare_rally', 'gone.signal'], combine_mode: 'all' },
      available,
    )

    expect(result.state.selectedSignals).toEqual(['indicator_three.prepare_rally'])
    expect(result.notes.join()).toContain('已移除')
  })

  it('clamps the minimum match count to what is actually selected', () => {
    const result = researchToFormState(
      {
        signals: ['indicator_three.prepare_rally'],
        combine_mode: 'at_least',
        minimum_matches: 9,
      },
      available,
    )

    // 只选中一个信号却要求「至少命中 9 条」，扫出来必然是空的，而且看不出为什么。
    expect(result.state.minimumMatches).toBe(1)
  })
})
