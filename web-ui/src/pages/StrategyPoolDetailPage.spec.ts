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
    // 顶栏每次挂载会读一次上游版本状态，那条走公共客户端、带请求选项；
    // 这里的断言只管本页自己的读请求：全是单参数 GET，没有偷偷用 POST 读数据。
    const pageCalls = fetch.mock.calls.filter(([url]) => !String(url).includes('/release-status'))
    expect(pageCalls.every((call) => call.length===1)).toBe(true)
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
      ranges: [
        { key: '1y', label: '最近一年', start_date: '2025-09-03', end_date: '2026-09-03', total_return: 0.12, max_drawdown: -0.08 },
        { key: 'ytd', label: '今年以来', start_date: '2026-01-01', end_date: '2026-09-03', total_return: 0.2, max_drawdown: -0.1 },
        { key: '2y', label: '最近两年', start_date: '2024-09-03', end_date: '2026-09-03', total_return: 0.3, max_drawdown: -0.15 },
        { key: '3y', label: '最近三年', start_date: '2023-09-03', end_date: '2026-09-03', total_return: 0.4, max_drawdown: -0.2 },
        { key: 'all', label: '全部区间', start_date: '2020-01-01', end_date: '2026-09-03', total_return: 0.5, max_drawdown: -0.3 },
        { key: 'created', label: '成立以来', start_date: '2026-01-01', end_date: '2026-09-03', total_return: 0.5, max_drawdown: -0.25 },
      ],
      exits: [
        { key: 'rule', label: '规则卖出', trades: 2, pnl: 1200 },
        { key: 'max_hold', label: '持有到期', trades: 1, pnl: -300 },
      ],
      analysis: {
        exposure: 0.55, max_consecutive_losses: 3, max_consecutive_wins: 2,
        top3_profit_share: 0.62, cost_total: 480, cost_share: 0.11,
        yearly: [
          { year: 2025, start_date: '2025-01-02', end_date: '2025-12-31', total_return: 0.1, max_drawdown: -0.05 },
          { year: 2026, start_date: '2026-01-05', end_date: '2026-09-03', total_return: 0.4, max_drawdown: -0.2 },
        ],
      },
      backtest: { start_date: '2026-01-01', end_date: '2026-09-03', bars: 3,
        benchmark_return: 0.3, excess_return: 0.2,
        performance: { total_return: 0.5, annual_return: 0.2, sharpe: 1.4, sortino: 2.1, calmar: 0.8,
          max_drawdown: -0.25, max_dd_duration: 33, volatility: 0.18, win_rate: 0.6, payoff_ratio: 1.5,
          profit_factor: 2.2, avg_holding_days: 17.5, total_trades: 24 },
        equity_curve: [{
          date: '2026-01-01', total: 1000, return_pct: 0, trade_directions: ['BUY', 'SELL'],
          trade_events: [
            { date: '2026-01-01', direction: 'BUY', price: 10, size: 100, signal_date: '2025-12-31' },
            { date: '2026-01-01', direction: 'SELL', price: 11, size: 100, pnl: 100, reason: '止盈' },
          ],
        }], trades: [] } }
    else data = { items: [{ date: '2026-09-03', direction: 'SELL', price: 12, size: 100 }], total: 123 }
    return { ok: true, json: async () => ({ data }) }
  })
  vi.stubGlobal('fetch', fetch)
  const wrapper = mount(StrategyPoolDetailPage)
  try {
    await flushPromises()
    const tradeCalls = () => fetch.mock.calls.map(([url]) => String(url)).filter((url) => url.includes('/trades'))
    const curveCalls = () => fetch.mock.calls.map(([url]) => String(url)).filter((url) => url.includes('/curve'))
    expect(tradeCalls()[0]).toContain('range=1y&order=desc&offset=0')
    expect(curveCalls()).toHaveLength(1)
    expect(curveCalls()[0]).toContain('range=1y')
    const point = wrapper.get('svg g[tabindex="0"]')
    await point.trigger('pointerenter')
    const marker = wrapper.get('[role="tooltip"]').text()
    for (const detail of ['日期：2026-01-01', '方向：买入', '方向：卖出', '价格：11.00',
      '数量：100', '盈亏：100.00', '原因：止盈', '信号日：2025-12-31']) {
      expect(marker).toContain(detail)
    }
    await point.trigger('pointerleave')
    expect(wrapper.find('[role="tooltip"]').exists()).toBe(false)

    // 版本状态、区间指标行和交易方向都得是中文，分页条要贴着表格显示页码。
    // 断言用结构而不是「夏普」「卖出」这类词——页面上本来就有「夏普最优」标签和
    // 「卖出规则」标题，用文字包含会假装验到了。
    expect(wrapper.text()).toContain('状态 已就绪')
    // 出场原因分布读窗口内的卖出成交：笔数与累计盈亏（净额）。
    const exits = wrapper.get('[data-testid="pool-exit-summary"]')
    expect(exits.text()).toContain('规则卖出')
    expect(exits.get('[data-testid="pool-exit-max_hold"]').text()).toContain('-300')
    // 行为与成本指标同为窗口口径：在场时间、连亏/连盈、盈利集中度、成本占比。
    const behavior = wrapper.get('[data-testid="pool-behavior-stats"]')
    expect(behavior.text()).toContain('55.00%')
    expect(behavior.text()).toContain('3 / 2 笔')
    expect(behavior.text()).toContain('62.00%')
    expect(behavior.text()).toContain('11.00%')
    const yearly = wrapper.get('[data-testid="pool-yearly-summary"]').findAll('tbody tr')
    expect(yearly).toHaveLength(2)
    expect(yearly[1]!.text()).toContain('2026')
    expect(yearly[1]!.text()).toContain('40.00%')
    expect(wrapper.find('.individual-trades .trade-sell').exists()).toBe(true)
    expect(wrapper.get('[data-testid="pool-trade-pager"]').text()).toContain('第 1 / 3 页 · 显示 1–50 笔 / 共 123 笔')
    expect(wrapper.get('[data-testid="pool-trade-prev"]').attributes('disabled')).toBeDefined()

    await wrapper.get('[data-testid="pool-trade-next"]').trigger('click')
    await flushPromises()
    expect(tradeCalls().at(-1)).toContain('offset=50')
    expect(wrapper.get('[data-testid="pool-trade-pager"]').text()).toContain('第 2 / 3 页 · 显示 51–100 笔 / 共 123 笔')
    // 曲线只跟「版本+区间」有关：翻页和换排序都不该重拉它，否则每点一次整张图重建一次
    expect(curveCalls()).toHaveLength(1)
    expect(wrapper.find('.individual-full-detail').exists()).toBe(true)

    await wrapper.get('[data-testid="pool-trade-order"]').setValue('asc')
    await flushPromises()
    expect(tradeCalls().at(-1)).toContain('order=asc&offset=0')
    expect(curveCalls()).toHaveLength(1)

    // 区间表一次给全：六行、当前区间高亮，值直接来自后端算好的窗口数字。
    const summaryRows = wrapper.get('[data-testid="pool-range-summary"]').findAll('tbody tr')
    expect(summaryRows).toHaveLength(6)
    expect(summaryRows.map((row) => row.find('td').text())).toEqual([
      '最近一年', '今年以来', '最近两年', '最近三年', '全部区间', '成立以来',
    ])
    expect(summaryRows[0]!.classes()).toContain('active')
    expect(summaryRows[4]!.text()).toContain('50.00%')
    expect(summaryRows[4]!.text()).toContain('-30.00%')

    await wrapper.get('[data-testid="pool-range-btn-created"]').trigger('click')
    await flushPromises()
    expect(curveCalls()).toHaveLength(2)
    expect(curveCalls().at(-1)).toContain('range=created')
    expect(tradeCalls().at(-1)).toContain('range=created&order=asc&offset=0')

    // 子区间没有整段口径的夏普/胜率，那一排指标必须收起来，只留区间表上的收益与回撤。
    await wrapper.get('[data-testid="pool-range-btn-1y"]').trigger('click')
    await flushPromises()
    expect(curveCalls().at(-1)).toContain('range=1y')
    expect(tradeCalls().at(-1)).toContain('range=1y&order=asc&offset=0')
    expect(wrapper.find('[data-testid="pool-metrics"]').exists()).toBe(false)
    // 出场原因分布是窗口口径，子区间照样展示。
    expect(wrapper.find('[data-testid="pool-exit-summary"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="pool-behavior-stats"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="pool-yearly-summary"]').exists()).toBe(true)

    await wrapper.get('[data-testid="pool-range-btn-all"]').trigger('click')
    await flushPromises()
    const metrics = wrapper.get('[data-testid="pool-metrics"]')
    expect(metrics.findAll('.metric-group p')).toHaveLength(15)
    expect(metrics.text()).toContain('17.5 天（自然日）')
    expect(metrics.text()).toContain('33 个交易日')
    expect(metrics.text()).toContain('2.20')
  } finally { wrapper.unmount() }
})

