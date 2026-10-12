import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import RuleGroupEditor from './RuleGroupEditor.vue'
import type { RuleGroupPayload } from '../types'

const rule: RuleGroupPayload = {
  signals: ['indicator_three.prepare_rally'],
  conditions: [],
  combine_mode: 'all',
  minimum_matches: null,
}

describe('RuleGroupEditor', () => {
  it('edits screening-style numeric conditions only', async () => {
    const wrapper = mount(RuleGroupEditor, {
      props: {
        modelValue: rule,
        title: '买入条件组合',
        testid: 'rule',
        values: [
          { id: 'indicator_three.var1', display_name: 'VAR1', description: '' },
          { id: 'indicator_two.mid_term', display_name: '中期趋势', description: '' },
        ],
      },
    })

    expect(wrapper.find('[data-testid="rule-mode-signals"]').exists()).toBe(false)
    await wrapper.get('[data-testid="rule-add-condition"]').trigger('click')
    await wrapper.get('[data-testid="rule-combine-mode"]').setValue('any')
    await wrapper.get('#rule-right-type-0').setValue('constant')
    await wrapper.get('#rule-right-0').setValue(8)

    const updates = wrapper.emitted('update:modelValue') ?? []
    const latest = updates.at(-1)?.[0] as RuleGroupPayload
    expect(latest.combine_mode).toBe('any')
    expect(latest.signals).toEqual([])
    expect(latest.conditions[0]?.right_constant).toBe(8)
    expect(latest.conditions[0]?.right_value).toBeNull()
    expect(wrapper.text()).toContain('VAR1')
    expect(wrapper.text()).toContain('中期趋势')
    expect(wrapper.text()).toContain('这里只使用指标输出')
  })

  it('locks required conditions to AND and respects the condition limit', async () => {
    const wrapper = mount(RuleGroupEditor, {
      props: {
        modelValue: { signals: [], conditions: [], combine_mode: 'all', minimum_matches: null },
        fixed: true,
        maxConditions: 2,
        title: '必选买入条件',
        testid: 'required',
        values: [
          { id: 'technical.ma5', display_name: 'MA5', description: '' },
          { id: 'technical.ma20', display_name: 'MA20', description: '' },
        ],
      },
    })

    expect(wrapper.find('[data-testid="required-combine-mode"]').exists()).toBe(false)
    await wrapper.get('[data-testid="required-add-condition"]').trigger('click')
    await wrapper.get('[data-testid="required-add-condition"]').trigger('click')

    expect(wrapper.findAll('[data-testid^="required-condition-"]')).toHaveLength(2)
    expect(wrapper.get('[data-testid="required-add-condition"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('.fixed-rule-note').classes()).toContain('fixed-rule-note--compact')
    const latest = (wrapper.emitted('update:modelValue') ?? []).at(-1)?.[0] as RuleGroupPayload
    expect(latest.combine_mode).toBe('all')
    expect(latest.conditions).toHaveLength(2)
  })
})
