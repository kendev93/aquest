import { describe, expect, it } from 'vitest'

import { highlightSegments, manualAnchor, parseQuery, searchManual } from './manual'

describe('manual 搜索', () => {
  it('查询串按空白切词并统一小写', () => {
    expect(parseQuery('  组合  回测 ')).toEqual(['组合', '回测'])
    expect(parseQuery('MinTRL')).toEqual(['mintrl'])
    expect(parseQuery('   ')).toEqual([])
  })

  it('命中区间切开成片段，其余原样保留', () => {
    expect(highlightSegments('按月度调仓', ['月度'])).toEqual([
      { text: '按', hit: false },
      { text: '月度', hit: true },
      { text: '调仓', hit: false },
    ])
  })

  it('多个词都要出现，命中区间重叠时合并', () => {
    expect(highlightSegments('ab cd', ['ab', 'cd'])).toEqual([
      { text: 'ab', hit: true },
      { text: ' ', hit: false },
      { text: 'cd', hit: true },
    ])
    // 'aab'（0-3）与 'aba'（1-4）在 'aaba' 上重叠，合并成一整段而不是切出碎块。
    expect(highlightSegments('aaba', ['aab', 'aba'])).toEqual([{ text: 'aaba', hit: true }])
  })

  it('空查询给出全部小节且不进入搜索态', () => {
    const result = searchManual('')
    expect(result.querying).toBe(false)
    expect(result.sections.length).toBe(result.total)
    expect(result.total).toBeGreaterThan(10)
  })

  it('只有要点命中时，只留命中的那一组', () => {
    const result = searchManual('手续费')
    expect(result.sections.map((section) => section.key)).toEqual(['user-manual'])
    const section = result.sections[0]
    expect(section.groups.map((group) => group.label)).toEqual(['它不能回答什么'])
    expect(section.hits).toBe(1)
  })

  it('标题命中时整节保留——用户要找的就是这一页', () => {
    const result = searchManual('因子归因')
    const section = result.sections.find((item) => item.key === 'factor-attribution')
    expect(section).toBeDefined()
    // 整节保留，而不是只剩标题里那几个字。
    expect(section?.groups.map((group) => group.label)).toContain('操作步骤')
    expect(section?.groups.map((group) => group.label)).toContain('口径提醒（决定了结论能不能信）')
  })

  it('搜不到时是空的，但仍然知道处于搜索态（界面要靠它区分「没搜」和「搜不到」）', () => {
    const result = searchManual('这个词肯定不存在xyz')
    expect(result.querying).toBe(true)
    expect(result.sections).toEqual([])
    expect(result.hitLines).toBe(0)
  })

  it('锚点前缀固定，目录链接和正文 id 才会对得上', () => {
    expect(manualAnchor('factor-test')).toBe('manual-factor-test')
  })
})
