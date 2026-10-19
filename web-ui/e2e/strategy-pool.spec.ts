import { expect, test } from '@playwright/test'

for (const rebuild of [true, false]) {
  test(`full market submission rebuild=${rebuild}`, async ({ page }) => {
    let submitted: unknown
    await page.addInitScript(() => {
      localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
    })
    await page.route('**/api/v1/**', async (route) => {
      const path = new URL(route.request().url()).pathname
      let data: unknown = {}
      if (path.endsWith('/strategy-pool/jobs') && route.request().method() === 'POST') {
        submitted = route.request().postDataJSON()
        data = { job_id: 'research', status: 'queued' }
      } else if (path.endsWith('/strategy-pool/jobs/research')) {
        data = { job_id: 'research', status: 'running', progress: 0 }
      } else if (path.endsWith('/strategy-pool/jobs')) {
        data = { items: [] }
      } else if (path.endsWith('/strategy-pool/entries')) {
        data = { items: [{
          market: 'SH', code: '600000', name: '旧股票', status: 'no_strategy',
          active_candidate: null, automatic_candidates: {}, manual_overrides: {},
        }] }
      }
      await route.fulfill({ json: { data } })
    })
    await page.goto('/strategy-pool')
    await expect(page.getByTestId('pool-row-600000')).toBeVisible()
    await page.getByTestId(rebuild ? 'pool-start-full' : 'pool-start-incremental').click()
    await expect.poll(() => submitted).toEqual({
      mode: 'full', rebuild, risk_trials: 3000, max_hold_days: 20,
      boards: ['main', 'chinext', 'star'], exclude_st: false,
    })
    await expect(page.getByTestId('pool-job-progress')).toBeVisible()
    if (rebuild) await expect(page.getByTestId('pool-row-600000')).toHaveCount(0)
    else await expect(page.getByTestId('pool-row-600000')).toBeVisible()
  })
}

test('daily signals are not repeated on reload or objective switch, and manual scanning remains available', async ({ page }) => {
  const requests: unknown[] = []
  let latestJob = ''
  const history: Array<Record<string, unknown>> = []
  await page.addInitScript(() => localStorage.setItem('aquest.strategy-pool.objective', 'annual_return'))
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    let data: unknown = {}
    if (path.endsWith('/strategy-pool/jobs') && route.request().method() === 'POST') {
      const payload = route.request().postDataJSON()
      requests.push(payload)
      latestJob = `signals-${requests.length}`
      history.unshift({ job_id: latestJob, mode: 'signals', objective: payload.objective,
        status: 'completed', created_at: Date.now() / 1000 })
      data = { job_id: latestJob, status: 'queued' }
    } else if (path.endsWith('/strategy-pool/jobs')) {
      data = { items: history }
    } else if (path.includes('/strategy-pool/jobs/')) {
      const job = history.find((item) => path.endsWith(String(item.job_id)))
      data = { job_id: job?.job_id, status: 'completed', result: {
        mode: 'signals', objective: job?.objective, holding_scope: 'per_stock_strategy',
        holding_snapshot_complete: true, buy_signals: [], sell_signals: [], states: [],
      } }
    } else if (path.endsWith('/strategy-pool/entries')) {
      data = { items: [{ market: 'SH', code: '600000', name: '测试股', status: 'ready',
        active_candidate: { candidate_id: 'c1', buy_rule: { signals: [] }, sell_rule: { signals: [] }, metrics: {} },
        automatic_candidates: {}, manual_overrides: {},
      }] }
    }
    await route.fulfill({ json: { data } })
  })
  await page.goto('/strategy-pool')
  await expect(page.getByTestId('pool-row-600000')).toBeVisible()
  expect(requests).toHaveLength(0)
  await expect(page.getByTestId('pool-refresh-signals')).toBeEnabled()
  await page.reload()
  await expect(page.getByTestId('pool-row-600000')).toBeVisible()
  await page.getByTestId('pool-objective').selectOption('sharpe')
  await expect(page.getByTestId('pool-refresh-signals')).toBeEnabled()
  expect(requests).toHaveLength(0)
  await page.getByTestId('pool-refresh-signals').click()
  await expect.poll(() => requests.length).toBe(1)
  expect(requests[0]).toEqual({ mode: 'signals', objective: 'sharpe' })
})