it('does not claim the account is uninitialized while the first read is still in flight', async () => {
  // 加载途中也会闪「账户尚未初始化」，看起来像后台根本没跑，其实只是还没读完。
  window.history.replaceState({}, '', '/strategy-pool/SH/600000?objective=annual_return')
  const version = { version_id: 'v1', created_at: '2026-09-02T06:00:00+00:00', source: 'automatic',
    is_current: true, status: 'pending', summary: {},
    candidate: { candidate_id: 'v1', buy_rule: { conditions: [] }, sell_rule: { signals: [] }, config: {} } }
  let release: (value: unknown) => void = () => {}
  const pending = new Promise((resolve) => { release = resolve })
  const fetch = vi.fn((url: string) => {
    if (String(url).endsWith('/detail')) {
      return Promise.resolve({ ok: true, json: async () => ({ data: { name: '测试股', objectives: { annual_return: [version], sharpe: [] } } }) })
    }
    return pending
  })
  vi.stubGlobal('fetch', fetch)
  const wrapper = mount(StrategyPoolDetailPage)
  try {
    await flushPromises()
    expect(wrapper.find('[data-testid="pool-detail-not-initialized"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="pool-curve-state"]').text()).toContain('读取中')

    // 一份响应同时喂 /curve 和 /trades：账户真的没初始化时两者都是空
    release({ ok: true, json: async () => ({ data: { backtest: null, summary: {}, items: [], total: 0 } }) })
    await flushPromises()
    await flushPromises()
    expect(wrapper.text()).toContain('状态 待初始化')
    expect(wrapper.get('[data-testid="pool-detail-not-initialized"]').text()).toContain('账户尚未初始化')
    expect(wrapper.find('.individual-full-detail').exists()).toBe(false)
  } finally { wrapper.unmount() }
})

