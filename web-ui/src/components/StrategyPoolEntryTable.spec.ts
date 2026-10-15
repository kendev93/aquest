import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import type { StrategyPoolCandidate, StrategyPoolEntry, StrategyPoolSignal } from '../types'
import StrategyPoolEntryTable from './StrategyPoolEntryTable.vue'

const candidate = {
  candidate_id: 'candidate-600000',
  buy_rule: { signals: ['technical.macd'] },
  sell_rule: { signals: ['technical.rsi14'] },
  config: { stop_loss_pct: 0.08 },
  metrics: {
    validation: { annual_return: 0.24, sharpe: 1.35, closed_trades: 12 },
    holdout: { closed_trades: 4 },
    full_period: { total_return: 0.71, annual_return: 0.19, win_rate: 0.55, avg_holding_days: 12.5 },
  },
} as unknown as StrategyPoolCandidate

function entry(code: string, name = `测试${code}`): StrategyPoolEntry {
  return {
    market: 'SH',
    code,
    name,
    automatic_candidates: { annual_return: candidate, sharpe: candidate },
    manual_overrides: { annual_return: null, sharpe: null },
    active_candidate: candidate,
    active_source: 'automatic',
    status: 'ready',
    created_return: 0.12,
    benchmark: { full_period: { total_return: 0.5 } },
  } as StrategyPoolEntry
}

const buyState = { symbol: 'SH600000', status: 'buy', candidate_id: candidate.candidate_id } as StrategyPoolSignal

function mountTable(overrides: Record<string, unknown> = {}) {
  return mount(StrategyPoolEntryTable, {
    props: {
      entries: [entry('600000')],
      objective: 'annual_return' as const,
      states: [buyState],
      heldKeys: new Set<string>(),
      followKeys: new Set<string>(),
      testid: 'pool',
      ...overrides,
    },
  })
}

describe('StrategyPoolEntryTable', () => {
  it('renders the same 13 columns with prefixed test ids for each scope', () => {
    const wrapper = mountTable()

    expect(wrapper.findAll('thead th')).toHaveLength(13)
    expect(wrapper.get('[data-testid="pool-row-600000"]').text()).toContain('触发买入')
    expect(wrapper.get('[data-testid="pool-entry-benchmark-600000"]').text()).toContain('全段基准 +50.00%')
    expect(wrapper.get('[data-testid="pool-holding-days-600000"]').text()).toContain('12.5 天')
    expect(wrapper.get('[data-testid="pool-detail-600000"]').attributes('href')).toBe(
      '/strategy-pool/SH/600000?objective=annual_return&list=',
    )
    expect(wrapper.get('[data-testid="pool-follow-entry-600000"]').text()).toBe('关注')

    const focus = mountTable({ testid: 'focus' })
    expect(focus.find('[data-testid="focus-row-600000"]').exists()).toBe(true)
    expect(focus.find('[data-testid="pool-row-600000"]').exists()).toBe(false)
  })

  it('carries the list context into the detail link so 返回 restores the table state', () => {
    const wrapper = mountTable({ listContext: 'objective=sharpe&page=3' })

    expect(wrapper.get('[data-testid="pool-detail-600000"]').attributes('href'))
      .toBe('/strategy-pool/SH/600000?objective=annual_return&list=objective%3Dsharpe%26page%3D3')
  })

  it('shows sort buttons only for the sortable market table', () => {
    const sortable = mountTable()
    expect(sortable.findAll('thead button')).toHaveLength(9)
    expect(sortable.get('thead button').text()).toContain('基准涨跌')

    const fixed = mountTable({ sortable: false, sortColumn: 'win_rate' as const })
    expect(fixed.findAll('thead button')).toHaveLength(0)
    expect(fixed.get('thead').text()).toContain('平均持仓')
  })

  it('marks the active sort direction on one column and hints the next click', () => {
    const descending = mountTable({ sortColumn: 'sharpe' as const, sortDirection: 'desc' as const })
    expect(descending.get('[data-testid="pool-sort-sharpe"]').text()).toBe('夏普 ↓')
    expect(descending.get('[data-testid="pool-sort-sharpe"]').attributes('title'))
      .toBe('当前按夏普从高到低排序；点击改为低→高，再点取消排序回到按状态分组')
    // 非活动列不带方向箭头，但提示里说明三态循环怎么走。
    expect(descending.get('[data-testid="pool-sort-annual_return"]').text()).toBe('年化')
    expect(descending.get('[data-testid="pool-sort-annual_return"]').attributes('title'))
      .toBe('点击按年化从高到低排序；再点改为低→高，第三次取消排序')

    const ascending = mountTable({ sortColumn: 'sharpe' as const, sortDirection: 'asc' as const })
    expect(ascending.get('[data-testid="pool-sort-sharpe"]').text()).toBe('夏普 ↑')
    expect(ascending.get('[data-testid="pool-sort-sharpe"]').attributes('title'))
      .toBe('当前按夏普从低到高排序；点击取消排序，回到按状态分组')

    // 取消排序回到 status 档：所有列头都不再显示箭头。
    const unsorted = mountTable({ sortColumn: 'status' as const, sortDirection: 'asc' as const })
    expect(unsorted.get('[data-testid="pool-sort-sharpe"]').text()).toBe('夏普')
  })

  it('marks followed rows and emits the row the user acted on', async () => {
    const wrapper = mountTable({ followKeys: new Set(['SH600000']) })

    expect(wrapper.get('[data-testid="pool-follow-entry-600000"]').text()).toBe('已关注')
    expect(wrapper.get('[data-testid="pool-follow-entry-600000"]').attributes('title'))
      .toBe('取消关注 SH 600000')

    await wrapper.get('[data-testid="pool-follow-entry-600000"]').trigger('click')
    await wrapper.get('[data-testid="pool-rerun-entry-600000"]').trigger('click')
    await wrapper.get('thead button').trigger('click')

    expect(wrapper.emitted('toggleFollow')?.[0]?.[0]).toMatchObject({ code: '600000' })
    expect(wrapper.emitted('rerun')?.[0]?.[0]).toMatchObject({ code: '600000' })
    expect(wrapper.emitted('sort')?.[0]).toEqual(['benchmark_return'])
  })

  it('disables rerun while a task owns the worker and keeps the queue label', () => {
    const wrapper = mountTable({
      busy: true,
      busyIntent: 'full',
      queuedSymbol: 'SH600000',
    })

    const rerun = wrapper.get('[data-testid="pool-rerun-entry-600000"]')
    expect((rerun.element as HTMLButtonElement).disabled).toBe(true)
    expect(rerun.text()).toBe('排队中')
    expect(rerun.attributes('title')).toBe('该标的已排队重跑')
  })

  it('renders the empty row with the caller wording across all columns', () => {
    const wrapper = mountTable({ entries: [], emptyText: '策略池为空，开始全市场研究后会逐只写入候选。' })

    const empty = wrapper.get('[data-testid="pool-entry-empty"]')
    expect(empty.text()).toContain('策略池为空')
    expect(empty.attributes('colspan')).toBe('13')
  })
})
