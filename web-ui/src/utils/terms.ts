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
  topNWinRate: {
    name: '前 N 名胜率',
    english: 'Top-N win rate',
    intro:
      '假设组合只能买前 N 名：每个信号日先筛出命中这个条件的股票，再按排序规则取前 N 名，' +
      '只看这批「真会成交」的样本里有多少是赚钱的。拿它跟全池胜率对比，就能看出这个条件' +
      '在有限槽位的组合里还成不成立 —— 差得越多，说明原来那条结论越难直接移植过去。',
  },
  slot: {
    name: '持仓槽位',
    english: 'Max positions',
    intro:
      '组合回测里同时最多持有几只标的。买入条件决定谁有资格进候选池，' +
      '排序规则决定谁抢到这有限的几个槽位，两者是互补又竞争的关系。',
  },
  vipdoc: {
    name: '通达信行情目录',
    english: 'TongDaXin vipdoc directory',
    intro:
      '通达信把下载的日线按 <目录>/sh/lday/sh600000.day、<目录>/sz/lday/sz000001.day 这样的结构存盘。' +
      '后端跑在 Docker 容器里，看不到你电脑上的目录，所以要把这个目录挂载进容器才能导入；' +
      '这里的路径要填「容器里看到的路径」，不是电脑上的原始路径。',
  },
  '噪声门槛检验次数': {
    name: '噪声门槛检验次数',
    english: 'Permutation test runs',
    intro:
      '研究一次会评估上千个阈值，就算全是噪声，「最好的那一个」看起来也会很漂亮。' +
      '检验的做法是把交易结果随机打乱再挖一遍，看纯运气能把提升推到多高，用那个数当门槛。' +
      '次数越多门槛越稳，但每次大约要多花挖掘耗时的十分之一；填 0 表示不检验，结果里就不显示显著性。',
  },
  swingThreshold: {
    name: '波段阈值',
    english: 'Reversal multiple（× ATR）',
    intro:
      '一段行情要从高点回撤多少个 ATR 才算换了一个方向。阈值取 3 表示回撤 3 倍 ATR 就认为上一段结束、新的反向段开始。' +
      '阈值越大切出的段越少、每段走得越远；太小会把日常震荡也算成波段。',
  },
  swingLabel: {
    name: '波段标签',
    english: 'Swing label',
    intro:
      '给每个交易日打的是/否标记。「段起点」是事后归因：回头看才知道那天是底部，当时看不出来；' +
      '「近期将启动」是当天就能算出来的：未来若干天内会不会走出一段。只有后者能直接拿去交易。',
  },
  noiseFloor: {
    name: '噪声门槛',
    english: 'IC noise floor / permutation threshold',
    intro:
      '把日期顺序打乱后重算信息系数，纯运气能得到的最大值就是门槛。' +
      '平均信息系数要超过它，且经过多重检验校正，才认为这个指标真的有预测力。',
  },
  falseDiscovery: {
    name: '假发现率校正',
    english: 'Benjamini-Hochberg FDR（q 值）',
    intro:
      '同时检验几十个「指标 × 标签」组合，就算全是噪声也会有幾個看起来显著。' +
      '这里按假发现率把 p 值校正成 q 值：q < 0.05 表示这批结论里错误的最多占 5%。',
  },
  blockPermutation: {
    name: '分块置换',
    english: 'Block permutation',
    intro:
      '打乱数据时按连续的交易日整块搬动，而不是逐日打乱。' +
      '因为相邻几天的行情高度相关，逐日打乱会低估噪声、把运气误判成本事。',
  },
  thresholdHint: {
    name: '阈值提示',
    english: 'Threshold hint',
    intro:
      '每个交易日把全部股票按这个指标排序，取第 80 百分位的数值，再对所有交易日取中位数。' +
      '它只是「从哪里开始试」的起点，会随行情强弱漂移，不能直接当成固定参数。',
  },
  tradeable: {
    name: '可交易',
    english: 'Tradable / observable in real time',
    intro:
      '标记这条结论当天能不能真的判断出来。「近期将启动」当天就算得出来，可以直接用；' +
      '「段起点」只有事后才知道，拿它做条件回测不会偷看未来，但它并不保证那段涨幅能兑现。',
  },
  benchmark: {
    name: '基准对照',
    english: 'Equal-weight buy-and-hold benchmark',
    intro:
      '同一批股票、同一笔本金，在回测第一天按收盘价各买一点、然后一直不动，' +
      '这就是基准。它回答的是「这套选股规则，是不是比闭着眼各买一点更强」。' +
      '基准不计手续费、不考虑必须用整手买，因为它是拿来比的尺子，不是真去执行的策略。',
  },
  excessReturn: {
    name: '超额收益',
    english: 'Excess return over benchmark',
    intro:
      '策略的年化收益减去基准的年化收益。为正说明这套规则确实挑出了更强的标的，' +
      '为负说明它还不如把池子里的股票各买一点然后躺着。',
  },
}
