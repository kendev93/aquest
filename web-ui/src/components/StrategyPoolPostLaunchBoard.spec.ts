import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import type { StrategyPoolEntry } from '../types'
import StrategyPoolPostLaunchBoard from './StrategyPoolPostLaunchBoard.vue'

function entry(overrides: Partial<StrategyPoolEntry> = {}): StrategyPoolEntry {
  return {
    market: 'SH',
    code: '600000',
    name: '浦发银行',
    automatic_candidates: { annual_return: null, sharpe: null },
    manual_overrides: { annual_return: null, sharpe: null },
    active_candidate: null,
    active_source: 'automatic',
    status: 'ready',
    tracking: { version_id: 'v-1' },
    tracking_status: 'ready',
    tracking_as_of_date: '2026-09-03',
    created_return: 0.4,
    post_launch_return: 0.25,
    post_launch_benchmark_return: 0.05,
    post_launch_days: 30,
    post_launch_has_data: true,
    ...overrides,
  }
}

function mountBoard(entries: StrategyPoolEntry[], listContext = 'page=2&search=600') {
  return mount(StrategyPoolPostLaunchBoard, {
    props: { entries, objective: 'annual_return' as const, listContext },
  })
}

describe('StrategyPoolPostLaunchBoard', () => {
  it('ranks the symbols that actually have a post-launch window, best first', () => {
    const wrapper = mountBoard([
      entry({ code: '600000', post_launch_return: 0.25, post_launch_days: 30 }),
      entry({ market: 'SZ', code: '000001', name: '平安银行', post_launch_return: 0.6, post_launch_days: 5 }),
      // 创建后还没有新行情：后端给的上线后收益是 0，那是「还没跑」，不能当成平盘排进榜。
      entry({ market: 'SZ', code: '000002', post_launch_return: 0, post_launch_has_data: false }),
      // 还没有生效版本，连窗口都不存在。
      entry({ market: 'SZ', code: '000003', tracking: null, post_launch_return: null }),
    ])

    const rows = wrapper.findAll('tbody tr')
    expect(rows).toHaveLength(2)
    expect(rows[0].text()).toContain('000001')
    expect(rows[0].findAll('td')[2].text()).toBe('60.00%')
    expect(wrapper.text()).toContain('还没有生效版本 1 只')
    expect(wrapper.text()).toContain('创建后还没有新行情 1 只')
  })

  it('shows the benchmark and the excess over it next to the raw return', () => {
    const wrapper = mountBoard([entry({ post_launch_return: 0.25, post_launch_benchmark_return: 0.05 })])
    const cells = wrapper.findAll('tbody tr td')
    expect(cells[3].text()).toBe('5.00%')
    expect(cells[4].text()).toBe('+20.00%')
    expect(cells[4].classes()).toContain('positive')

    // 老版本没有上线时间时基准算不出来，留白而不是假装超额等于收益。
    const noBenchmark = mountBoard([entry({ post_launch_benchmark_return: null })])
    expect(noBenchmark.findAll('tbody tr td')[4].text()).toBe('—')
  })

  it('flips the direction and keeps the longer track record first on a tie', async () => {
    const wrapper = mountBoard([
      entry({ market: 'SZ', code: '000004', post_launch_return: 0.1, post_launch_days: 3 }),
      entry({ market: 'SH', code: '600005', post_launch_return: 0.1, post_launch_days: 80 }),
      entry({ market: 'SH', code: '600006', post_launch_return: 0.9, post_launch_days: 12 }),
    ])

    const desc = wrapper.findAll('tbody tr')
    expect(desc[0].text()).toContain('600006')
    // 并列先比上线天数：跑了 80 天的 10% 比 3 天的 10% 更站得住，两个方向都这样排。
    expect(desc[1].text()).toContain('600005')
    expect(desc[2].text()).toContain('000004')

    await wrapper.get('[data-testid="pool-board-asc"]').trigger('click')
    const asc = wrapper.findAll('tbody tr')
    expect(asc[0].findAll('td')[2].text()).toBe('10.00%')
    expect(asc[0].text()).toContain('600005')
    expect(asc[2].text()).toContain('600006')
  })

  it('can require a track record and says how many symbols the gate dropped', async () => {
    const wrapper = mountBoard([
      entry({ code: '600000', post_launch_days: 30 }),
      entry({ market: 'SZ', code: '000001', post_launch_days: 4 }),
    ])
    expect(wrapper.findAll('tbody tr')).toHaveLength(2)

    await wrapper.get('[data-testid="pool-board-min-days"]').setValue('20')
    expect(wrapper.findAll('tbody tr')).toHaveLength(1)
    expect(wrapper.get('[data-testid="pool-board-caliber"]').text()).toContain('另有 1 只未满 20 个交易日')
  })

  it('warns when a ranked symbol is measured over a shorter window than the rest', () => {
    const wrapper = mountBoard([
      entry({ code: '600000', tracking_as_of_date: '2026-09-03' }),
      entry({ market: 'SZ', code: '000001', tracking_as_of_date: '2026-08-20' }),
    ])

    expect(wrapper.get('[data-testid="pool-board-caliber"]').text()).toContain('榜内 1 只的行情截至日落后')
  })

  it('links each row back to its detail page without losing the list position', () => {
    const wrapper = mountBoard([entry({ code: '601133', name: '柏诚股份' })])
    const link = wrapper.get('tbody tr a')

    expect(link.attributes('href')).toBe(
      '/strategy-pool/SH/601133?objective=annual_return&list=page%3D2%26search%3D600',
    )
  })

  it('says why the board is empty instead of showing a blank table', () => {
    const wrapper = mountBoard([entry({ tracking: null, post_launch_return: null, post_launch_has_data: null })])

    expect(wrapper.find('table').exists()).toBe(false)
    expect(wrapper.get('[data-testid="pool-board-empty"]').text()).toContain('模拟跟踪任务')
  })

  it('caps the board at twenty rows and reports how many were rankable', () => {
    const wrapper = mountBoard(
      Array.from({ length: 25 }, (_unused, index) => entry({ code: String(600000 + index) })),
    )

    expect(wrapper.findAll('tbody tr')).toHaveLength(20)
    expect(wrapper.text()).toContain('共 25 只可排')
  })
})
