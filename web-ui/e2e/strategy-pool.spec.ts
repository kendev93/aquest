import { expect, test } from '@playwright/test'

for (const rebuild of [true, false]) {
  test(`full market submission rebuild=${rebuild}`, async ({ page }) => {
    let submitted: unknown
    await page.addInitScript(() => {
      localStorage.setItem('indicator-lab.strategy-pool.objective', 'annual_return')
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
    await expect.poll(() => submitted).toEqual({ mode: 'full', rebuild, risk_trials: 3000 })
    await expect(page.getByTestId('pool-job-progress')).toBeVisible()
    if (rebuild) await expect(page.getByTestId('pool-row-600000')).toHaveCount(0)
    else await expect(page.getByTestId('pool-row-600000')).toBeVisible()
  })
}

test('daily signals are not repeated on reload or objective switch, and manual scanning remains available', async ({ page }) => {
  const requests: unknown[] = []
  let latestJob = ''
  const history: Array<Record<string, unknown>> = []
  await page.addInitScript(() => localStorage.setItem('indicator-lab.strategy-pool.objective', 'annual_return'))
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
  await page.addInitScript(() => localStorage.setItem('indicator-lab.strategy-pool.objective', 'annual_return'))
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
  await page.addInitScript(() => localStorage.setItem('indicator-lab.strategy-pool.objective', 'annual_return'))
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    let data: unknown = {}
    if (path.endsWith('/strategy-pool/jobs')) {
      data = { items: [{ job_id: 'timed', mode: 'full', status: 'running' }] }
    } else if (path.endsWith('/strategy-pool/jobs/timed')) {
      data = { job_id: 'timed', status: 'running', metadata: {
        finished_symbols: 0,
        active_symbols: [
          { symbol: 'SH:600000', name: '浦发银行', progress: 0.4, started_at: startedAt },
          { symbol: 'SH:600004', name: '白云机场', progress: 0.1, started_at: startedAt + 60 },
        ],
      } }
    } else if (path.endsWith('/strategy-pool/entries')) {
      data = { items: [] }
    }
    await route.fulfill({ json: { data } })
  })
  await page.goto('/strategy-pool')
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

test('pool details are read-only and expose both rules, launch curve and paged trades', async ({ page }) => {
  let posts = 0
  const version = (id: string) => ({ version_id:id,created_at:'2026-09-02T06:00:00+00:00',
    source:'automatic',is_current:true,status:'ready',summary:{},candidate:{candidate_id:id,
      buy_rule:{conditions:[{left_value:'price.close',operator:'gt',right_constant:10}]},
      sell_rule:{conditions:[{left_value:'price.close',operator:'lt',right_constant:9}]},config:{stop_loss_pct:.12}} })
  await page.route('**/api/v1/**', async route => {
    if (route.request().method()==='POST') posts++
    const path = new URL(route.request().url()).pathname
    let data: unknown = {}
    if (path.endsWith('/detail')) data={name:'测试股',objectives:{annual_return:[version('a')],sharpe:[version('s')]}}
    else if (path.endsWith('/curve')) data={summary:{created_return:.5,post_launch_return:.25,
      inherited_position:true,baseline_date:'2026-09-01',as_of_date:'2026-09-03',holding:[],has_post_launch_data:true},
      backtest:{start_date:'2026-09-01',end_date:'2026-09-03',bars:3,performance:{total_return:.5},
        equity_curve:[{date:'2026-09-01',total:1000,return_pct:0,trade_directions:['BUY']},{date:'2026-09-03',total:1500,return_pct:.5}],trades:[]} }
    else if (path.endsWith('/trades')) data={items:[{date:'2026-09-01',direction:'BUY',size:100,price:10}],total:101}
    else if (path.includes('/metadata')) data={indicators:[]}
    await route.fulfill({json:{data}})
  })
  await page.goto('/strategy-pool/SH/600000?objective=annual_return&list=page%3D2%26search%3D600000')
  await expect(page.getByTestId('pool-detail-page')).toBeVisible()
  await expect(page.getByRole('heading',{name:'交易规则'})).toBeVisible()
  await expect(page.locator('.metrics b').filter({hasText:'50.00%'})).toBeVisible()
  await expect(page.locator('svg')).toBeVisible()
  await expect(page.getByText('上线 2026-09-02',{exact:true})).toBeVisible()
  await expect(page.locator('svg line[stroke-dasharray]')).toHaveAttribute('x1','400')
  await expect(page.locator('textarea')).toHaveCount(0)
  await page.getByTestId('detail-sharpe').click()
  await page.getByRole('button',{name:'下一页',exact:true}).click()
  await expect(page.getByText('51–100 / 101 笔')).toBeVisible()
  expect(posts).toBe(0)
  await page.getByRole('link',{name:'← 返回全市场标的状态'}).click()
  await expect(page).toHaveURL(/page=2.*search=600000/)
})
