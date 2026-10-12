import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it } from 'vitest'

import PageGuide from './PageGuide.vue'

const KEY = 'factor-test'
const STORAGE_KEY = `aquest.guide-expanded.${KEY}`

describe('PageGuide', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('默认收起：摘要可见，正文不渲染', () => {
    const wrapper = mount(PageGuide, { props: { pageKey: KEY } })

    expect(wrapper.find(`[data-testid="page-guide-${KEY}"]`).exists()).toBe(true)
    expect(wrapper.find(`[data-testid="page-guide-body-${KEY}"]`).exists()).toBe(false)
    expect(wrapper.get(`[data-testid="page-guide-toggle-${KEY}"]`).attributes('aria-expanded')).toBe(
      'false',
    )
  })

  it('点开之后给出操作步骤、不能回答什么和口径提醒', async () => {
    const wrapper = mount(PageGuide, { props: { pageKey: KEY } })

    await wrapper.get(`[data-testid="page-guide-toggle-${KEY}"]`).trigger('click')

    const body = wrapper.get(`[data-testid="page-guide-body-${KEY}"]`)
    expect(body.text()).toContain('操作步骤')
    expect(body.text()).toContain('它不能回答什么')
    expect(body.text()).toContain('口径提醒')
    expect(body.text()).toContain('下一步')
  })

  it('展开状态写进 localStorage，重新挂载时还开着', async () => {
    const first = mount(PageGuide, { props: { pageKey: KEY } })
    await first.get(`[data-testid="page-guide-toggle-${KEY}"]`).trigger('click')
    expect(localStorage.getItem(STORAGE_KEY)).toBe('1')

    const second = mount(PageGuide, { props: { pageKey: KEY } })
    expect(second.find(`[data-testid="page-guide-body-${KEY}"]`).exists()).toBe(true)
  })

  it('收起会改回 localStorage，不会一直赖着', async () => {
    localStorage.setItem(STORAGE_KEY, '1')
    const wrapper = mount(PageGuide, { props: { pageKey: KEY } })

    await wrapper.get(`[data-testid="page-guide-toggle-${KEY}"]`).trigger('click')

    expect(localStorage.getItem(STORAGE_KEY)).toBe('0')
    expect(wrapper.find(`[data-testid="page-guide-body-${KEY}"]`).exists()).toBe(false)
  })

  it('每个页面的状态互不干扰', () => {
    localStorage.setItem(`aquest.guide-expanded.${KEY}`, '1')

    const other = mount(PageGuide, { props: { pageKey: 'factor-attribution' } })

    expect(other.find('[data-testid="page-guide-body-factor-attribution"]').exists()).toBe(false)
  })

  it('给了一个不存在的 key 就什么都不渲染，而不是报错', () => {
    const wrapper = mount(PageGuide, { props: { pageKey: 'no-such-page' } })

    expect(wrapper.find('[data-testid^="page-guide-"]').exists()).toBe(false)
  })

  it('下一步链接指向真实路由', async () => {
    const wrapper = mount(PageGuide, { props: { pageKey: KEY } })
    await wrapper.get(`[data-testid="page-guide-toggle-${KEY}"]`).trigger('click')

    const links = wrapper.findAll('.guide-next-link')
    expect(links.length).toBeGreaterThan(0)
    for (const link of links) {
      expect(link.attributes('href')).toMatch(/^\//)
    }
  })
})