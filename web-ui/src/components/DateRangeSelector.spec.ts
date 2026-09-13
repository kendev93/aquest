import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import type { DateRangeSelection } from '../types'
import DateRangeSelector from './DateRangeSelector.vue'

const initial: DateRangeSelection = {
  mode: 'recent5',
  startDate: '',
  endDate: '',
}

describe('DateRangeSelector', () => {
  it('defaults to recent five years and exposes custom date fields on demand', async () => {
    const wrapper = mount(DateRangeSelector, {
      props: {
        modelValue: initial,
        prefix: 'backtest',
        testid: 'backtest-date-range',
      },
    })

    expect((wrapper.get('[data-testid="backtest-date-mode"]').element as HTMLSelectElement).value).toBe('recent5')
    expect(wrapper.find('[data-testid="backtest-start"]').exists()).toBe(false)

    await wrapper.get('[data-testid="backtest-date-mode"]').setValue('custom')
    await wrapper.setProps({ modelValue: { mode: 'custom', startDate: '', endDate: '' } })
    expect(wrapper.get('[data-testid="backtest-start"]').exists()).toBe(true)
    expect(wrapper.get('[data-testid="backtest-end"]').exists()).toBe(true)
    expect(wrapper.emitted('update:modelValue')?.at(-1)?.[0]).toEqual({
      mode: 'custom',
      startDate: '',
      endDate: '',
    })
  })

  it('clears custom dates when switching back to a preset', async () => {
    const wrapper = mount(DateRangeSelector, {
      props: {
        modelValue: { mode: 'custom', startDate: '2024-01-01', endDate: '2024-12-31' },
        prefix: 'portfolio',
        testid: 'portfolio-date-range',
      },
    })

    await wrapper.get('[data-testid="portfolio-date-mode"]').setValue('full')

    expect(wrapper.emitted('update:modelValue')?.at(-1)?.[0]).toEqual({
      mode: 'full',
      startDate: '',
      endDate: '',
    })
  })
})
