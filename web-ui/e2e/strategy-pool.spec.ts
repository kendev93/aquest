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
  await expect.poll(() => requests.length).toBe(1)
  expect(requests[0]).toEqual({ mode: 'signals', objective: 'annual_return', automatic: true })
  await expect(page.getByTestId('pool-refresh-signals')).toBeEnabled()
  await page.reload()
  await expect(page.getByTestId('pool-row-600000')).toBeVisible()
  await page.getByTestId('pool-objective').selectOption('sharpe')
  await expect(page.getByTestId('pool-refresh-signals')).toBeEnabled()
  expect(requests).toHaveLength(1)
  await page.getByTestId('pool-refresh-signals').click()
  await expect.poll(() => requests.length).toBe(2)
  expect(requests[1]).toEqual({ mode: 'signals', objective: 'sharpe' })
})