test('saved stock statuses appear during full research without starting a signal scan', async ({ page }) => {
  let saved = 0
  let submitted = 0
  let listReads = 0
  const stock = { market: 'SH', code: '600004', name: '白云机场', status: 'ready',
    active_candidate: { candidate_id: 'c1', buy_rule: { signals: [] }, sell_rule: { signals: [] }, metrics: {} },
    automatic_candidates: {}, manual_overrides: {},
  }
  await page.addInitScript(() => localStorage.setItem('aquest.strategy-pool.objective', 'annual_return'))
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    let data: unknown = {}
    if (route.request().method() === 'POST') submitted++
    if (path.endsWith('/strategy-pool/jobs')) {
      data = { items: [{ job_id: 'full-live', mode: 'full', status: 'running' }] }
    } else if (path.endsWith('/strategy-pool/jobs/full-live')) {
      data = { job_id: 'full-live', status: 'running', progress: saved / 3,
        metadata: { finished_symbols: saved, total_symbols: 3 } }
    } else if (path.endsWith('/strategy-pool/entries')) {
      listReads++
      const items: Array<Record<string, unknown>> = saved ? [stock] : []
      if (saved > 1) items.push({ ...stock, code: '600006', name: '历史不足股',
        status: 'insufficient_history', active_candidate: null })
      data = { items }
    }
    await route.fulfill({ json: { data } })
  })
  await page.goto('/strategy-pool')
  await expect(page.getByTestId('pool-job-progress')).toContainText('全市场研究进行中')
  saved = 1
  await expect(page.getByTestId('pool-row-600004')).toBeVisible()
  await page.getByTestId('pool-entry-search').fill('600006')
  saved = 2
  await expect(page.getByTestId('pool-row-600006')).toContainText('历史样本不足')
  await expect(page.getByTestId('pool-entry-search')).toHaveValue('600006')
  await expect(page.getByTestId('pool-job-progress')).toContainText('全市场研究进行中')
  expect(listReads).toBe(3)
  expect(submitted).toBe(0)
})

test('per-stock research timers advance and survive page reload', async ({ page }) => {
  const startedAt = Date.now() / 1000 - 65
  await page.addInitScript(() => localStorage.setItem('aquest.strategy-pool.objective', 'annual_return'))
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    let data: unknown = {}
    if (path.endsWith('/strategy-pool/jobs')) {
      data = { items: [{ job_id: 'timed', mode: 'full', status: 'running' }] }
    } else if (path.endsWith('/strategy-pool/jobs/timed')) {
      data = { job_id: 'timed', status: 'running', metadata: {
        finished_symbols: 0,
        active_symbols: [
          { symbol: 'SH:600000', name: '浦发银行', progress: 0.4, started_at: startedAt, work_details: { stage: '搜索止损、止盈和冷却参数', current: 1600, total: 3000, best_return: .15, best_sharpe: 1.2 } },
          { symbol: 'SH:600004', name: '白云机场', progress: 0.1, started_at: startedAt + 60 },
        ],
      } }
    } else if (path.endsWith('/strategy-pool/entries')) {
      data = { items: [] }
    }
    await route.fulfill({ json: { data } })
  })
  await page.goto('/strategy-pool')
  await expect(page.getByTestId('research-work-details')).toContainText('1600 / 3000')
  await expect(page.getByTestId('research-work-details')).toContainText('15.00%')
  const timer = page.getByTestId('pool-elapsed-SH:600000')
  await expect(timer).toContainText('耗时 00:01:')
  await expect(page.getByTestId('pool-elapsed-SH:600004')).toContainText('耗时 00:00:')
  const initial = await timer.innerText()
  await expect.poll(() => timer.innerText()).not.toBe(initial)
  const elapsed = async () => {
    const match = /([0-9]+):([0-9]+):([0-9]+)/.exec(await timer.innerText())!
    return Number(match[1]) * 3600 + Number(match[2]) * 60 + Number(match[3])
  }
  const beforeReload = await elapsed()
  await page.reload()
  await expect(timer).toContainText('耗时 00:01:')
  expect(await elapsed()).toBeGreaterThanOrEqual(beforeReload)
})

