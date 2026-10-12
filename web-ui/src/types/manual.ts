/** 使用手册页的视图类型；内容全部来自 pageGuides.json，这里只描述加工后的形态。 */

/** 一段文字。hit=true 的片段要渲染成 <mark>，让用户看清为什么这条被搜出来。 */
export interface ManualSegment {
  text: string
  hit: boolean
}

/** 一条要点（一句话）。segments 是它按命中切开后的样子。 */
export interface ManualLine {
  text: string
  segments: ManualSegment[]
}

export type ManualTone = 'plain' | 'limits' | 'caliber'

/** 一组要点，比如「什么时候用它」。搜索没命中时整组可能为空，那时就不渲染。 */
export interface ManualGroup {
  label: string
  ordered: boolean
  tone: ManualTone
  lines: ManualLine[]
}

export interface ManualSection {
  key: string
  path: string
  title: string
  titleSegments: ManualSegment[]
  summarySegments: ManualSegment[]
  groups: ManualGroup[]
  /** 命中条数：标题/摘要各算 1，要点每条算 1。给目录项显示「这页里有几条对得上」。 */
  hits: number
  next: { label: string; path: string }[]
}

export interface ManualSearchResult {
  /** 是否处于搜索态。空查询与「什么都搜不到」是两件事，界面要靠它区分。 */
  querying: boolean
  sections: ManualSection[]
  /** 全部小节数，搜索时也要能说「共 14 个页面里匹配到 N 个」。 */
  total: number
  hitLines: number
}

/** pageGuides.json 里一条说明的原始形态（只取手册页用得到的中文字段）。 */
export interface PageGuideEntry {
  key: string
  path: string
  title: string
  summary: string
  useCases: string[]
  steps: string[]
  answers: string[]
  limits: string[]
  caliber: string[]
  next: { label: string; path: string }[]
}
