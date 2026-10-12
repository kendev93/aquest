import { test, expect } from '@playwright/test'

const metadata = {
  indicators: [{
    id: 'indicator_three', display_name: '指标三 · 拉升准备', minimum_bars: 34, recommended_bars: 120,
    signals: [
      { id: 'indicator_three.prepare_rally', display_name: '准备拉升', description: 'VAR1 上穿 8' },
      { id: 'indicator_three.accumulation_zone', display_name: '建仓区', description: 'VARO7 < 10' },
    ],
  }],
  combine_modes: [{ value: 'all', label: '全部满足 AND' }, { value: 'any', label: '任一满足 OR' }, { value: 'at_least', label: '至少满足 N 个' }],
  supported_markets: ['SH', 'SZ'], supported_universe: [{ value: 'all', label: '沪深全部 A 股' }],
  periods: [{ value: 'daily', label: '日线' }], data_directory_help: '本地目录',
}

test('user can configure a formula scan, view results, and export them', async ({ page }) => {
  await page.route('**/api/v1/formula-screen/metadata', (route) => route.fulfill({ json: { data: metadata } }))
  await page.route('**/api/v1/formula-screen/jobs', (route) => route.fulfill({ status: 202, json: { data: { job_id: 'e2e-job', status: 'queued' } } }))
  await page.route('**/api/v1/formula-screen/jobs/e2e-job', (route) => route.fulfill({ json: { data: { job_id: 'e2e-job', status: 'completed', progress: 1, total_candidates: 1, total_scanned: 1, total_signals: 1, errors: 0, skipped: 0, error: null } } }))
  await page.route('**/api/v1/formula-screen/jobs/e2e-job/results', (route) => route.fulfill({ json: { data: [{ market: 'SH', code: '600000', signal_date: 20260824, last_close: 12.35, matched_signals: ['indicator_three.accumulation_zone'], match_count: 1, indicator_values: { 'indicator_three.varo7': 2.2 } }], meta: { total_candidates: 1, total_scanned: 1, total_signals: 1, errors: 0, skipped: 0, failure_reasons: {}, skip_reasons: {} } } }))

  await page.goto('/aquest')
  await page.getByTestId('signal-indicator_three.prepare_rally').check()
  await page.getByTestId('signal-indicator_three.accumulation_zone').check()
  await page.getByTestId('advanced-toggle').click()
  await page.getByTestId('minimum-matches').fill('2')
  await page.getByTestId('start-scan').click()

  await expect(page.getByTestId('results-table')).toContainText('600000')
  await expect(page.getByTestId('export-json')).toBeEnabled()
  await expect(page.getByTestId('export-csv')).toBeEnabled()
})

test('user can parse a custom formula and scan with an overridden parameter', async ({ page }) => {
  await page.route('**/api/v1/formula-screen/metadata', (route) => route.fulfill({ json: { data: metadata } }))
  await page.route('**/api/v1/formula-screen/parse', (route) => route.fulfill({ json: { data: {
    parameters: [{ name: 'N', default: 5, minimum: 1, maximum: 10000, step: 1 }],
    signals: [{ id: 'custom.breakout', display_name: 'BREAKOUT', description: 'BREAKOUT 的最后一根 K 线输出不为 0' }],
    minimum_bars: 6, warnings: [],
  } } }))
  await page.route('**/api/v1/formula-screen/jobs', (route) => route.fulfill({ status: 202, json: { data: { job_id: 'custom-job', status: 'queued' } } }))
  await page.route('**/api/v1/formula-screen/jobs/custom-job', (route) => route.fulfill({ json: { data: { job_id: 'custom-job', status: 'completed', progress: 1, total_candidates: 1, total_scanned: 1, total_signals: 1, errors: 0, skipped: 0, error: null } } }))
  await page.route('**/api/v1/formula-screen/jobs/custom-job/results', (route) => route.fulfill({ json: { data: [{ market: 'SH', code: '600001', signal_date: 20260824, last_close: 9.8, matched_signals: ['custom.breakout'], match_count: 1, indicator_values: { 'custom.N': 7, 'custom.LEVEL': 9.8 } }], meta: { total_candidates: 1, total_scanned: 1, total_signals: 1, errors: 0, skipped: 0, failure_reasons: {}, skip_reasons: {} } } }))

  await page.goto('/aquest')
  await page.getByTestId('mode-custom').click()
  await page.getByTestId('custom-formula').fill('N:=5; BREAKOUT:CROSS(C,REF(C,N));')
  await page.getByTestId('parse-formula').click()
  await expect(page.getByTestId('custom-formula-meta')).toContainText('1 个参数')
  await page.getByTestId('formula-param-N').fill('7')
  await page.getByTestId('advanced-toggle').click()
  await page.getByTestId('minimum-matches').fill('1')
  await page.getByTestId('start-scan').click()

  await expect(page.getByTestId('results-table')).toContainText('600001')
})

test('user can sync latest market data into DuckDB', async ({ page }) => {
  await page.route('**/api/v1/formula-screen/metadata', (route) => route.fulfill({ json: { data: metadata } }))
  await page.route('**/api/v1/market-data/sync', (route) => route.fulfill({ status: 202, json: { data: { job_id: 'sync-job', status: 'queued' } } }))
  await page.route('**/api/v1/market-data/sync/jobs/sync-job', (route) => route.fulfill({ json: { data: { job_id: 'sync-job', status: 'completed', progress: 1, total_candidates: 2, total_scanned: 2, errors: 0, error: null, result: { total_candidates: 2, processed: 2, updated_files: 2, unchanged_files: 0, written_bars: 4, errors: 0, failure_reasons: {} } } } }))

  await page.goto('/aquest')
  await page.getByTestId('sync-market-data').click()

  await expect(page.getByTestId('screen-message')).toContainText('写入 4 根')
  await expect(page.getByTestId('sync-progress')).toContainText('已处理 2 / 2')
  await expect(page.getByTestId('sync-progress')).toContainText('剩余 0')
})