test('task history shows durations, research split and per-mode averages', async ({ page }) => {
  const startedAt = Date.now() / 1000 - 3_600
  await page.addInitScript(() => localStorage.setItem('aquest.strategy-pool.objective', 'annual_return'))
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    let data: unknown = {}
    if (path.endsWith('/strategy-pool/jobs/stats')) {
      data = { sample_limit: 60, modes: { full: {
        samples: 3, avg_duration_seconds: 3_600, median_duration_seconds: 3_000, started_at_samples: 3,
        throughput_samples: 4, avg_seconds_per_symbol: 60, median_seconds_per_symbol: 45, avg_parallelism: 4,
      } } }
    } else if (path.endsWith('/strategy-pool/jobs')) {
      data = { items: [
        { job_id: 'timed', mode: 'full', status: 'running', created_at: startedAt - 600, started_at: startedAt,
          // total_symbols 是预扫描剔掉复用之后的队列长度，completed 却含复用：两者分母不同。
          total_symbols: 1_000, completed_symbols: 120, skipped_symbols: 20, newly_researched_symbols: 100, maximum_parallelism: 4 },
        ...Array.from({ length: 8 }, (_, index) => ({ job_id: `scan-${index}`, mode: 'signals', status: 'completed',
          created_at: Date.now() / 1000 - index, duration_seconds: 30, duration_includes_queue: false, automatic: true })),
        { job_id: 'old-full', mode: 'full', status: 'cancelled', created_at: 1_700_000_000, automatic: true,
          duration_seconds: 18_720, duration_includes_queue: true, total_symbols: 4_357, completed_symbols: 3_498,
          skipped_symbols: 859, newly_researched_symbols: 2_639, failed_symbols: 10, maximum_parallelism: 4 },
      ] }
    } else if (path.endsWith('/strategy-pool/jobs/timed')) {
      data = { job_id: 'timed', status: 'running', created_at: startedAt - 600, started_at: startedAt, metadata: {
        total_symbols: 1_000, completed_symbols: 120, skipped_symbols: 20, failed_symbols: 0,
        current_parallelism: 4, maximum_parallelism: 4,
      } }
    } else if (path.endsWith('/strategy-pool/entries')) {
      data = { items: [] }
    }
    await route.fulfill({ json: { data } })
  })
  await page.goto('/strategy-pool')

  // 后台跟踪每十分钟一条扫描记录：分组之后长跑的全市场任务不会再被挤出列表。
  await expect(page.getByTestId('pool-history-old-full')).toContainText('耗时 5 小时 12 分钟（含排队）')
  await expect(page.getByTestId('pool-history-old-full')).toContainText('已完成 3498/4357（新研究 2639 · 复用 859）')
  await expect(page.getByTestId('pool-history-old-full')).toContainText('自动触发')
  await expect(page.getByTestId('pool-history-timed')).toContainText('已运行 1 小时')
  await expect(page.getByTestId('pool-research-pace')).toContainText('平均每只新研究标的 36')
  await expect(page.getByTestId('pool-research-pace')).toContainText('剩余 900 只预计还需 9 小时')
  await expect(page.getByTestId('pool-history-stats')).toContainText('全市场重选近 3 次完成耗时：中位 50 分钟')
  await expect(page.getByTestId('pool-history-stats')).toContainText('每只新研究标的中位 45.0 秒（4 轮样本，平均并行 4.0）')
  await expect(page.getByTestId('pool-history-stats-note')).toContainText('每种任务各取最近 60 次终态记录')
})

