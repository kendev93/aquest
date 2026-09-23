import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it } from 'vitest'

import guides from '../data/pageGuides.json'
import UserManualPage from './UserManualPage.vue'

const PAGE_COUNT = guides.guides.length

function search(wrapper: ReturnType<typeof mount>, keyword: string) {
  return wrapper.get('[data-testid="manual-search"]').setValue(keyword)
}

describe('UserManualPage', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('目录列出全部页面，正文每个页面一节', () => {
    const wrapper = mount(UserManualPage)

    expect(wrapper.findAll('[data-testid="manual-toc"] a').length).toBe(PAGE_COUNT)
    expect(wrapper.findAll('[data-testid="manual-article"]').length).toBe(PAGE_COUNT)
    expect(wrapper.get('[data-testid="manual-count"]').text()).toContain(`共 ${PAGE_COUNT} 个页面`)
  })

  it('目录锚点和正文 id 一一对应（对不上就是点了没反应）', () => {
    const wrapper = mount(UserManualPage)

    const hrefs = wrapper.findAll('[data-testid="manual-toc"] a').map((link) => link.attributes('href'))
    const ids = wrapper.findAll('[data-testid="manual-article"]').map((node) => node.attributes('id'))
    expect(hrefs).toEqual(ids.map((id) => `#${id}`))
  })

  it('每一节都给了「打开这一页」的去处', () => {
    const wrapper = mount(UserManualPage)

    const hrefs = wrapper.findAll('.manual-open').map((link) => link.attributes('href'))
    expect(hrefs).toEqual(guides.guides.map((guide) => guide.path))
  })

  it('搜索把目录收窄到命中的页面，并高亮命中的字', async () => {
    const wrapper = mount(UserManualPage)

    await search(wrapper, '衰减比')

    const articles = wrapper.findAll('[data-testid="manual-article"]')
    expect(articles.length).toBe(1)
    expect(articles[0].text()).toContain('策略追踪')
    // 高亮必须真的落在查询词上，不然"搜到了"和"高亮了"会各自漂移。
    expect(wrapper.findAll('[data-testid="manual-hit"]').map((node) => node.text())).toContain('衰减比')
    expect(wrapper.get('[data-testid="manual-count"]').text()).toContain('匹配到')
  })

  it('标题命中的页面整节保留，不是只剩那几个字', async () => {
    const wrapper = mount(UserManualPage)

    await search(wrapper, '因子归因')

    const article = wrapper.get('[data-testid="manual-article"]')
    expect(article.text()).toContain('操作步骤')
    expect(article.text()).toContain('口径提醒')
  })

  it('搜不到时给空态而不是一片空白', async () => {
    const wrapper = mount(UserManualPage)

    await search(wrapper, '这个词肯定不存在xyz')

    expect(wrapper.findAll('[data-testid="manual-article"]').length).toBe(0)
    expect(wrapper.find('[data-testid="manual-empty"]').exists()).toBe(true)
  })

  it('清空之后目录恢复全部', async () => {
    const wrapper = mount(UserManualPage)

    await search(wrapper, '衰减比')
    expect(wrapper.findAll('[data-testid="manual-article"]').length).toBe(1)

    await wrapper.get('[data-testid="manual-search-clear"]').trigger('click')
    expect(wrapper.findAll('[data-testid="manual-article"]').length).toBe(PAGE_COUNT)
  })
})
