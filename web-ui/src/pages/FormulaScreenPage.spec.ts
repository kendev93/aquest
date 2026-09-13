import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { FormulaScreenMetadata, LocalMarketChart } from '../types'
import FormulaScreenPage from './FormulaScreenPage.vue'

import * as api from '../api/formulaScreen'
import * as marketApi from '../api/marketData'
import * as jobsApi from '../api/jobs'

vi.mock('../api/formulaScreen', async () => {
  const actual = await vi.importActual<typeof import('../api/formulaScreen')>('../api/formulaScreen')
  return { ...actual, fetchMetadata: vi.fn(), parseFormula: vi.fn(), createJob: vi.fn(), getJob: vi.fn(), getResults: vi.fn(), fetchScreenHistory: vi.fn(), getScreenHistory: vi.fn(), createSyncJob: vi.fn(), getSyncJob: vi.fn() }
})

vi.mock('../api/marketData', async () => {
  const actual = await vi.importActual<typeof import('../api/marketData')>('../api/marketData')
  return { ...actual, fetchLocalChart: vi.fn() }
})

vi.mock('../api/jobs', async () => {
  const actual = await vi.importActual<typeof import('../api/jobs')>('../api/jobs')
  return { ...actual, fetchActiveJobs: vi.fn() }
})

const metadata: FormulaScreenMetadata = {
  indicators: [
    {
      id: 'indicator_three', display_name: '准备拉升', minimum_bars: 34, recommended_bars: 120,
      signals: [
        { id: 'indicator_three.prepare_rally', display_name: '准备拉升', description: 'VAR1 上穿 8' },
        { id: 'indicator_three.accumulation_zone', display_name: '建仓区', description: 'VARO7 < 10' },
      ],
      values: [
        { id: 'indicator_three.var1', display_name: 'VAR1', description: 'VAR1 的数值输出' },
        { id: 'indicator_three.var2', display_name: 'VAR2', description: 'VAR2 的数值输出' },
        { id: 'indicator_three.prepare_rally', display_name: '准备拉升', description: '准备拉升的数值输出' },
        { id: 'indicator_three.suppress_main', display_name: '压住庄家', description: '压住庄家的数值输出' },
      ],
    },
  ],
  combine_modes: [
    { value: 'all', label: '全部满足 AND' }, { value: 'any', label: '任一满足 OR' }, { value: 'at_least', label: '至少满足 N 个' },
  ],
  supported_markets: ['SH', 'SZ'],
  supported_universe: [
    { value: 'all', label: '沪深全部 A 股' },
    { value: 'sh', label: '仅上海品种' },
    { value: 'sz', label: '仅深圳品种' },
    { value: 'custom', label: '自定义股票列表' },
  ],
  periods: [{ value: 'daily', label: '日线' }],
  data_directory_help: '本地目录',
  default_vipdoc_path: '/data/vipdoc',
}

const localChart: LocalMarketChart = {
  market: 'SH',
  code: '600000',
  period: 'daily',
  total_daily_bars: 3,
  bars: 3,
  available_data_start: '2024-01-01',
  available_data_end: '2024-01-03',
  data_start: '2024-01-01',
  data_end: '2024-01-03',
  candles: [
    { date: '2024-01-01', open: 10, high: 11, low: 9, close: 10.5, volume: 1000, amount: 10000, ma: {}, rsi14: null, macd: null, macd_signal: null, macd_histogram: null },
    { date: '2024-01-02', open: 10.5, high: 11.5, low: 10, close: 11, volume: 1100, amount: 11000, ma: {}, rsi14: null, macd: null, macd_signal: null, macd_histogram: null },
    { date: '2024-01-03', open: 11, high: 12, low: 10.5, close: 11.5, volume: 1200, amount: 12000, ma: {}, rsi14: null, macd: null, macd_signal: null, macd_histogram: null },
  ],
}

