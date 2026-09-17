/** 界面术语表：主文案一律用中文，英文名只在悬浮说明里出现。 */

export interface TermDefinition {
  /** 中文名，界面上直接显示。 */
  name: string
  /** 英文原名与缩写，放在悬浮说明里，方便和用户讨论或查资料时对照。 */
  english: string
  /** 一句白话解释，不堆公式。 */
  intro: string
}

export const TERMS: Record<string, TermDefinition> = {
  ic: {
    name: '信息系数',
    english: 'Information Coefficient（IC）',
    intro:
      '把同一个交易日的股票按某个指标排个名次，再看它们之后的收益排第几，' +
      '两组名次的吻合程度就是信息系数。取值 −1 到 1，越接近 1 说明这个指标越能把好股票排到前面。',
  },
  icMean: {
    name: '平均信息系数',
    english: 'Mean IC',
    intro:
      '每个有信号的交易日都能算出一个信息系数，把它们平均起来就是平均信息系数。' +
      '它回答「平均而言这个指标有多准」：A 股里能到 0.05 已经相当不错，接近 0 等于没有预测力。',
  },
  icir: {
    name: '信息系数信息比率',
    english: 'IC Information Ratio（ICIR）',
    intro:
      '平均信息系数除以它的波动幅度，相当于「这个准头的信噪比」。' +
      '数值越高说明每天表现越一致、实盘越靠得住；数值低就是时灵时不灵，哪怕平均值好看也别急着用。',
  },
  segment: {
    name: '分段同号',
    english: 'Segment sign agreement',
    intro:
      '把整个研究区间按时间切成若干段，分别算每段的信息系数，看有几段和总体方向一致。' +
      '6 段里 6 段同号说明这个规律贯穿始终；只有 3 段同号则多半是某段行情的运气。',
  },
  quantile: {
    name: '分层收益',
    english: 'Quantile returns',
    intro:
      '每个交易日按指标值把当天命中的股票分成五档，看第一档到第五档的平均收益。' +
      '收益随档位单调下降才是普遍规律；如果只有第一档特别好，说明盈利集中在极端样本上，实盘容易塌。',
  },
  topNLift: {
    name: '前 N 名提升',
    english: 'Top-N lift',
    intro:
      '只买按这个指标排在前 N 名的股票，平均收益比当日全部命中信号高出多少。' +
      '它回答「配上这个排序能多赚多少」，但因为它是在寻优段算出来的，必须拿验证段和留出段复验才算数。',
  },
  rankOrder: {
    name: '排序方向',
    english: 'Rank order（asc / desc）',
    intro:
      '降序表示指标值越大越优先买，升序表示越小越优先。' +
      '信息系数为负不代表指标没用，而是说明它该反着用 —— 系统会直接给出应该选哪个方向。',
  },
  dailyHits: {
    name: '日均命中股票数',
    english: 'Average daily hits',
    intro:
      '平均每个有信号的交易日有多少只股票触发买入条件。' +
      '拿它跟组合的持仓槽位数比一下就知道条件还有没有意义：命中 3 只时条件说了算，' +
      '命中 300 只而只有 5 个槽位时，真正决定买谁的是排序规则。',
  },
  slot: {
    name: '持仓槽位',
    english: 'Max positions',
    intro:
      '组合回测里同时最多持有几只标的。买入条件决定谁有资格进候选池，' +
      '排序规则决定谁抢到这有限的几个槽位，两者是互补又竞争的关系。',
  },
}
