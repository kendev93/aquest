import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import type { IndividualResearchFullBacktest } from '../types/individualResearch'
import IndividualResearchBacktestDetail from './IndividualResearchBacktestDetail.vue'

function backtest(overrides: Partial<IndividualResearchFullBacktest> = {}): IndividualResearchFullBacktest {
  return {
    start_date: '2024-01-02',
    end_date: '2026-09-03',
    bars: 3,
    performance: { total_return: 0.2 },
    benchmark: {
      label: '个股买入持有',
      instrument_count: 1,
      total_return: 0.1,
      annual_return: 0.03,
      max_drawdown: -0.12,
      dates: [20240102, 20250102, 20260102, 20260903],
      totals: [1_000_000, 1_050_000, 1_080_000, 1_100_000],
    },
    equity_curve: [
      { date: '2024-01-02', return_pct: 0, total: 1_000_000 },
      { date: '2025-01-02', return_pct: 0.04, total: 1_040_000 },
      { date: '2026-01-02', return_pct: 0.08, total: 1_080_000 },
      { date: '2026-09-03', return_pct: 0.2, total: 1_200_000 },
    ],
    trades: [],
    ...overrides,
  }
}

describe('IndividualResearchBacktestDetail', () => {
  it('overlays the buy-and-hold benchmark curve with a legend and year ticks', () => {
    const wrapper = mount(IndividualResearchBacktestDetail, {
      props: { backtest: backtest(), returnCurve: true, launchDate: '2024-06-01T00:00:00+08:00' },
    })
    const polylines = wrapper.findAll('polyline')
    // 策略曲线 + 基准曲线
    expect(polylines).toHaveLength(2)
    expect(wrapper.text()).toContain('基准·买入持有')
    expect(wrapper.text()).toContain('策略')
    // 2025 / 2026 两个年份刻度
    expect(wrapper.text()).toContain('2025')
    expect(wrapper.text()).toContain('2026')
  })

  it('renders legend markers for buy and sell and hides the benchmark when no series exists', () => {
    const withoutSeries = backtest({
      benchmark: { label: '个股买入持有', instrument_count: 1, total_return: 0.1, annual_return: 0.03, max_drawdown: -0.12 },
      equity_curve: [
        { date: '2024-01-02', return_pct: 0, trade_directions: ['BUY'] },
        { date: '2025-01-02', return_pct: 0.1, trade_directions: ['SELL'] },
      ],
    })
    const wrapper = mount(IndividualResearchBacktestDetail, {
      props: { backtest: withoutSeries, returnCurve: true },
    })
    expect(wrapper.findAll('polyline')).toHaveLength(1)
    expect(wrapper.text()).toContain('买入')
    expect(wrapper.text()).toContain('卖出')
    expect(wrapper.findAll('circle title')).toHaveLength(2)
  })

  it('renders trade rows with Chinese sides, the signal date, and no English risk token', () => {
    const withTrades = backtest({
      trades: [
        { date: '2024-01-02', signal_date: '2024-01-01', direction: 'BUY', price: 10, size: 100, pnl: 0, reason: '排名入选' },
        // 风控出场的 reason 已经是中文「止损」，再挂一列英文 stop_loss 是同一件事说两遍
        { date: '2024-02-20', signal_date: '2024-02-19', direction: 'SELL', price: 12, size: 100, pnl: 200, reason: '止损', risk_trigger: 'stop_loss' },
      ],
    })
    const wrapper = mount(IndividualResearchBacktestDetail, {
      props: { backtest: withTrades, returnCurve: true },
      slots: { 'trades-toolbar': '<nav id="pager">分页</nav>' },
    })
    const headers = wrapper.findAll('thead th').map((header) => header.text())
    expect(headers).toEqual(['日期', '方向', '价格', '数量', '盈亏', '原因', '信号日'])
    const rows = wrapper.findAll('tbody tr')
    const buyCells = rows[0]!.findAll('td')
    const sellCells = rows[1]!.findAll('td')
    expect(buyCells[1]?.text()).toBe('买入')
    expect(sellCells[1]?.text()).toBe('卖出')
    expect(sellCells[1]?.classes()).toContain('trade-sell')
    expect(sellCells[6]?.text()).toBe('2024-02-19')
    expect(wrapper.text()).not.toContain('stop_loss')
    // 分页插槽排在表格之前，父页面才能把翻页按钮放到表头上面
    const toolbar = wrapper.get('#pager').element
    const table = wrapper.get('.individual-trades').element
    expect(toolbar.compareDocumentPosition(table) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })
})