describe('FormulaScreenPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    vi.mocked(api.fetchMetadata).mockResolvedValue(metadata)
    vi.mocked(api.parseFormula).mockResolvedValue({
      parameters: [{ name: 'N', default: 5, minimum: 1, maximum: 10000, step: 1 }],
      signals: [{ id: 'custom.breakout', display_name: 'BREAKOUT', description: 'BREAKOUT 的最后一根 K 线输出不为 0' }],
      values: [
        { id: 'custom.breakout', display_name: 'BREAKOUT', description: 'BREAKOUT 的数值输出' },
        { id: 'custom.baseline', display_name: 'BASELINE', description: 'BASELINE 的数值输出' },
      ],
      minimum_bars: 6,
      warnings: [],
    })
    vi.mocked(api.createJob).mockResolvedValue({ job_id: 'job-1', status: 'queued' })
    vi.mocked(api.createSyncJob).mockResolvedValue({ job_id: 'sync-1', status: 'queued' })
    vi.mocked(api.getSyncJob).mockResolvedValue({
      job_id: 'sync-1', status: 'completed', progress: 1, total_candidates: 2, total_scanned: 2,
      errors: 0, error: null, result: { total_candidates: 2, processed: 2, updated_files: 2, unchanged_files: 0, written_bars: 4, errors: 0, failure_reasons: {} },
    })
    vi.mocked(api.getJob).mockResolvedValue({
      job_id: 'job-1', status: 'completed', progress: 1, total_candidates: 2, total_scanned: 2,
      total_signals: 2, errors: 0, skipped: 0, error: null,
    })
    vi.mocked(api.getResults).mockResolvedValue({
      results: [{ market: 'SH', code: '600000', name: '浦发银行', signal_date: 20260824, last_close: 12.35, matched_signals: ['indicator_three.accumulation_zone'], match_count: 1, indicator_values: { 'indicator_three.varo7': 2.2, 'indicator_three.varo6': null } }],
      meta: { total_candidates: 2, total_scanned: 2, total_signals: 1, errors: 0, skipped: 0, failure_reasons: {}, skip_reasons: {} },
    })
    vi.mocked(api.fetchScreenHistory).mockResolvedValue([
      { history_id: 'history-1', created_at: '2026-09-09T10:00:00', total_candidates: 2, total_scanned: 2, total_signals: 1, errors: 0, skipped: 0, result_count: 1 },
    ])
    vi.mocked(api.getScreenHistory).mockResolvedValue({
      history_id: 'history-1',
      created_at: '2026-09-09T10:00:00',
      meta: { total_candidates: 2, total_scanned: 2, total_signals: 1, errors: 0, skipped: 0, failure_reasons: {}, skip_reasons: {} },
      results: [],
    })
    vi.mocked(marketApi.fetchLocalChart).mockResolvedValue(localChart)
    vi.mocked(jobsApi.fetchActiveJobs).mockResolvedValue([])
  })

  it('recovers and resumes a running screening task after the page is refreshed', async () => {
    vi.mocked(jobsApi.fetchActiveJobs).mockResolvedValueOnce([{
      job_id: 'screen-refresh', description: '公式选股', status: 'running', progress: 0.42,
      total_candidates: 2, total_scanned: 1, total_signals: 1, errors: 0, skipped: 0,
      error: null, result: null,
    }])
    vi.mocked(api.getJob).mockResolvedValueOnce({
      job_id: 'screen-refresh', status: 'completed', progress: 1, total_candidates: 2, total_scanned: 2,
      total_signals: 2, errors: 0, skipped: 0, error: null,
    })

    const wrapper = mount(FormulaScreenPage)
    await flushPromises()

    expect(jobsApi.fetchActiveJobs).toHaveBeenCalled()
    expect(api.getJob).toHaveBeenCalledWith('screen-refresh')
    expect(wrapper.get('[data-testid="results-table"]').text()).toContain('600000')
  })

  it('renders signal choices and validates an empty selection', async () => {
    const wrapper = mount(FormulaScreenPage)
    await flushPromises()

    expect(wrapper.get('[data-testid="formula-screen-page"]')).toBeTruthy()
    expect(wrapper.get('img.brand-mark').attributes()).toMatchObject({
      src: '/indicator-lab-mark.png',
      alt: '',
      'aria-hidden': 'true',
    })
    expect(wrapper.get('[data-testid="signal-indicator_three.prepare_rally"]')).toBeTruthy()
    for (const checkbox of wrapper.findAll('input[type="checkbox"]')) {
      if ((checkbox.element as HTMLInputElement).checked) await checkbox.setValue(false)
    }
    await wrapper.get('[data-testid="screen-config"]').trigger('submit')
    expect(wrapper.get('[data-testid="signals-error"]').text()).toContain('至少选择')
    expect(api.createJob).not.toHaveBeenCalled()
  })

  it('shows the condition AND/OR selector and defaults to all conditions', async () => {
    const wrapper = mount(FormulaScreenPage)
    await flushPromises()

    await wrapper.get('[data-testid="rule-mode-conditions"]').trigger('click')
    await flushPromises()

    const combineMode = wrapper.get('[data-testid="condition-combine-mode"]')
    expect((combineMode.element as HTMLSelectElement).value).toBe('all')

    await combineMode.setValue('any')
    expect((combineMode.element as HTMLSelectElement).value).toBe('any')
  })

  it('lists history and expands a result with local K-line and primary indicators', async () => {
    const wrapper = mount(FormulaScreenPage)
    await flushPromises()

    expect(wrapper.get('[data-testid="screen-history"]').text()).toContain('1 条结果')
    expect(wrapper.get('[data-testid="screen-history-item-history-1"]')).toBeTruthy()

    await wrapper.get('[data-testid="rule-mode-conditions"]').trigger('click')
    await wrapper.get('[data-testid="screen-config"]').trigger('submit')
    await flushPromises()
    const resultRow = wrapper.get('[data-testid="results-table"] .screen-result-row')
    await resultRow.trigger('click')
    await flushPromises()

    expect(marketApi.fetchLocalChart).toHaveBeenCalledWith(expect.objectContaining({ market: 'SH', code: '600000', period: 'daily' }))
    expect(wrapper.get('[data-testid="result-detail-SH-600000"]').text()).toContain('主要指标')
    expect(wrapper.get('[data-testid="result-indicator-SH-600000-0"]')).toBeTruthy()
  })

  it('distinguishes metadata loading failures from scan failures', async () => {
    vi.mocked(api.fetchMetadata).mockRejectedValueOnce(new Error('metadata network down'))
    const wrapper = mount(FormulaScreenPage)
    await flushPromises()

    expect(wrapper.get('[data-testid="screen-message"]').text()).toContain('公式元数据加载失败')
    expect(wrapper.get('[data-testid="screen-message"]').text()).toContain('metadata network down')
  })

  it('keeps advanced settings collapsed until requested', async () => {
    const wrapper = mount(FormulaScreenPage)
    await flushPromises()

    expect(wrapper.find('[data-testid="advanced-settings"]').exists()).toBe(false)
    await wrapper.get('[data-testid="advanced-toggle"]').trigger('click')
    expect(wrapper.get('[data-testid="advanced-settings"]').text()).toContain('市场范围')
  })

  it('builds and submits a numeric indicator condition', async () => {
    const wrapper = mount(FormulaScreenPage)
    await flushPromises()

    await wrapper.get('[data-testid="rule-mode-conditions"]').trigger('click')
    await wrapper.get('[data-testid="condition-left-0"]').setValue('indicator_three.var1')
    await wrapper.get('[data-testid="condition-operator-0"]').setValue('lt')
    await wrapper.get('[data-testid="condition-right-type-0"]').setValue('constant')
    await wrapper.get('[data-testid="condition-constant-0"]').setValue('10')
    vi.mocked(api.getResults).mockResolvedValueOnce({
      results: [{ market: 'SH', code: '600000', name: '浦发银行', signal_date: 20260824, last_close: 12.35, matched_signals: ['condition_1'], match_count: 1, indicator_values: { 'indicator_three.var1': 2.2 } }],
      meta: { total_candidates: 2, total_scanned: 2, total_signals: 1, errors: 0, skipped: 0, failure_reasons: {}, skip_reasons: {} },
    })
    await wrapper.get('[data-testid="screen-config"]').trigger('submit')
    await flushPromises()

    expect(api.createJob).toHaveBeenCalledWith(expect.objectContaining({
      selected_signals: [],
      combine_mode: 'all',
      minimum_matches: null,
      conditions: [{
        left_value: 'indicator_three.var1',
        operator: 'lt',
        right_value: null,
        right_constant: 10,
      }],
    }))
    expect(wrapper.get('[data-testid="results-table"]').text()).toContain('VAR1 小于 10')
  })

  it('shows readable Chinese output names in the condition selector', async () => {
    const wrapper = mount(FormulaScreenPage)
    await flushPromises()

    await wrapper.get('[data-testid="rule-mode-conditions"]').trigger('click')

    expect(wrapper.get('[data-testid="condition-left-0"]').findAll('option').map((option) => option.text())).toEqual([
      'VAR1',
      'VAR2',
      '准备拉升',
      '压住庄家',
    ])
    expect(wrapper.get('[data-testid="condition-left-0"]').text()).not.toContain('VARO7')
    expect(wrapper.get('[data-testid="condition-right-type-0"]').text()).toContain('固定数值')
    expect(wrapper.get('[data-testid="condition-right-type-0"]').text()).toContain('另一个指标')
  })

  it('keeps condition values valid when switching to a parsed custom formula', async () => {
    const wrapper = mount(FormulaScreenPage)
    await flushPromises()

    await wrapper.get('[data-testid="rule-mode-conditions"]').trigger('click')
    await wrapper.get('[data-testid="mode-custom"]').trigger('click')
    await wrapper.get('[data-testid="custom-formula"]').setValue('BREAKOUT:CROSS(C,REF(C,1));')
    await wrapper.get('[data-testid="parse-formula"]').trigger('click')
    await flushPromises()

    expect((wrapper.get('[data-testid="condition-left-0"]').element as HTMLSelectElement).value).toBe('custom.breakout')
    await wrapper.get('[data-testid="condition-right-type-0"]').setValue('value')
    expect((wrapper.get('[data-testid="condition-right-value-0"]').element as HTMLSelectElement).value).toBe('custom.baseline')
    await wrapper.get('[data-testid="screen-config"]').trigger('submit')
    await flushPromises()

    expect(api.createJob).toHaveBeenCalledWith(expect.objectContaining({
      formula_text: 'BREAKOUT:CROSS(C,REF(C,1));',
      conditions: [{
        left_value: 'custom.breakout',
        operator: 'lt',
        right_value: 'custom.baseline',
        right_constant: null,
      }],
    }))
  })

  it('adds, switches, reloads, and deletes a saved indicator combination', async () => {
    const wrapper = mount(FormulaScreenPage)
    await flushPromises()

    await wrapper.get('[data-testid="rule-mode-conditions"]').trigger('click')
    await wrapper.get('[data-testid="add-condition"]').trigger('click')
    expect(wrapper.findAll('[data-testid^="condition-row-"]')).toHaveLength(2)
    await wrapper.get('[data-testid="remove-condition-1"]').trigger('click')
    expect(wrapper.findAll('[data-testid^="condition-row-"]')).toHaveLength(1)

    await wrapper.get('[data-testid="rule-mode-signals"]').trigger('click')
    expect(wrapper.find('[data-testid="condition-builder"]').exists()).toBe(false)
    await wrapper.get('[data-testid="rule-mode-conditions"]').trigger('click')
    await wrapper.get('[data-testid="condition-set-name"]').setValue('主力进场低位')
    await wrapper.get('[data-testid="save-condition-set"]').trigger('click')

    const conditionSetSelect = wrapper.get('[data-testid="condition-set-select"]')
    const savedOption = conditionSetSelect.findAll('option').find((option) => option.text() === '主力进场低位')
    expect(savedOption).toBeDefined()
    await conditionSetSelect.setValue('')
    await conditionSetSelect.setValue(savedOption?.element.value ?? '')
    await flushPromises()
    expect(wrapper.get('[data-testid="screen-message"]').text()).toContain('已加载指标组合：主力进场低位')

    await wrapper.get('[data-testid="delete-condition-set"]').trigger('click')
    expect(conditionSetSelect.text()).not.toContain('主力进场低位')
  })

  it('saves a named indicator combination for later use', async () => {
    const wrapper = mount(FormulaScreenPage)
    await flushPromises()

    await wrapper.get('[data-testid="rule-mode-conditions"]').trigger('click')
    await wrapper.get('[data-testid="condition-set-name"]').setValue('主力进场低位')
    await wrapper.get('[data-testid="save-condition-set"]').trigger('click')

    expect(wrapper.get('[data-testid="screen-message"]').text()).toContain('已保存指标组合：主力进场低位')
    expect(wrapper.get('[data-testid="condition-set-select"]').text()).toContain('主力进场低位')
  })

  it('validates a custom universe and an invalid minimum match count', async () => {
    const wrapper = mount(FormulaScreenPage)
    await flushPromises()
    await wrapper.get('[data-testid="advanced-toggle"]').trigger('click')
    await wrapper.get('[data-testid="universe"]').setValue('custom')
    await wrapper.get('[data-testid="minimum-matches"]').setValue(3)
    await wrapper.get('[data-testid="screen-config"]').trigger('submit')

    expect(wrapper.text()).toContain('自定义范围需要提供股票列表文件')
    expect(wrapper.text()).toContain('N 必须在 1 和已选择条件数量之间')
    expect(api.createJob).not.toHaveBeenCalled()
  })

  it('submits selected signals, shows results, and keeps export controls', async () => {
    const wrapper = mount(FormulaScreenPage)
    await flushPromises()
    await wrapper.get('[data-testid="signal-indicator_three.prepare_rally"]').setValue(true)
    await wrapper.get('[data-testid="signal-indicator_three.accumulation_zone"]').setValue(true)
    await wrapper.get('[data-testid="advanced-toggle"]').trigger('click')
    await wrapper.get('[data-testid="minimum-matches"]').setValue(2)
    await wrapper.get('[data-testid="screen-config"]').trigger('submit')
    await flushPromises()

    expect(api.createJob).toHaveBeenCalledWith(expect.objectContaining({
      selected_signals: ['indicator_three.prepare_rally', 'indicator_three.accumulation_zone'],
      combine_mode: 'at_least', minimum_matches: 2,
    }))
    expect(wrapper.get('[data-testid="results-table"]').text()).toContain('600000')
    expect(wrapper.get('[data-testid="results-table"]').text()).toContain('浦发银行')
    expect(wrapper.get('[data-testid="export-json"]')).toBeTruthy()
    expect(wrapper.get('[data-testid="export-csv"]')).toBeTruthy()

    Object.defineProperty(URL, 'createObjectURL', { value: vi.fn().mockReturnValue('blob:test'), configurable: true })
    Object.defineProperty(URL, 'revokeObjectURL', { value: vi.fn(), configurable: true })
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined)
    await wrapper.get('[data-testid="export-json"]').trigger('click')
    await wrapper.get('[data-testid="export-csv"]').trigger('click')
    expect(clickSpy).toHaveBeenCalledTimes(2)
    clickSpy.mockRestore()
  })

  it('submits the selected instrument and board scope', async () => {
    const wrapper = mount(FormulaScreenPage)
    await flushPromises()
    await wrapper.get('[data-testid="advanced-toggle"]').trigger('click')
    await wrapper.get('[data-testid="screen-scope-type-stock"]').setValue(true)
    await wrapper.get('[data-testid="screen-scope-board-main"]').setValue(true)
    await wrapper.get('[data-testid="screen-config"]').trigger('submit')
    await flushPromises()

    expect(api.createJob).toHaveBeenCalledWith(expect.objectContaining({
      instrument_types: ['stock'],
      boards: ['main'],
    }))
  })

  it('shows a user-facing error when the job request fails', async () => {
    vi.mocked(api.createJob).mockRejectedValueOnce(new Error('network down'))
    const wrapper = mount(FormulaScreenPage)
    await flushPromises()
    await wrapper.get('[data-testid="signal-indicator_three.prepare_rally"]').setValue(true)
    await wrapper.get('[data-testid="advanced-toggle"]').trigger('click')
    await wrapper.get('[data-testid="minimum-matches"]').setValue(1)
    await wrapper.get('[data-testid="screen-config"]').trigger('submit')
    await flushPromises()

    expect(wrapper.get('[data-testid="screen-message"]').text()).toContain('扫描任务提交失败')
    expect(wrapper.get('[data-testid="screen-message"]').text()).toContain('network down')
  })

  it('parses a custom formula, exposes parameters, and submits the override', async () => {
    const wrapper = mount(FormulaScreenPage)
    await flushPromises()
    await wrapper.get('[data-testid="mode-custom"]').trigger('click')
    await wrapper.get('[data-testid="custom-formula"]').setValue('N:=5; BREAKOUT:CROSS(C,REF(C,N));')
    await wrapper.get('[data-testid="parse-formula"]').trigger('click')
    await flushPromises()

    expect(wrapper.get('[data-testid="custom-formula-meta"]').text()).toContain('1 个参数')
    expect(wrapper.get('[data-testid="custom-signal-custom.breakout"]')).toBeTruthy()
    await wrapper.get('[data-testid="formula-param-N"]').setValue(7)
    await wrapper.get('[data-testid="advanced-toggle"]').trigger('click')
    await wrapper.get('[data-testid="combine-mode"]').setValue('any')
    await wrapper.get('[data-testid="screen-config"]').trigger('submit')
    await flushPromises()

    expect(api.parseFormula).toHaveBeenCalledWith('N:=5; BREAKOUT:CROSS(C,REF(C,N));')
    expect(api.createJob).toHaveBeenCalledWith(expect.objectContaining({
      formula_text: 'N:=5; BREAKOUT:CROSS(C,REF(C,N));',
      formula_parameters: { N: 7 },
      selected_signals: ['custom.breakout'],
    }))
  })

  it('preserves an edited custom parameter when the formula is parsed again', async () => {
    const wrapper = mount(FormulaScreenPage)
    await flushPromises()
    await wrapper.get('[data-testid="mode-custom"]').trigger('click')
    await wrapper.get('[data-testid="custom-formula"]').setValue('N:=5; BREAKOUT:CROSS(C,REF(C,N));')
    await wrapper.get('[data-testid="parse-formula"]').trigger('click')
    await flushPromises()
    await wrapper.get('[data-testid="formula-param-N"]').setValue(7)
    await wrapper.get('[data-testid="parse-formula"]').trigger('click')
    await flushPromises()

    expect((wrapper.get('[data-testid="formula-param-N"]').element as HTMLInputElement).value).toBe('7')
  })

  it('requires reparsing after a custom formula is edited', async () => {
    const wrapper = mount(FormulaScreenPage)
    await flushPromises()
    await wrapper.get('[data-testid="mode-custom"]').trigger('click')
    await wrapper.get('[data-testid="custom-formula"]').setValue('N:=5; BREAKOUT:CROSS(C,REF(C,N));')
    await wrapper.get('[data-testid="parse-formula"]').trigger('click')
    await flushPromises()

    expect(wrapper.get('[data-testid="custom-formula-meta"]').exists()).toBe(true)
    await wrapper.get('[data-testid="custom-formula"]').setValue('N:=6; BREAKOUT:CROSS(C,REF(C,N));')
    await flushPromises()

    expect(wrapper.find('[data-testid="custom-formula-meta"]').exists()).toBe(false)
    expect((wrapper.get('[data-testid="start-scan"]').element as HTMLButtonElement).disabled).toBe(true)
  })

  it('syncs latest market data with one click and shows the write summary', async () => {
    const wrapper = mount(FormulaScreenPage)
    await flushPromises()

    await wrapper.get('[data-testid="sync-market-data"]').trigger('click')
    await flushPromises()

    expect(api.createSyncJob).toHaveBeenCalledWith({ provider: 'eastmoney', universe: 'all' })
    expect(wrapper.get('[data-testid="screen-message"]').text()).toContain('写入 4 根')
    expect(wrapper.get('[data-testid="sync-progress"]').text()).toContain('已处理 2 / 2')
    expect(wrapper.get('[data-testid="sync-progress"]').text()).toContain('剩余 0')
    expect(wrapper.get('[data-testid="sync-progress"]').text()).toContain('更新 2 个文件')
  })

  it('uses the selected scope for online synchronization', async () => {
    const wrapper = mount(FormulaScreenPage)
    await flushPromises()
    await wrapper.get('[data-testid="advanced-toggle"]').trigger('click')
    await wrapper.get('[data-testid="universe"]').setValue('sh')
    await wrapper.get('[data-testid="screen-scope-type-stock"]').setValue(true)
    await wrapper.get('[data-testid="screen-scope-board-main"]').setValue(true)
    await wrapper.get('[data-testid="sync-market-data"]').trigger('click')
    await flushPromises()

    expect(api.createSyncJob).toHaveBeenCalledWith({
      provider: 'eastmoney',
      universe: 'sh',
      instrument_types: ['stock'],
      boards: ['main'],
    })
  })

  it('surfaces parser, scan-job, and sync failures', async () => {
    const wrapper = mount(FormulaScreenPage)
    await flushPromises()

    await wrapper.get('[data-testid="mode-custom"]').trigger('click')
    await wrapper.get('[data-testid="parse-formula"]').trigger('click')
    expect(wrapper.text()).toContain('请输入通达信公式后再解析')

    vi.mocked(api.parseFormula).mockRejectedValueOnce(new Error('unsupported'))
    await wrapper.get('[data-testid="custom-formula"]').setValue('BAD:UNKNOWN(C);')
    await wrapper.get('[data-testid="parse-formula"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('公式解析失败，请检查语法和函数是否受支持')

    vi.mocked(api.parseFormula).mockRejectedValueOnce(new api.FormulaScreenApiError('公式被拒绝', 422))
    await wrapper.get('[data-testid="custom-formula"]').setValue('BAD:UNKNOWN(D);')
    await wrapper.get('[data-testid="parse-formula"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('公式被拒绝')

    vi.mocked(api.getJob).mockResolvedValueOnce({
      job_id: 'job-1', status: 'failed', progress: 1, total_candidates: 2, total_scanned: 0,
      total_signals: 0, errors: 1, skipped: 0, error: 'DuckDB 读取失败',
    })
    await wrapper.get('[data-testid="mode-preset"]').trigger('click')
    await wrapper.get('[data-testid="signal-indicator_three.prepare_rally"]').setValue(true)
    await wrapper.get('[data-testid="advanced-toggle"]').trigger('click')
    await wrapper.get('[data-testid="minimum-matches"]').setValue(1)
    await wrapper.get('[data-testid="screen-config"]').trigger('submit')
    await flushPromises()
    expect(wrapper.get('[data-testid="screen-message"]').text()).toContain('DuckDB 读取失败')

    vi.mocked(api.createSyncJob).mockRejectedValueOnce(new api.FormulaScreenApiError('行情服务拒绝', 503))
    await wrapper.get('[data-testid="sync-market-data"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-testid="screen-message"]').text()).toContain('行情服务拒绝')

    vi.mocked(api.createSyncJob).mockRejectedValueOnce(new Error('sync down'))
    await wrapper.get('[data-testid="sync-market-data"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-testid="screen-message"]').text()).toContain('行情同步失败，请检查网络')
  })
})
