import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import TermHint from './TermHint.vue'

describe('TermHint', () => {
  it('shows the Chinese name inline and keeps the English name in the tooltip', () => {
    const wrapper = mount(TermHint, { props: { term: 'icir' } })

    const mark = wrapper.find('[data-testid="term-hint-icir"]')
    expect(mark.exists()).toBe(true)
    expect(mark.attributes('aria-label')).toContain('信息系数信息比率')
    const popup = wrapper.get('[role="tooltip"]')
    expect(popup.text()).toContain('信息系数信息比率')
    expect(popup.text()).toContain('IC Information Ratio')
    expect(popup.text()).not.toMatch(/^ICIR/)
  })

  it('renders nothing for an unknown term', () => {
    const wrapper = mount(TermHint, { props: { term: 'nope' } })

    expect(wrapper.find('.term-hint').exists()).toBe(false)
  })
})