test('pool details are read-only and expose both rules, launch curve and paged trades', async ({ page }) => {
  let posts = 0
  const tradeQueries: string[] = []
  const version = (id: string) => ({ version_id:id,created_at:'2026-09-02T06:00:00+00:00',
    source:'automatic',is_current:true,status:'ready',summary:{},candidate:{candidate_id:id,
      buy_rule:{conditions:[{left_value:'price.close',operator:'gt',right_constant:10}]},
      sell_rule:{conditions:[{left_value:'price.close',operator:'lt',right_constant:9}]},config:{stop_loss_pct:.12}} })
  await page.route('**/api/v1/**', async route => {
    if (route.request().method()==='POST') posts++
    const url = new URL(route.request().url())
    const path = url.pathname
    let data: unknown = {}
    if (path.endsWith('/detail')) data={name:'测试股',objectives:{annual_return:[version('a')],sharpe:[version('s')]}}
    else if (path.endsWith('/curve')) data={summary:{created_return:.5,post_launch_return:.25,
      inherited_position:true,baseline_date:'2026-09-01',as_of_date:'2026-09-03',holding:[],has_post_launch_data:true},
      backtest:{start_date:'2026-09-01',end_date:'2026-09-03',bars:3,performance:{total_return:.5},
        equity_curve:[{date:'2026-09-01',total:1000,return_pct:0,trade_directions:['BUY']},{date:'2026-09-03',total:1500,return_pct:.5}],trades:[]} }
    else if (path.endsWith('/trades')) { tradeQueries.push(url.search); data={items:[{date:'2026-09-01',signal_date:'2026-08-31',direction:'BUY',size:100,price:10,reason:'排名入选'}],total:101} }
    else if (path.includes('/metadata')) data={indicators:[]}
    await route.fulfill({json:{data}})
  })
  await page.goto('/strategy-pool/SH/600000?objective=annual_return&list=page%3D2%26search%3D600000')
  await expect(page.getByTestId('pool-detail-page')).toBeVisible()
  await expect(page.getByRole('heading',{name:'交易规则'})).toBeVisible()
  await expect(page.getByRole('heading',{name:'收益曲线与历史交易'})).toBeVisible()
  await expect(page.locator('.metrics b').filter({hasText:'50.00%'})).toBeVisible()
  await expect(page.locator('svg')).toBeVisible()
  await expect(page.getByText('上线 2026-09-02',{exact:true})).toBeVisible()
  await expect(page.locator('svg line[stroke-dasharray]')).toHaveAttribute('x1','400')
  await expect(page.locator('textarea')).toHaveCount(0)
  // 历史交易默认最近成交在前，排序由后端在 offset 切片之前完成。
  expect(tradeQueries[0]).toContain('order=desc')
  // 分页条紧贴交易表并给出页码；方向列与曲线图例同词
  await expect(page.getByTestId('pool-trade-pager')).toContainText('第 1 / 3 页 · 显示 1–50 笔 / 共 101 笔')
  await expect(page.locator('.individual-trades tbody td').nth(1)).toHaveText('买入')
  await page.getByTestId('detail-sharpe').click()
  await page.getByTestId('pool-trade-next').click()
  await expect(page.getByTestId('pool-trade-pager')).toContainText('第 2 / 3 页 · 显示 51–100 笔 / 共 101 笔')
  await page.getByTestId('pool-trade-order').selectOption('asc')
  await expect.poll(() => tradeQueries.at(-1)).toContain('order=asc&offset=0')
  expect(posts).toBe(0)
  await page.getByRole('link',{name:'← 返回全市场标的状态'}).click()
  await expect(page).toHaveURL(/page=2.*search=600000/)
})

test('following a symbol scopes it into the 特别关注 panel', async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem('aquest.strategy-pool.objective', 'annual_return')
  })
  let focus: unknown[] = []
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    const method = route.request().method()
    let data: unknown = {}
    if (path.includes('/strategy-pool/focus')) {
      if (method === 'POST') focus = [{ market: 'SH', code: '600000', name: '测试股票' }]
      else if (method === 'DELETE') focus = []
      data = { items: focus }
    } else if (path.endsWith('/strategy-pool/entries')) {
      data = { items: [{
        market: 'SH', code: '600000', name: '测试股票', status: 'ready',
        active_candidate: null, automatic_candidates: {}, manual_overrides: {},
      }] }
    } else if (path.endsWith('/strategy-pool/jobs')) {
      data = { items: [] }
    }
    await route.fulfill({ json: { data } })
  })
  await page.goto('/strategy-pool')

  await expect(page.getByTestId('pool-focus-empty')).toBeVisible()

  await page.getByTestId('pool-follow-entry-600000').click()
  await expect(page.getByTestId('focus-row-600000')).toBeVisible()
  await expect(page.getByTestId('focus-follow-entry-600000')).toHaveText('取消关注')
  await expect(page.getByTestId('pool-follow-entry-600000')).toHaveText('取消关注')

  await page.getByTestId('focus-follow-entry-600000').click()
  await expect(page.getByTestId('focus-row-600000')).toHaveCount(0)
  await expect(page.getByTestId('pool-focus-empty')).toBeVisible()

  await page.reload()
  await expect(page.getByTestId('pool-focus-empty')).toBeVisible()
})

