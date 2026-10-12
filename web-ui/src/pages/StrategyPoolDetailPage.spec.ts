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
