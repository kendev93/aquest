import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import StrategyPoolFollowButton from './StrategyPoolFollowButton.vue'
import type { StrategyPoolFollowTarget } from '../types'

const target: StrategyPoolFollowTarget = { market: 'SH', code: '600000', name: '测试600000' }

function mountButton(followKeys: Set<string>, extra: Record<string, unknown> = {}) {
  return mount(StrategyPoolFollowButton, { props: { target, followKeys, ...extra } })
}

describe('StrategyPoolFollowButton', () => {
  it('offers 关注 and says where the symbol will go', () => {
    const wrapper = mountButton(new Set<string>())
    const button = wrapper.get('button')

    expect(button.text()).toBe('关注')
    expect(button.attributes('title')).toBe('关注 SH 600000，加入特别关注面板')
    expect(button.attributes('data-testid')).toBe('pool-follow-600000')
  })

  it('switches to 取消关注 for a followed symbol', () => {
    const wrapper = mountButton(new Set(['SH600000']))

    expect(wrapper.get('button').text()).toBe('取消关注')
    expect(wrapper.get('button').attributes('title')).toBe('取消关注 SH 600000')
  })

  it('blocks a second click while the list is still saving, then emits this target', async () => {
    const busy = mountButton(new Set<string>(), { busy: true, testId: 'pool-buy-follow-600000' })
    expect(busy.get('button').attributes('disabled')).toBeDefined()
    await busy.get('button').trigger('click')
    expect(busy.emitted('toggle')).toBeFalsy()

    const ready = mountButton(new Set<string>(), { testId: 'pool-buy-follow-600000' })
    expect(ready.get('button').attributes('data-testid')).toBe('pool-buy-follow-600000')
    await ready.get('button').trigger('click')
    expect(ready.emitted('toggle')?.[0]).toEqual([target])
  })
})
