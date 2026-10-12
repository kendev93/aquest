// 使用手册页的取数：把 pageGuides.json 那份唯一数据源切成「能按关键词搜、能高亮」的视图。
// 内容不在本文件里写死——页内折叠说明、这一页、docs/user-manual.md 三处都从同一份 JSON 出，
// 任何一处单独改都会三处说法不一致。

import guidesData from '../data/pageGuides.json'
import type {
  ManualGroup,
  ManualLine,
  ManualSection,
  ManualSearchResult,
  ManualSegment,
  ManualTone,
  PageGuideEntry,
} from '../types'

const guides = (guidesData as unknown as { guides: PageGuideEntry[] }).guides

interface GroupSpec {
  label: string
  ordered: boolean
  tone: ManualTone
  pick: (guide: PageGuideEntry) => string[]
}

const GROUP_SPECS: GroupSpec[] = [
  { label: '什么时候用它', ordered: false, tone: 'plain', pick: (guide) => guide.useCases },
  { label: '操作步骤', ordered: true, tone: 'plain', pick: (guide) => guide.steps },
  { label: '它能回答什么', ordered: false, tone: 'plain', pick: (guide) => guide.answers },
  { label: '它不能回答什么', ordered: false, tone: 'limits', pick: (guide) => guide.limits },
  {
    label: '口径提醒（决定了结论能不能信）',
    ordered: false,
    tone: 'caliber',
    pick: (guide) => guide.caliber,
  },
]

/**
 * 查询串切成关键词：按空白分，多个词之间是「都要出现」。
 * 中文没有分词，所以做的是字面包含——「手续费」搜不到只写了「佣金」的那条，
 * 这是明面上的取舍，写在页内说明里，不假装懂同义词。
 */
export function parseQuery(query: string): string[] {
  return query.trim().toLowerCase().split(/\s+/).filter(Boolean)
}

function matches(text: string, terms: string[]): boolean {
  if (!terms.length) return true
  const lower = text.toLowerCase()
  return terms.every((term) => lower.includes(term))
}

/** 把一段文字按命中的区间切开，命中区间合并相邻/重叠部分。 */
export function highlightSegments(text: string, terms: string[]): ManualSegment[] {
  if (!terms.length) return [{ text, hit: false }]
  const lower = text.toLowerCase()
  const ranges: Array<[number, number]> = []
  for (const term of terms) {
    let from = 0
    for (;;) {
      const found = lower.indexOf(term, from)
      if (found < 0) break
      ranges.push([found, found + term.length])
      from = found + term.length
    }
  }
  if (!ranges.length) return [{ text, hit: false }]

  ranges.sort((left, right) => left[0] - right[0])
  const merged: Array<[number, number]> = []
  for (const range of ranges) {
    const last = merged[merged.length - 1]
    if (last && range[0] <= last[1]) last[1] = Math.max(last[1], range[1])
    else merged.push([range[0], range[1]])
  }

  const segments: ManualSegment[] = []
  let cursor = 0
  for (const [start, end] of merged) {
    if (start > cursor) segments.push({ text: text.slice(cursor, start), hit: false })
    segments.push({ text: text.slice(start, end), hit: true })
    cursor = end
  }
  if (cursor < text.length) segments.push({ text: text.slice(cursor), hit: false })
  return segments
}

/** 目录锚点。顶栏是 sticky 的，跳转靠 CSS 的 scroll-margin-top 让开。 */
export function manualAnchor(key: string): string {
  return `manual-${key}`
}

export function searchManual(query: string): ManualSearchResult {
  const terms = parseQuery(query)
  const querying = terms.length > 0
  const sections: ManualSection[] = []
  let hitLines = 0

  for (const guide of guides) {
    const titleHit = querying && matches(guide.title, terms)
    const summaryHit = querying && matches(guide.summary, terms)
    // 标题或摘要命中 ⇒ 用户要找的就是这一页，整页都留着；只留几条反而看不出上下文。
    const keepWhole = !querying || titleHit || summaryHit

    const groups: ManualGroup[] = []
    let hits = (titleHit ? 1 : 0) + (summaryHit ? 1 : 0)
    for (const spec of GROUP_SPECS) {
      const items = spec.pick(guide)
      const matched = items.filter((item) => matches(item, terms))
      const kept = keepWhole ? items : matched
      if (!kept.length) continue
      hits += matched.length
      const lines: ManualLine[] = kept.map((text) => ({
        text,
        segments: highlightSegments(text, terms),
      }))
      groups.push({ label: spec.label, ordered: spec.ordered, tone: spec.tone, lines })
    }

    if (querying && !hits) continue
    hitLines += hits
    sections.push({
      key: guide.key,
      path: guide.path,
      title: guide.title,
      titleSegments: highlightSegments(guide.title, terms),
      summarySegments: highlightSegments(guide.summary, terms),
      groups,
      hits,
      next: guide.next ?? [],
    })
  }

  return { querying, sections, total: guides.length, hitLines }
}
