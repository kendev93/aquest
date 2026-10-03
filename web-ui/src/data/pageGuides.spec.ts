import { readFileSync, readdirSync } from 'node:fs'
import { resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

import guides from './pageGuides.json'

// vitest 运行时 import.meta.url 不是 file: 协议，用进程工作目录定位源码
//（这些用例都在 web-ui 目录下执行）。
const pagesDir = resolve(process.cwd(), 'src', 'pages')
const appSource = readFileSync(resolve(process.cwd(), 'src', 'App.vue'), 'utf-8')

// App.vue 里没有 /indicator-lab 的 startsWith 分支，它是兜底路由（最后那个 return）。
const FALLBACK_PATH = '/indicator-lab'

function pageFiles(): string[] {
  return readdirSync(pagesDir).filter((name) => name.endsWith('.vue'))
}

function pageKeysMentioned(): Map<string, string> {
  const found = new Map<string, string>()
  for (const file of pageFiles()) {
    const source = readFileSync(pagesDir + '/' + file, 'utf-8')
    const match = /<PageGuide\s+page-key="([^"]+)"/.exec(source)
    if (match) found.set(match[1], file)
  }
  return found
}

describe('pageGuides', () => {
  it('每个页面都挂了一份说明', () => {
    const missing = pageFiles().filter((file) => !/<PageGuide\s+page-key="([^"]+)"/.test(readFileSync(pagesDir + '/' + file, 'utf-8')) )
    expect(missing).toEqual([])
  })

  it('每条说明都被某个页面用上了，没有孤儿条目', () => {
    const mentioned = pageKeysMentioned()
    const orphans = guides.guides.filter((guide) => !mentioned.has(guide.key)).map((g) => g.key)
    expect(orphans).toEqual([])
  })

  it('每个页面引用的 key 在数据源里都存在', () => {
    const known = new Set(guides.guides.map((guide) => guide.key))
    for (const key of pageKeysMentioned().keys()) {
      expect(known.has(key), `页面引用了不存在的说明 ${key}`).toBe(true)
    }
  })

  it('路由路径和 App.vue 的分支一致', () => {
    for (const guide of guides.guides) {
      if (guide.path === FALLBACK_PATH) continue
      expect(appSource, `App.vue 缺少 ${guide.path} 的路由分支`).toContain(
        `pathname.startsWith('${guide.path}`,
      )
    }
  })

  it('必填字段都写满了', () => {
    for (const guide of guides.guides) {
      expect(guide.summary.length, `${guide.key} 缺摘要`).toBeGreaterThan(10)
      expect(guide.useCases.length, `${guide.key} 缺适用场景`).toBeGreaterThan(0)
      expect(guide.steps.length, `${guide.key} 缺操作步骤`).toBeGreaterThan(0)
      expect(guide.answers.length, `${guide.key} 缺「能回答什么」`).toBeGreaterThan(0)
      expect(guide.limits.length, `${guide.key} 缺「不能回答什么」`).toBeGreaterThan(0)
      expect(guide.caliber.length, `${guide.key} 缺口径提醒`).toBeGreaterThan(0)
    }
  })

  it('「下一步」指向的都是真实存在的页面', () => {
    const known = new Set(guides.guides.map((guide) => guide.path))
    for (const guide of guides.guides) {
      for (const next of guide.next) {
        expect(known.has(next.path), `${guide.key} 的下一步指向了不存在的 ${next.path}`).toBe(true)
      }
    }
  })

  it('每个页面都手写齐了全部导航链接', () => {
    const paths = guides.guides.map((guide) => guide.path)
    for (const file of pageFiles()) {
      const source = readFileSync(pagesDir + '/' + file, 'utf-8')
      for (const path of paths) {
        expect(source, `${file} 缺少指向 ${path} 的导航链接`).toContain(`href="${path}"`)
      }
    }
  })

  it('key 不重复', () => {
    const keys = guides.guides.map((guide) => guide.key)
    expect(new Set(keys).size).toBe(keys.length)
  })
})