it('initializes only this stock from the detail page', async () => {
  window.history.replaceState({}, '', '/strategy-pool/SH/600000?objective=annual_return')
  const version = { version_id: 'v1', created_at: '2026-09-02T06:00:00+00:00', source: 'automatic',
    is_current: true, status: 'pending', summary: {},
    candidate: { candidate_id: 'v1', buy_rule: { conditions: [] }, sell_rule: { signals: [] }, config: {} } }
  let initialized = false
  const ready = {
    start_date: '2026-01-02', end_date: '2026-09-03', bars: 2,
    performance: { total_return: 0.2 },
    equity_curve: [
      { date: '2026-01-02', return_pct: 0, total: 1_000_000 },
      { date: '2026-09-03', return_pct: 0.2, total: 1_200_000 },
    ],
    trades: [],
  }
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const path = String(url)
    if (path.endsWith('/detail')) {
      return { ok: true, json: async () => ({ data: { name: '测试股', objectives: { annual_return: [version], sharpe: [] } } }) }
    }
    if (init?.method === 'POST' && path.endsWith('/jobs')) {
      initialized = true
      return { ok: true, json: async () => ({ data: { job_id: 'job-1', status: 'queued' } }) }
    }
    if (path.includes('/jobs/job-1')) {
      return { ok: true, json: async () => ({ data: { job_id: 'job-1', status: 'completed', progress: 1 } }) }
    }
    if (path.includes('/curve')) {
      return { ok: true, json: async () => ({ data: initialized ? { backtest: ready, summary: {}, ranges: [], exits: [], analysis: null } : { backtest: null, summary: {} } }) }
    }
    if (path.includes('/trades')) {
      return { ok: true, json: async () => ({ data: { items: [], total: 0 } }) }
    }
    return { ok: true, json: async () => ({ data: {} }) }
  })
  vi.stubGlobal('fetch', fetch)
  const wrapper = mount(StrategyPoolDetailPage)
  try {
    await flushPromises()
    expect(wrapper.find('[data-testid="pool-detail-not-initialized"]').exists()).toBe(true)

    await wrapper.get('[data-testid="pool-init-account"]').trigger('click')
    await flushPromises()
    await flushPromises()

    const posted = fetch.mock.calls.find(([, init]) => init?.method === 'POST')
    expect(JSON.parse(String(posted?.[1]?.body))).toEqual({
      mode: 'signals', objective: 'annual_return', market: 'SH', code: '600000',
    })
    expect(wrapper.find('[data-testid="pool-detail-not-initialized"]').exists()).toBe(false)
    expect(wrapper.find('.individual-full-detail').exists()).toBe(true)
  } finally { wrapper.unmount(); vi.unstubAllGlobals() }
})

