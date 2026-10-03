import { expect, test } from '@playwright/test'

test('individual research exposes expanded defaults and successful adaptive stopping', async ({ page }) => {
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname
    let data: unknown = []
    if (path.endsWith('/metadata')) data = { indicators: [], universes: [] }
    else if (path.endsWith('/jobs/active')) data = [{ job_id: 'adaptive', description: '个股研究' }]
    else if (path.endsWith('/individual-research/jobs/adaptive')) data = {
      job_id: 'adaptive', status: 'completed', progress: 1,
      result: {
        market: 'SH', code: '600000', name: '浦发银行',
        date_range: { start: '2000-01-01', end: '2026-09-30', days: 6000 },
        segmentation: {}, split_metadata: { train: {}, validation: {}, holdout: {} },
        condition_candidates: { buy: [], sell: [] },
        rule_candidates: { results_by_return: [], results_by_sharpe: [], evaluated_pairs: 225 },
        results_by_return: [], results_by_sharpe: [], candidate_count: 0,
        risk_trials_per_rule_set: 3000,
        risk_search_runs: [{ rule_id: 'rules-demo', search_metadata: {
          evaluated_trials: 1504, max_trials: 3000, stop_reason: 'converged',
          fidelity_evaluations: { medium: 120, full: 40 },
        } }],
      },
    }
    await route.fulfill({ json: { data } })
  })
  await page.goto('/individual-research')
  await expect(page.getByTestId('individual-candidate-limit')).toHaveValue('15')
  await expect(page.getByLabel('每组规则最多尝试次数')).toBeVisible()
  const stats = page.getByTestId('individual-search-budget')
  await expect(stats).toContainText('提前收敛')
  await expect(stats).toContainText('1504')
  await expect(stats).toContainText('3000')
  await expect(stats).toContainText('完整精度 40')
})
