import { readFileSync, readdirSync } from 'node:fs'
import { resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

// 主题只有 styles.css :root 里那一份 token。写成 var(--text-muted, #64748b) 这种
// 「不存在的名字 + 兜底色」时，浏览器永远取兜底，页面其实没接上主题；连兜底都没写
// 的 var(--text-muted) 更糟：整条声明在计算期失效，颜色继承父级，肉眼还看不出坏。
// 之前有 26 处这种情况（--text-muted / --border-subtle / --line …），测试完全发现
// 不了，所以钉一条门禁：引用的名字必须在真的定义过（全局 :root 或本文件局部）。
const srcDir = resolve(process.cwd(), 'src')

const DEFINITION = /(--[a-z][a-z0-9-]*)\s*:/g
const REFERENCE = /var\(\s*(--[a-z][a-z0-9-]*)/g

function sourceFiles(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = resolve(dir, entry.name)
    if (entry.isDirectory()) sourceFiles(full, out)
    else if (entry.name.endsWith('.vue') || entry.name.endsWith('.css')) out.push(full)
  }
  return out
}

function undefinedReferences(file: string, source: string): string[] {
  // 组件里可以只写局部 token，所以定义集 = 全局 :root + 本文件。
  const defined = new Set<string>()
  for (const matched of (file.endsWith('.css') ? source : globalTokens() + source).matchAll(DEFINITION)) {
    defined.add(matched[1])
  }
  const missing: string[] = []
  for (const matched of source.matchAll(REFERENCE)) {
    if (!defined.has(matched[1])) missing.push(matched[1])
  }
  return [...new Set(missing)]
}

let cachedGlobals: string | null = null

function globalTokens(): string {
  if (cachedGlobals === null) {
    const styles = readFileSync(resolve(srcDir, 'styles.css'), 'utf-8')
    const root = /:root\s*\{([\s\S]*?)\}/.exec(styles)
    cachedGlobals = root?.[1] ?? ''
  }
  return cachedGlobals
}

describe('设计 token', () => {
  it('每个 var() 引用的都是已定义的 token', () => {
    const offenders: string[] = []
    for (const file of sourceFiles(srcDir)) {
      const missing = undefinedReferences(file, readFileSync(file, 'utf-8'))
      if (missing.length) {
        offenders.push(`${file.replace(srcDir + '/', '')}: ${missing.join(', ')}`)
      }
    }
    expect(offenders, '不存在的 token 名会静默走兜底色').toEqual([])
  })
})