test('the post-launch board ranks tracked symbols and keeps symbols without a window out', async ({ page }) => {
  // 上线后收益榜的价值全在「谁在前面」和「谁不该在这里」：刚创建、还没有新行情的标的
  // 后端给的收益是 0，排进去就是一条假平线压住真数字。
  const item = (code: string, name: string, post: number | null, benchmark: number | null, days: number | null, asOf: string, hasData: boolean) => ({
    market: code.startsWith('6') ? 'SH' : 'SZ', code, name, status: 'ready',
    automatic_candidates: {}, manual_overrides: {}, active_source: 'automatic',
    active_candidate: { candidate_id: `c-${code}`, buy_rule: { signals: [] }, sell_rule: { signals: [] }, metrics: {} },
    tracking: { version_id: `v-${code}` }, tracking_status: 'ready', tracking_as_of_date: asOf,
    created_return: 0.1, post_launch_return: post, post_launch_benchmark_return: benchmark,
    post_launch_days: days, post_launch_has_data: hasData,
    latest_close: 12.34, latest_close_date: asOf,
  })
  await page.addInitScript(() => localStorage.setItem('aquest.strategy-pool.objective', 'annual_return'))
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    let data: unknown = {}
    if (path.endsWith('/strategy-pool/entries')) {
      data = { items: [
        item('600000', '浦发银行', 0.3, 0.1, 80, '2026-09-30', true),
        item('601133', '柏诚股份', 0.55, -0.2, 12, '2026-10-16', true),
        item('000001', '平安银行', 0, null, 0, '2026-10-16', false),
      ] }
    } else if (path.endsWith('/strategy-pool/jobs')) {
      data = { items: [] }
    }
    await route.fulfill({ json: { data } })
  })

  await page.goto('/strategy-pool')
  const board = page.getByTestId('pool-post-launch-board')
  await expect(board).toContainText('上线后收益榜 2')
  await expect(board.locator('tbody tr').first()).toContainText('601133')
  await expect(board.locator('tbody tr').first()).toContainText('+75.00%')
  await expect(board).not.toContainText('平安银行')
  await expect(board).toContainText('创建后还没有新行情 1 只')
  // 两只标的的行情截至日不同，窗口一长一短，必须点出来。
  await expect(board).toContainText('榜内 1 只的行情截至日落后')
  // 现价列：价从行情直接取，每一行都有——包括榜外那只还没有上线后窗口的标的。
  await expect(page.getByTestId('pool-latest-close-601133')).toHaveText('12.34')
  await expect(page.getByTestId('pool-latest-close-600000')).toHaveText('12.34')
  await expect(page.getByTestId('pool-latest-close-000001')).toHaveText('12.34')

  await page.getByTestId('pool-board-asc').click()
  await expect(board.locator('tbody tr').first()).toContainText('600000')

  await page.getByTestId('pool-board-min-days').selectOption('60')
  await expect(board.locator('tbody tr')).toHaveCount(1)
  await expect(board).toContainText('另有 1 只未满 60 个交易日')

  await page.getByTestId('pool-board-min-days').selectOption('0')
  // 每一行都要能点进详情，并且带上当前的目标与列表位置。
  await expect(board.locator('tbody tr a').first()).toHaveAttribute('href', /\/strategy-pool\/SH\/600000\?objective=annual_return/)
})
