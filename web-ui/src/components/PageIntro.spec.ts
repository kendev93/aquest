import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import PageIntro from './PageIntro.vue'

const BASE = {
  eyebrow: 'FACTOR TEST / FORWARD RETURN',
  title: '因子检验',
  copy: '看指标能不能把股票按未来收益排个先后。',
  noteLabel: '它能回答什么',
  guideKey: 'factor-test',
}

describe('PageIntro', () => {
  it('props 版给出 eyebrow、h1、说明、右栏和页面功能说明', () => {
    const wrapper = mount(PageIntro, {
      props: BASE,
      slots: { default: '<strong>这个指标有没有预测力</strong><small>无条件全样本</small>' },
    })

    const intro = wrapper.get('.page-intro')
    expect(intro.get('.eyebrow').text()).toBe(BASE.eyebrow)
    expect(intro.get('h1').text()).toBe(BASE.title)
    expect(intro.get('.intro-copy').text()).toBe(BASE.copy)
    expect(intro.get('.note-label').text()).toBe(BASE.noteLabel)
    expect(intro.get('.intro-note').text()).toContain('这个指标有没有预测力')
    expect(wrapper.find('[data-testid="page-guide-factor-test"]').exists()).toBe(true)
  })

  it('没有说明文案时不留一个空 p', () => {
    const wrapper = mount(PageIntro, {
      props: { ...BASE, copy: undefined },
      slots: { default: '<small>x</small>' },
    })

    expect(wrapper.find('.intro-copy').exists()).toBe(false)
  })

  it('右栏整个缺席时不渲染 intro-note，个股研究那种两行标题就是这么写的', () => {
    const wrapper = mount(PageIntro, { props: { ...BASE, noteLabel: undefined } })

    expect(wrapper.find('.intro-note').exists()).toBe(false)
    expect(wrapper.find('.note-label').exists()).toBe(false)
  })

  it('插槽优先于 props：标题和说明里要嵌组件时用插槽', () => {
    const wrapper = mount(PageIntro, {
      props: { ...BASE, copy: '不该出现' },
      slots: {
        title: '候选库<span class="term-hint-mock">?</span>',
        copy: '研究挖出来的候选条件存在这里，<strong>发布必须由你确认</strong>。',
        default: '<small>x</small>',
      },
    })

    expect(wrapper.get('h1').text()).toBe('候选库?')
    expect(wrapper.get('h1').find('.term-hint-mock').exists()).toBe(true)
    expect(wrapper.get('.intro-copy').html()).toContain('<strong>发布必须由你确认</strong>')
    expect(wrapper.get('.intro-copy').text()).not.toContain('不该出现')
  })

  it('标签也能由插槽自己给（右栏按条件出现的页面），不会多出一个空标签', () => {
    const wrapper = mount(PageIntro, {
      props: { eyebrow: BASE.eyebrow, title: BASE.title, guideKey: BASE.guideKey },
      slots: { default: '<span class="note-label">当前决策目标</span><strong>年化收益</strong>' },
    })

    const note = wrapper.get('.intro-note')
    expect(note.findAll('.note-label')).toHaveLength(1)
    expect(note.text()).toBe('当前决策目标年化收益')
  })
})