it('prints the risk lines and the holding deadline of the tracked position', async () => {
  // 风控线只有引擎那份才算数：界面上标的价格、判定日期和到期日必须同源，
  // 否则用户按一个算不出来的价位挂单。
  window.history.replaceState({}, '', '/strategy-pool/SH/600000?objective=annual_return')
  const version = { version_id: 'v1', created_at: '2026-09-02T06:00:00+00:00', source: 'automatic',
    is_current: true, status: 'ready', summary: {},
    candidate: { candidate_id: 'v1', buy_rule: { conditions: [] }, sell_rule: { signals: [] }, config: {} } }
  const fetch = vi.fn(async (url: string) => {
    let data: unknown
    if (url.endsWith('/detail')) data = { name: '测试股', objectives: { annual_return: [version], sharpe: [] } }
    else if (url.includes('/curve')) data = { summary: { as_of_date: '2026-09-03', baseline_date: '2026-09-01',
      holding: [{ size: 100, entry_date: '2026-09-01', entry_price: 10, close: 9.4, unrealized_pnl_pct: -0.06,
        stop_loss_price: 9, trailing_stop_price: null, take_profit_price: null, risk_level_as_of: '2026-09-03',
        pending_exit_reason: '止损', pending_exit_signal_date: '2026-09-03', pending_exit_risk_trigger: 'stop_loss',
        max_hold_days: 20, max_hold_days_left: 3, max_hold_expiry_date: '2026-10-01' }] },
      backtest: { start_date: '2026-09-01', end_date: '2026-09-03', bars: 3, performance: { total_return: 0.5 },
        equity_curve: [{ date: '2026-09-01', total: 1000, return_pct: 0 }], trades: [] } }
    else data = { items: [], total: 0 }
    return { ok: true, json: async () => ({ data }) }
  })
  vi.stubGlobal('fetch', fetch)
  const wrapper = mount(StrategyPoolDetailPage)
  try {
    await flushPromises()
    const holding = wrapper.get('[data-testid="pool-detail-holding"]').text()
    expect(holding).toContain('止损 9.00（截至 2026-09-03 收盘） · 2026-10-01 到期 · 还剩 3 个交易日')
    // 未启用的止盈线不能显示成 0。
    expect(holding).not.toContain('0.00')
  } finally { wrapper.unmount() }
})
