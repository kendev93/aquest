import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { FormulaScreenMetadata, LocalMarketChart, ScreenPresetPayload } from '../types'
import FormulaScreenPage from './FormulaScreenPage.vue'

import * as api from '../api/formulaScreen'
import * as marketApi from '../api/marketData'
import * as jobsApi from '../api/jobs'
import * as presetApi from '../api/screenPresets'
import * as groupApi from '../api/stockGroups'

vi.mock('../api/stockGroups', async () => {
  const actual = await vi.importActual<typeof import('../api/stockGroups')>('../api/stockGroups')
  return {
    ...actual,
    fetchStockGroups: vi.fn(),
    fetchStockGroupsSummary: vi.fn(),
    createStockGroup: vi.fn(),
    getStockGroup: vi.fn(),
    fetchStockGroupPerformance: vi.fn(),
    removeStockGroupMembers: vi.fn(),
    renameStockGroup: vi.fn(),
    deleteStockGroup: vi.fn(),
  }
})

const CRITERIA = {
  group_id: 'group-1',
  summary: '信号：建仓启动、底部区域，至少满足 2 个，范围 全部 A 股',
  payload: { selected_signals: ['indicator_three.prepare_rally'] },
  created_at: '2026-09-22T10:00:00',
  updated_at: '2026-09-22T10:00:00',
}

vi.mock('../api/screenPresets', async () => {
  const actual = await vi.importActual<typeof import('../api/screenPresets')>('../api/screenPresets')
  return {
    ...actual,
    fetchScreenPresets: vi.fn(),
    getScreenPreset: vi.fn(),
    createScreenPreset: vi.fn(),
    updateScreenPreset: vi.fn(),
    deleteScreenPreset: vi.fn(),
  }
})

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

