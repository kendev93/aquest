import { describe, expect, it } from 'vitest'

import {
  presetLoadMessage,
  stalePresetReferences,
} from './conditionResearch'

const availability = {
  valueIds: ['indicator.score'],
  signalIds: ['indicator.buy'],
}

describe('stalePresetReferences', () => {
  it('counts conditions and signals that no longer exist', () => {
    const payload = {
      buy_rule: {
        signals: ['indicator.buy', 'indicator.gone'],
        conditions: [{ left_value: 'indicator.score', operator: 'gt', right_constant: 1 }],
        required_conditions: [
          { left_value: 'indicator.gone', operator: 'lt', right_constant: 0 },
        ],
      },
      sell_rule: null,
    }

    expect(stalePresetReferences(payload, availability)).toBe(2)
  })

  it('stays silent while metadata has not arrived yet', () => {
    const payload = {
      buy_rule: {
        signals: ['indicator.buy'],
        conditions: [{ left_value: 'indicator.score', operator: 'gt', right_constant: 1 }],
      },
    }

    // 没有依据的时候宁可什么都不说，也不要把整套条件报成失效。
    expect(stalePresetReferences(payload, { valueIds: [], signalIds: [] })).toBe(0)
  })

  it('treats a null sell rule as an explicit choice, not a broken reference', () => {
    const payload = { buy_rule: { signals: ['indicator.buy'] }, sell_rule: null }

    expect(stalePresetReferences(payload, availability)).toBe(0)
  })

  it('ignores anything that is not an object', () => {
    expect(stalePresetReferences(null, availability)).toBe(0)
    expect(stalePresetReferences('nope', availability)).toBe(0)
  })
})

describe('presetLoadMessage', () => {
  it('reports stale references ahead of the plain success message', () => {
    expect(presetLoadMessage('甲', 3)).toContain('3 项引用了当前不存在的指标')
    expect(presetLoadMessage('甲', 0)).toContain('请确认后重新运行')
  })
})
