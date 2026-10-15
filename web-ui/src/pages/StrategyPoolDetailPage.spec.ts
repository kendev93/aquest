import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, expect, it, vi } from 'vitest'
import StrategyPoolDetailPage from './StrategyPoolDetailPage.vue'

vi.mock('../api/formulaScreen', () => ({ fetchMetadata: vi.fn().mockResolvedValue({ indicators: [
  { values:[{id:'technical.macd',display_name:'MACD'}],signals:[] },
] }) }))
afterEach(() => { vi.unstubAllGlobals(); window.history.replaceState({},'', '/'); })

it('reads both objectives, human-readable rules, launch marker and paginated trades without a POST', async () => {
  window.history.replaceState({},'', '/strategy-pool/SH/600000?objective=sharpe&list=search%3D600000%26page%3D3')
  const version = (id: string) => ({ version_id:id, created_at:'2026-09-02T06:00:00+00:00',
    source:'automatic', is_current:true,status:'ready', summary:{}, candidate:{ candidate_id:id,
      buy_rule:{conditions:[{left_value:'technical.macd',operator:'gt',right_constant:0}]},
      sell_rule:{signals:[]}, config:{stop_loss_pct:.12} } })
  const fetch = vi.fn(async (url: string) => {
    let data: unknown
    if (url.endsWith('/detail')) data = {name:'测试股',objectives:{annual_return:[version('a')],sharpe:[version('s')]}}
    else if (url.includes('/curve')) data = { summary:{created_return:.5,post_launch_return:.25,
      inherited_position:true,baseline_date:'2026-09-01',as_of_date:'2026-09-03',has_post_launch_data:true,holding:[]},
      backtest:{start_date:'2026-09-01',end_date:'2026-09-03',bars:3,performance:{total_return:.5},
        equity_curve:[{date:'2026-09-01',total:1000,return_pct:0},{date:'2026-09-03',total:1500,return_pct:.5}],trades:[]} }
    else data = {items:[{date:'2026-09-01',direction:'BUY',price:10,size:100}],total:123}
    return {ok:true,json:async () => ({data})}
  })
  vi.stubGlobal('fetch',fetch)
  const wrapper = mount(StrategyPoolDetailPage)
  try {
    await flushPromises()
    expect(wrapper.text()).toContain('MACD > 0')
    expect(wrapper.text()).toContain('止损 12%')
    expect(wrapper.text()).toContain('50.00%')
    expect(wrapper.text()).toContain('25.00%')
    expect(wrapper.find('textarea').exists()).toBe(false)
    expect(wrapper.find('svg line[stroke-dasharray]').exists()).toBe(true)
    expect(wrapper.get('[data-testid="pool-detail-back"]').attributes('href')).toContain('page=3')
    await wrapper.get('[data-testid="detail-annual"]').trigger('click')
    await flushPromises()
    expect(fetch.mock.calls.some(([url]) => url.includes('/versions/a/curve'))).toBe(true)
    const next = wrapper.findAll('nav button')[1]!
    await next.trigger('click'); await flushPromises()
    expect(fetch.mock.calls.some(([url]) => url.includes('offset=50'))).toBe(true)
    expect(fetch.mock.calls.every((call) => call.length===1)).toBe(true)
  } finally { wrapper.unmount() }
})

it('defaults history trades to the latest settlement first and re-requests from page one', async () => {
  // 分页是后端按 offset 切的，所以切换排序必须把 offset 归零后重新取第一页；
  // 停在第 3 页再翻方向，看到的是同一段成交的反序，用户会以为数据错了。
  window.history.replaceState({}, '', '/strategy-pool/SH/600000?objective=annual_return')
  const version = { version_id: 'v1', created_at: '2026-09-02T06:00:00+00:00', source: 'automatic',
    is_current: true, status: 'ready', summary: {},
    candidate: { candidate_id: 'v1', buy_rule: { conditions: [] }, sell_rule: { signals: [] }, config: {} } }
  const fetch = vi.fn(async (url: string) => {
    let data: unknown
    if (url.endsWith('/detail')) data = { name: '测试股', objectives: { annual_return: [version], sharpe: [] } }
    else if (url.includes('/curve')) data = { summary: { as_of_date: '2026-09-03', baseline_date: '2026-01-01' },
      backtest: { start_date: '2026-01-01', end_date: '2026-09-03', bars: 3, performance: { total_return: 0.5 },
        equity_curve: [{ date: '2026-01-01', total: 1000, return_pct: 0 }], trades: [] } }
    else data = { items: [{ date: '2026-09-03', direction: 'SELL', price: 12, size: 100 }], total: 123 }
    return { ok: true, json: async () => ({ data }) }
  })
  vi.stubGlobal('fetch', fetch)
  const wrapper = mount(StrategyPoolDetailPage)
  try {
    await flushPromises()
    const tradeCalls = () => fetch.mock.calls.map(([url]) => String(url)).filter((url) => url.includes('/trades'))
    expect(tradeCalls()[0]).toContain('scope=full&order=desc&offset=0')
    // 版本状态、区间指标行和交易方向都得是中文，分页条要贴着表格显示页码。
    // 断言用结构而不是「夏普」「卖出」这类词——页面上本来就有「夏普最优」标签和
    // 「卖出规则」标题，用文字包含会假装验到了。
    expect(wrapper.text()).toContain('状态 已就绪')
    expect(wrapper.findAll('.individual-full-metrics span')).toHaveLength(8)
    expect(wrapper.find('.individual-trades .trade-sell').exists()).toBe(true)
    expect(wrapper.get('[data-testid="pool-trade-pager"]').text()).toContain('第 1 / 3 页 · 显示 1–50 笔 / 共 123 笔')
    expect(wrapper.get('[data-testid="pool-trade-prev"]').attributes('disabled')).toBeDefined()

    await wrapper.get('[data-testid="pool-trade-next"]').trigger('click')
    await flushPromises()
    expect(tradeCalls().at(-1)).toContain('offset=50')
    expect(wrapper.get('[data-testid="pool-trade-pager"]').text()).toContain('第 2 / 3 页 · 显示 51–100 笔 / 共 123 笔')

    await wrapper.get('[data-testid="pool-trade-order"]').setValue('asc')
    await flushPromises()
    expect(tradeCalls().at(-1)).toContain('order=asc&offset=0')
  } finally { wrapper.unmount() }
})
