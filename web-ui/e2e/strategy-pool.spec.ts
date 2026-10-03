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