const presetPayload: ScreenPresetPayload = {
  mode: 'preset',
  rule_mode: 'conditions',
  selected_signals: [],
  conditions: [
    { left_value: 'indicator_three.var1', operator: 'lt', right_value: null, right_constant: 5 },
  ],
  combine_mode: 'all',
  minimum_matches: null,
  universe: 'sh',
  universe_file: '',
  instrument_types: ['stock'],
  boards: ['main'],
  workers: 8,
  period: 'daily',
  formula_text: '',
  formula_parameters: {},
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
    sessionStorage.clear()
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
    vi.mocked(presetApi.fetchScreenPresets).mockResolvedValue([])
    vi.mocked(presetApi.createScreenPreset).mockResolvedValue({
      preset_id: 'preset-1', name: 'x', note: '',
      created_at: '2026-09-21T10:00:00', updated_at: '2026-09-21T10:00:00',
      payload: presetPayload, notes: [],
    })
    vi.mocked(presetApi.updateScreenPreset).mockResolvedValue({
      preset_id: 'preset-1', name: 'x', note: '',
      created_at: '2026-09-21T10:00:00', updated_at: '2026-09-21T10:00:00',
      payload: presetPayload, notes: [],
    })
    vi.mocked(presetApi.deleteScreenPreset).mockResolvedValue({ deleted: true })
    vi.mocked(presetApi.getScreenPreset).mockResolvedValue({
      preset_id: 'preset-1', name: 'x', note: '',
      created_at: '2026-09-21T10:00:00', updated_at: '2026-09-21T10:00:00',
      payload: presetPayload,
    })
    vi.mocked(groupApi.fetchStockGroups).mockResolvedValue([])
    vi.mocked(groupApi.fetchStockGroupsSummary).mockResolvedValue({
      as_of: '2026-09-22',
      windows: [20, 60],
      labels: { '20': '近 1 月', '60': '近 3 月' },
      groups: [],
    })
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

  it('adds and removes conditions', async () => {
    const wrapper = mount(FormulaScreenPage)
    await flushPromises()

    await wrapper.get('[data-testid="rule-mode-conditions"]').trigger('click')
    await wrapper.get('[data-testid="add-condition"]').trigger('click')
    expect(wrapper.findAll('[data-testid^="condition-row-"]')).toHaveLength(2)
    await wrapper.get('[data-testid="remove-condition-1"]').trigger('click')
    expect(wrapper.findAll('[data-testid^="condition-row-"]')).toHaveLength(1)
  })

  it('saves the whole configuration as a named preset', async () => {
    vi.mocked(presetApi.createScreenPreset).mockResolvedValue({
      preset_id: 'preset-1', name: '主力进场低位', note: '',
      created_at: '2026-09-21T10:00:00', updated_at: '2026-09-21T10:00:00',
      payload: presetPayload, notes: [],
    })
    vi.mocked(presetApi.fetchScreenPresets).mockResolvedValue([
      { preset_id: 'preset-1', name: '主力进场低位', note: '', created_at: '2026-09-21T10:00:00', updated_at: '2026-09-21T10:00:00' },
    ])

    const wrapper = mount(FormulaScreenPage)
    await flushPromises()

    await wrapper.get('[data-testid="preset-name"]').setValue('主力进场低位')
    await wrapper.get('[data-testid="save-preset"]').trigger('click')
    await flushPromises()

    expect(presetApi.createScreenPreset).toHaveBeenCalledWith(
      expect.objectContaining({ name: '主力进场低位' }),
    )
    expect(wrapper.get('[data-testid="screen-message"]').text()).toContain('已保存方案')
    expect(wrapper.get('[data-testid="preset-select"]').text()).toContain('主力进场低位')
  })

  it('loads a preset into the form without running a scan', async () => {
    vi.mocked(presetApi.fetchScreenPresets).mockResolvedValue([
      { preset_id: 'preset-1', name: '主板低位', note: '', created_at: '2026-09-21T10:00:00', updated_at: '2026-09-21T10:00:00' },
    ])
    vi.mocked(presetApi.getScreenPreset).mockResolvedValue({
      preset_id: 'preset-1', name: '主板低位', note: '',
      created_at: '2026-09-21T10:00:00', updated_at: '2026-09-21T10:00:00',
      payload: presetPayload,
    })

    const wrapper = mount(FormulaScreenPage)
    await flushPromises()

    await wrapper.get('[data-testid="preset-select"]').setValue('preset-1')
    await wrapper.get('[data-testid="load-preset"]').trigger('click')
    await flushPromises()

    expect(wrapper.get('[data-testid="screen-message"]').text()).toContain('已载入方案「主板低位」')
    expect(wrapper.get('[data-testid="screen-message"]').text()).toContain('需要重新扫描')
    // 加载只还原设置，不自动跑扫描。
    expect(api.createJob).not.toHaveBeenCalled()
  })

  it('renames and deletes a preset', async () => {
    vi.mocked(presetApi.fetchScreenPresets).mockResolvedValue([
      { preset_id: 'preset-1', name: '甲', note: '', created_at: '2026-09-21T10:00:00', updated_at: '2026-09-21T10:00:00' },
    ])
    vi.mocked(presetApi.updateScreenPreset).mockResolvedValue({
      preset_id: 'preset-1', name: '乙', note: '',
      created_at: '2026-09-21T10:00:00', updated_at: '2026-09-21T10:00:00',
      payload: presetPayload,
    })
    vi.mocked(presetApi.deleteScreenPreset).mockResolvedValue({ deleted: true })

    const wrapper = mount(FormulaScreenPage)
    await flushPromises()

    await wrapper.get('[data-testid="preset-select"]').setValue('preset-1')
    await wrapper.get('[data-testid="preset-name"]').setValue('乙')
    await wrapper.get('[data-testid="rename-preset"]').trigger('click')
    await flushPromises()
    expect(presetApi.updateScreenPreset).toHaveBeenCalledWith('preset-1', { name: '乙' })
    expect(wrapper.get('[data-testid="screen-message"]').text()).toContain('已重命名')

    await wrapper.get('[data-testid="delete-preset"]').trigger('click')
    await flushPromises()
    expect(presetApi.deleteScreenPreset).toHaveBeenCalledWith('preset-1')
    expect(wrapper.get('[data-testid="screen-message"]').text()).toContain('已删除方案')
  })

  it('tells the user when a preset references indicators that no longer exist', async () => {
    vi.mocked(presetApi.fetchScreenPresets).mockResolvedValue([
      { preset_id: 'preset-1', name: '旧方案', note: '', created_at: '2026-09-21T10:00:00', updated_at: '2026-09-21T10:00:00' },
    ])
    vi.mocked(presetApi.getScreenPreset).mockResolvedValue({
      preset_id: 'preset-1', name: '旧方案', note: '',
      created_at: '2026-09-21T10:00:00', updated_at: '2026-09-21T10:00:00',
      payload: {
        ...presetPayload,
        conditions: [{ left_value: 'indicator_three.gone', operator: 'lt', right_value: null, right_constant: 5 }],
      },
    })

    const wrapper = mount(FormulaScreenPage)
    await flushPromises()

    await wrapper.get('[data-testid="preset-select"]').setValue('preset-1')
    await wrapper.get('[data-testid="load-preset"]').trigger('click')
    await flushPromises()

    expect(wrapper.get('[data-testid="screen-message"]').text()).toContain('1 条条件引用了当前不存在的指标')
  })

  it('offers to import the legacy browser-local condition sets', async () => {
    localStorage.setItem('indicator-lab.condition-sets.v1', JSON.stringify([{
      id: 'legacy-1', name: '旧组合', ruleMode: 'conditions',
      selectedSignals: [], conditions: [{ left_value: 'indicator_three.var1', operator: 'lt', right_value: null, right_constant: 5 }],
      combineMode: 'all', minimumMatches: null, mode: 'preset', formulaText: '',
      formulaParameters: {},
    }]))
    vi.mocked(presetApi.createScreenPreset).mockResolvedValue({
      preset_id: 'preset-9', name: '旧组合', note: '',
      created_at: '2026-09-21T10:00:00', updated_at: '2026-09-21T10:00:00',
      payload: presetPayload, notes: [],
    })

    const wrapper = mount(FormulaScreenPage)
    await flushPromises()

    expect(wrapper.get('[data-testid="preset-import"]').text()).toContain('1 个')
    await wrapper.get('[data-testid="import-legacy-sets"]').trigger('click')
    await flushPromises()

    expect(presetApi.createScreenPreset).toHaveBeenCalledWith(
      expect.objectContaining({ name: '旧组合' }),
    )
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

    expect(wrapper.find('[data-testid="custom-formula-meta"]').exists()).toBe(true)
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

    expect(api.createSyncJob).toHaveBeenCalledWith({ universe: 'all' })
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

  it('picks up a condition-research handoff and clears it so a refresh cannot reapply it', async () => {
    sessionStorage.setItem(
      'indicator-lab.condition-research.to-screen',
      JSON.stringify({
        signals: ['indicator_three.accumulation_zone'],
        conditions: [],
        required_conditions: [],
        combine_mode: 'all',
        minimum_matches: null,
        universe: 'sh',
        boards: ['main'],
        instrument_types: ['stock'],
      }),
    )

    const wrapper = mount(FormulaScreenPage)
    await flushPromises()

    expect(wrapper.get('[data-testid="screen-message"]').text())
      .toContain('已带入买卖条件研究的买入条件')
    // 卖出条件没跟过来这件事必须讲，不能让人以为整套规则都搬过来了。
    expect(wrapper.get('[data-testid="screen-message"]').text())
      .toContain('卖出条件没有带过来')
    expect(wrapper.get('[data-testid="signal-indicator_three.accumulation_zone"]').element).toBeTruthy()
    expect(sessionStorage.getItem('indicator-lab.condition-research.to-screen')).toBeNull()
  })

  it('keeps picked results as a group and reports how that group did afterwards', async () => {
    const wrapper = mount(FormulaScreenPage)
    await flushPromises()
    await wrapper.get('[data-testid="screen-config"]').trigger('submit')
    await flushPromises()

    // 没勾选时「保存勾选的」必须是禁用的，否则会存出一组空的东西。
    expect(wrapper.get('[data-testid="save-picked-group"]').attributes('disabled')).toBeDefined()

    await wrapper.get('[data-testid="result-pick-SH-600000"]').setValue(true)
    await wrapper.get('[data-testid="group-name"]').setValue('9月候选')
    vi.mocked(groupApi.createStockGroup).mockResolvedValue({
      group_id: 'group-1', name: '9月候选', note: '', source: 'screen', source_label: '选股结果',
      source_history_id: null, created_at: '2026-09-22T10:00:00', updated_at: '2026-09-22T10:00:00',
      item_count: 1, items: [{ market: 'SH', code: '600000', name: '浦发银行' }],
      criteria: CRITERIA,
    })
    vi.mocked(groupApi.fetchStockGroups).mockResolvedValue([{
      group_id: 'group-1', name: '9月候选', note: '', source: 'screen', source_label: '选股结果',
      source_history_id: null, created_at: '2026-09-22T10:00:00', updated_at: '2026-09-22T10:00:00',
      item_count: 1, criteria: CRITERIA,
    }])
    // 组保存完会自动展开并读表现，所以这两个 mock 要在点保存之前就备好。
    vi.mocked(groupApi.getStockGroup).mockResolvedValue({
      group_id: 'group-1', name: '9月候选', note: '', source: 'screen', source_label: '选股结果',
      source_history_id: null, created_at: '2026-09-22T10:00:00', updated_at: '2026-09-22T10:00:00',
      item_count: 2, criteria: CRITERIA,
      items: [
        { market: 'SH', code: '600000', name: '浦发银行' },
        { market: 'SZ', code: '000001', name: '平安银行' },
      ],
    })
    // 本组 +25%、全市场 -5%、超额 +30 个百分点。
    vi.mocked(groupApi.fetchStockGroupPerformance).mockResolvedValue({
      group_id: 'group-1',
      start_date: '2026-09-01',
      end_date: '2026-09-22',
      trading_days: 15,
      group_curve: {
        label: '本组等权买入持有', instrument_count: 1, dates: [20260901, 20260922],
        totals: [1, 1.25], performance: { total_return: 0.25 },
      },
      market_curve: {
        label: '全市场等权买入持有', instrument_count: 5300, dates: [20260901, 20260922],
        totals: [1, 0.95], performance: { total_return: -0.05 },
      },
      excess_return: 0.3,
      spread: {
        counted: 2, advanced: 1, declined: 1, median_return: 0.1,
        best: { market: 'SH', code: '600000', name: '浦发银行', return: 0.25 },
        worst: { market: 'SZ', code: '000001', name: '平安银行', return: -0.05 },
      },
      excluded: [{ market: 'SZ', code: '000002', name: '万科A' }],
      stalled: [],
      truncated: false,
      notes: ['1 只标的在起点当天没有收盘价（新股或停牌），未计入曲线'],
    })
    await wrapper.get('[data-testid="save-picked-group"]').trigger('click')
    await flushPromises()

    expect(groupApi.createStockGroup).toHaveBeenCalledWith(expect.objectContaining({
      name: '9月候选',
      items: [{ market: 'SH', code: '600000', name: '浦发银行' }],
      source: 'screen',
    }))
    // 筛选条件必须跟着组一起存：扫描历史只留 20 条，等历史被淘汰时只剩这句人话
    // 能回答「这批票是按什么选出来的」。
    const saved = vi.mocked(groupApi.createStockGroup).mock.calls[0][0]
    expect(saved.criteria?.summary).toContain('信号：')
    expect(saved.criteria?.payload).toBeTruthy()
    expect(wrapper.get('[data-testid="group-item-group-1"]')).toBeTruthy()

    // 保存完这一组已经是展开的（再点一次是收起），表现应当已经读到了。
    expect(wrapper.get('[data-testid="group-return"]').text()).toBe('+25.00%')
    expect(wrapper.get('[data-testid="market-return"]').text()).toBe('-5.00%')
    expect(wrapper.get('[data-testid="excess-return"]').text()).toContain('30.00 个百分点')
    // 未计入的标的必须点名，否则「这一组算了几只」说不清。
    expect(wrapper.get('[data-testid="group-performance"]').text()).toContain('000002')
    expect(wrapper.get('[data-testid="group-performance"]').text()).toContain('没有收盘价')
    expect(wrapper.find('[data-testid="group-chart"]').exists()).toBe(true)
  })

  it('renames a group in place', async () => {
    vi.mocked(groupApi.fetchStockGroups).mockResolvedValue([{
      group_id: 'group-1', name: '旧名字', note: '', source: 'screen', source_label: '选股结果',
      source_history_id: null, created_at: '2026-09-22T10:00:00', updated_at: '2026-09-22T10:00:00',
      item_count: 3, criteria: CRITERIA,
    }])
    vi.mocked(groupApi.renameStockGroup).mockResolvedValue({
      group_id: 'group-1', name: '新名字', note: '', source: 'screen', source_label: '选股结果',
      source_history_id: null, created_at: '2026-09-22T10:00:00', updated_at: '2026-09-22T10:00:00',
      item_count: 3, items: [], criteria: CRITERIA,
    })
    const wrapper = mount(FormulaScreenPage)
    await flushPromises()

    const prompt = vi.spyOn(window, 'prompt').mockReturnValue('新名字')
    await wrapper.get('[data-testid="group-rename-group-1"]').trigger('click')
    await flushPromises()

    expect(groupApi.renameStockGroup).toHaveBeenCalledWith('group-1', '新名字')
    // 改名后要重新拉一次列表，否则界面上还是旧名字。
    expect(groupApi.fetchStockGroups).toHaveBeenCalledTimes(2)
    prompt.mockRestore()
  })

  it('does not call rename when the prompt is dismissed', async () => {
    vi.mocked(groupApi.fetchStockGroups).mockResolvedValue([{
      group_id: 'group-1', name: '旧名字', note: '', source: 'screen', source_label: '选股结果',
      source_history_id: null, created_at: '2026-09-22T10:00:00', updated_at: '2026-09-22T10:00:00',
      item_count: 3, criteria: CRITERIA,
    }])
    const wrapper = mount(FormulaScreenPage)
    await flushPromises()

    const prompt = vi.spyOn(window, 'prompt').mockReturnValue(null)
    await wrapper.get('[data-testid="group-rename-group-1"]').trigger('click')
    await flushPromises()

    expect(groupApi.renameStockGroup).not.toHaveBeenCalled()
    prompt.mockRestore()
  })

  it('shows the folded row without paying for the since-entry maths', async () => {
    vi.mocked(groupApi.fetchStockGroups).mockResolvedValue([{
      group_id: 'group-1', name: '9月候选', note: '', source: 'screen', source_label: '选股结果',
      source_history_id: null, created_at: '2026-09-22T02:00:00Z', updated_at: '2026-09-22T02:00:00Z',
      item_count: 3, criteria: CRITERIA,
    }])
    vi.mocked(groupApi.fetchStockGroupsSummary).mockResolvedValue({
      as_of: '2026-09-22',
      windows: [20, 60],
      labels: { '20': '近 1 月', '60': '近 3 月' },
      groups: [{
        group_id: 'group-1',
        windows: [
          {
            window: 20, trading_days: 20, group_return: 0.0321, market_return: 0.02,
            excess_return: 0.0121, counted: 3, complete: true,
          },
          {
            window: 60, trading_days: 41, group_return: -0.05, market_return: -0.03,
            excess_return: -0.02, counted: 3, complete: false,
          },
        ],
      }],
    })
    const wrapper = mount(FormulaScreenPage)
    await flushPromises()

    const item = wrapper.get('[data-testid="group-item-group-1"]')
    // 入组日期按东八区显示：后端存的是 UTC，直接截前 10 位会差一天。
    expect(item.text()).toContain('入组 2026-09-22')
    expect(item.text()).toContain('筛选条件：')
    expect(item.text()).toContain('近 1 月')
    expect(item.text()).toContain('+3.21%')
    // 窗口没填满必须说清实际只有几天，不能让人当成 60 天的收益看。
    expect(item.text()).toContain('仅 41 个交易日')

    // 折叠态的代价到此为止：「入组至今」要扫全市场，不展开就不该被触发。
    expect(groupApi.fetchStockGroupPerformance).not.toHaveBeenCalled()
    await wrapper.get('[data-testid="group-pick-group-1"]').trigger('click')
    await flushPromises()
    expect(groupApi.fetchStockGroupPerformance).toHaveBeenCalledWith('group-1')
  })

  it('folds the list to 20 rows until all groups are asked for', async () => {
    vi.mocked(groupApi.fetchStockGroups).mockResolvedValue(
      Array.from({ length: 25 }, (_, index) => ({
        group_id: `g-${index}`, name: `组${index}`, note: '', source: 'screen',
        source_label: '选股结果', source_history_id: null,
        created_at: '2026-09-22T02:00:00Z', updated_at: '2026-09-22T02:00:00Z',
        item_count: 2, criteria: null,
      })),
    )
    const wrapper = mount(FormulaScreenPage)
    await flushPromises()

    expect(wrapper.findAll('[data-testid^="group-item-"]').length).toBe(20)
    await wrapper.get('[data-testid="group-toggle-all"]').trigger('click')
    await flushPromises()
    expect(wrapper.findAll('[data-testid^="group-item-"]').length).toBe(25)
  })

  it('keeps both save buttons disabled until there is something to save', async () => {
    const wrapper = mount(FormulaScreenPage)
    await flushPromises()

    // 没结果时整批保存是禁用的，没勾选时保存勾选也是禁用的——存出一组空的东西
    // 没有任何意义，按钮直接不给点比点了再报错更清楚。
    expect(wrapper.get('[data-testid="save-all-group"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[data-testid="save-picked-group"]').attributes('disabled')).toBeDefined()
    expect(groupApi.createStockGroup).not.toHaveBeenCalled()
  })
})
