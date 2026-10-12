import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  PORTFOLIO_BACKTEST_HANDOFF_KEY,
  consumePortfolioBacktestHandoff,
  savePortfolioBacktestHandoff,
} from './portfolioBacktestHandoff'

import type { PortfolioBacktestHandoff } from './portfolioBacktestHandoff'

function write(value: unknown): void {
  sessionStorage.setItem(PORTFOLIO_BACKTEST_HANDOFF_KEY, JSON.stringify(value))
}

const stockPool: PortfolioBacktestHandoff = {
  kind: 'stock-pool',
  source: 'formula-screen',
  source_label: '公式选股',
  codes: ['600000', '000001'],
  ranking_value: 'custom.rank',
  rank_order: 'desc',
  mode: 'custom',
  formula_text: 'BUY:C>10;',
  formula_parameters: { n: 14 },
  signal_date: 20260901,
}

afterEach(() => {
  sessionStorage.clear()
  vi.restoreAllMocks()
})

describe('portfolioBacktestHandoff', () => {
  it('保存后可读出，并且读一次即失效', () => {
    expect(savePortfolioBacktestHandoff(stockPool)).toBe(true)
    expect(consumePortfolioBacktestHandoff()).toEqual(stockPool)
    // 目的地页面刷新时不该重复套用同一份交接。
    expect(consumePortfolioBacktestHandoff()).toBeNull()
  })

  it('存储不可用时保存失败而不是抛错', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('denied')
    })
    expect(savePortfolioBacktestHandoff(stockPool)).toBe(false)
  })

  it('没有交接记录时返回 null', () => {
    expect(consumePortfolioBacktestHandoff()).toBeNull()
  })

  it('损坏的 JSON 返回 null 并清掉原值', () => {
    sessionStorage.setItem(PORTFOLIO_BACKTEST_HANDOFF_KEY, '{ 不是 JSON')
    expect(consumePortfolioBacktestHandoff()).toBeNull()
    expect(sessionStorage.getItem(PORTFOLIO_BACKTEST_HANDOFF_KEY)).toBeNull()
  })

  it('非对象的 JSON 一律拒绝', () => {
    write([1, 2, 3])
    expect(consumePortfolioBacktestHandoff()).toBeNull()
    write(null)
    expect(consumePortfolioBacktestHandoff()).toBeNull()
  })

  it('股票池交接必须带非空代码列表', () => {
    write({ ...stockPool, codes: [] })
    expect(consumePortfolioBacktestHandoff()).toBeNull()
    write({ ...stockPool, codes: '600000' })
    expect(consumePortfolioBacktestHandoff()).toBeNull()
    // 只要有一个非空代码就放行（空白项是 UI 残留，不该整份丢弃）。
    const mixed = { ...stockPool, codes: ['  ', '600000'] }
    write(mixed)
    expect(consumePortfolioBacktestHandoff()).toEqual(mixed)
  })

  it('波段研究的规则交接原样透传', () => {
    const rules: PortfolioBacktestHandoff = {
      kind: 'rules',
      source: 'swing-research',
      source_label: '波段研究',
      buy_rule: null,
      sell_rule: null,
      ranking_value: null,
      rank_order: 'asc',
    }
    write(rules)
    expect(consumePortfolioBacktestHandoff()).toEqual(rules)
  })

  it('因子榜单交接必须带非空 value_id', () => {
    const ranking: PortfolioBacktestHandoff = {
      kind: 'ranking',
      source: 'factor-test',
      source_label: '因子检验',
      value_id: 'factor.ic',
      rank_order: 'desc',
      evidence: 'IC 0.12',
    }
    write(ranking)
    expect(consumePortfolioBacktestHandoff()).toEqual(ranking)
    write({ ...ranking, source: 'factor-attribution', value_id: '   ' })
    expect(consumePortfolioBacktestHandoff()).toBeNull()
    write({ ...ranking, source: 'unknown' })
    expect(consumePortfolioBacktestHandoff()).toBeNull()
  })

  it('kind 与 source 对不上时拒绝', () => {
    write({ ...stockPool, kind: 'rules' })
    expect(consumePortfolioBacktestHandoff()).toBeNull()
  })

  it('清理动作失败也不影响已解析的结果', () => {
    write(stockPool)
    vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => {
      throw new Error('denied')
    })
    expect(consumePortfolioBacktestHandoff()).toEqual(stockPool)
  })
})
