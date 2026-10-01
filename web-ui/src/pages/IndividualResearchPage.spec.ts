import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import IndividualResearchPage from './IndividualResearchPage.vue'
import * as formulaApi from '../api/formulaScreen'
import * as individualApi from '../api/individualResearch'
import * as marketApi from '../api/marketData'
import * as jobsApi from '../api/jobs'
import * as strategyApi from '../api/savedStrategies'
import type { FormulaScreenMetadata, IndividualResearchResult, LocalInstrument } from '../types'

vi.mock('../api/formulaScreen', async () => {
  const actual = await vi.importActual<typeof import('../api/formulaScreen')>('../api/formulaScreen')
  return { ...actual, fetchMetadata: vi.fn() }
})

vi.mock('../api/individualResearch', async () => {
  const actual = await vi.importActual<typeof import('../api/individualResearch')>('../api/individualResearch')
  return {
    ...actual,
    createIndividualResearch: vi.fn(),
    getIndividualResearchJob: vi.fn(),
    cancelIndividualResearch: vi.fn(),
    fetchIndividualResearchHistory: vi.fn(),
    getIndividualResearchHistory: vi.fn(),
    deleteIndividualResearchHistory: vi.fn(),
  }
})

vi.mock('../api/marketData', async () => {
  const actual = await vi.importActual<typeof import('../api/marketData')>('../api/marketData')
  return { ...actual, fetchLocalInstruments: vi.fn() }
})

vi.mock('../api/jobs', async () => {
  const actual = await vi.importActual<typeof import('../api/jobs')>('../api/jobs')
  return { ...actual, fetchActiveJobs: vi.fn() }
})

vi.mock('../api/savedStrategies', async () => {
  const actual = await vi.importActual<typeof import('../api/savedStrategies')>('../api/savedStrategies')
  return { ...actual, createStrategy: vi.fn() }
})

const metadata: FormulaScreenMetadata = {
  indicators: [{
    id: 'technical',
    display_name: '技术指标',
    minimum_bars: 20,
    recommended_bars: 60,
    signals: [{ id: 'technical.rsi_oversold', display_name: 'RSI 超卖', description: 'RSI 低于阈值' }],
    values: [
      { id: 'technical.rsi14', display_name: 'RSI 14', description: 'RSI 指标', rankable: true },
      { id: 'technical.ma5', display_name: 'MA 5', description: '5 日均线', rankable: false },
    ],
  }],
  combine_modes: [{ value: 'all', label: '全部满足' }, { value: 'any', label: '任一满足' }, { value: 'at_least', label: '至少满足' }],
  supported_markets: ['SH', 'SZ'],
  supported_universe: [],
  periods: [{ value: 'daily', label: '日线' }],
  data_directory_help: '测试数据目录说明',
  condition_operators: [],
}

const candidate = {
  candidate_hash: 'candidate-1',
  candidate_id: 'rules-1',
  condition_candidate_id: 'rules-1',
  full_backtest_id: 'candidate-1',
  human_readable_rules: 'RSI 14 ≤ 35 / RSI 14 ≥ 65',
  condition_rules: { buy: { signals: [], conditions: [{ left_value: 'technical.rsi14', operator: 'lte', right_constant: 35 }] }, sell: { signals: [], conditions: [{ left_value: 'technical.rsi14', operator: 'gte', right_constant: 65 }] } },
  config: {
    universe: 'codes' as const,
    codes: ['SH:600000'],
    selected_signals: [],
    combine_mode: 'all',
    minimum_matches: null,
    single_security_mode: true,
    ranking_value: null,
    rank_order: 'desc' as const,
    max_positions: 1,
    buy_rule: { signals: [], conditions: [{ left_value: 'technical.rsi14', operator: 'lte', right_constant: 35 }] },
    sell_rule: { signals: [], conditions: [{ left_value: 'technical.rsi14', operator: 'gte', right_constant: 65 }] },
    stop_loss_pct: 0.08,
    take_profit_pct: 0.2,
    trailing_stop_pct: null,
    start_date: '2021-01-01',
    end_date: '2025-12-31',
  },
  metrics: {
    train: { sharpe: 0.7, annual_return: 0.12, total_return: 0.4, win_rate: 0.55, payoff_ratio: 1.5, max_drawdown: -0.18, closed_trades: 9 },
    validation: { sharpe: 0.8, annual_return: 0.15, total_return: 0.05, win_rate: 0.6, payoff_ratio: 1.6, max_drawdown: -0.2, closed_trades: 6 },
    holdout: { sharpe: 0.6, annual_return: 0.1, total_return: 0.03, win_rate: 0.5, payoff_ratio: 1.2, max_drawdown: -0.32, closed_trades: 3 },
  },
  quality: { badges: ['样本较充分', '风险高'], sample_adequate: true, high_drawdown_risk: true, eligible_for_display: true },
  data_fingerprint: 'data-1',
} as unknown as IndividualResearchResult['results_by_sharpe'][number]

const fullBacktest = {
    start_date: '2021-01-01',
    end_date: '2025-12-31',
    bars: 1250,
    closed_trades: 1,
    performance: { total_return: 0.25, annual_return: 0.05, sharpe: 0.9, max_drawdown: -0.12, win_rate: 0.6, payoff_ratio: 1.5, closed_trades: 8 },
    equity_curve: [{ date: '2021-01-01', equity: 1_000_000 }, { date: '2025-12-31', equity: 1_250_000 }],
    trades: [{ date: '2021-02-10', direction: 'SELL', size: 100, price: 12.5, pnl: 1250, reason: '止盈', risk_trigger: 'take_profit' }],
    data_fingerprint: 'data-1',
}

const result: IndividualResearchResult = {
  history_id: 'research-1',
  market: 'SH',
  code: '600000',
  name: '浦发银行',
  start_date: '2021-01-01',
  end_date: '2025-12-31',
  date_range: { start: '2021-01-01', end: '2025-12-31', days: 1250 },
  single_security_mode: true,
  segmentation: { up_segments: 12, down_segments: 11, completed_segments: 23, median_up_amplitude: 0.08, median_down_amplitude: 0.07 },
  split_metadata: {
    train: { start: '2021-01-01', end: '2023-12-31', days: 700, purged_days: 10 },
    validation: { start: '2024-01-15', end: '2024-12-31', days: 220, purged_days: 10 },
    holdout: { start: '2025-01-15', end: '2025-12-31', days: 220, purged_days: 10 },
    method: '60/20/20',
  },
  condition_candidates: { buy: [], sell: [] },
  rule_candidates: { results_by_sharpe: [], results_by_return: [], evaluated_pairs: 1 },
  results_by_sharpe: [candidate],
  results_by_return: [candidate],
  full_backtests: { 'candidate-1': fullBacktest },
  candidate_count: 1,
  risk_rule_sets: ['rules-1'],
  risk_trials_per_rule_set: 1000,
  searched_value_ids: ['technical.rsi14'],
  searched_signal_ids: [],
  data_fingerprint: 'data-1',
  configuration_fingerprint: 'config-1',
}

function mountPage() {
  return mount(IndividualResearchPage, {
    global: {
      stubs: {
        PageGuide: true,
        DateRangeSelector: {
          props: ['modelValue', 'prefix', 'testid', 'helper', 'defaultMode'],
          template: `<fieldset><legend>历史区间</legend>
            <button type="button" data-testid="date-range-custom-invalid" @click="$emit('update:modelValue', { mode: 'custom', startDate: '', endDate: '' })" />
            <button type="button" data-testid="date-range-custom-inverted" @click="$emit('update:modelValue', { mode: 'custom', startDate: '2025-01-01', endDate: '2024-01-01' })" />
          </fieldset>`,
        },
      },
    },
  })
}

async function chooseStock(page: ReturnType<typeof mountPage>) {
  await page.get('[data-testid="individual-stock-search-button"]').trigger('click')
  await flushPromises()
  await page.get('.individual-instrument-option').trigger('click')
}

describe('IndividualResearchPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(formulaApi.fetchMetadata).mockResolvedValue(metadata)
    vi.mocked(marketApi.fetchLocalInstruments).mockResolvedValue({
      items: [{ market: 'SH', code: '600000', name: '浦发银行', instrument_type: 'stock', board: 'main', bars: 2000, data_start: '2020-01-01', data_end: '2025-12-31', last_close: 10, error: null }],
      meta: { total: 1, page: 1, page_size: 20, pages: 1 },
    })
    vi.mocked(jobsApi.fetchActiveJobs).mockResolvedValue([])
    vi.mocked(individualApi.fetchIndividualResearchHistory).mockResolvedValue([])
    vi.mocked(strategyApi.createStrategy).mockResolvedValue({} as never)
  })

  it('lets the user select one local stock and submits a single-security request', async () => {
    vi.mocked(individualApi.createIndividualResearch).mockResolvedValue({ job_id: 'job-1', status: 'queued' })
    vi.mocked(individualApi.getIndividualResearchJob).mockResolvedValue({
      job_id: 'job-1', status: 'completed', progress: 1, total_candidates: 1, total_scanned: 1,
      errors: 0, skipped: 0, error: null, result, description: '个股研究', phase: 'completed', metadata: {}, cancel_requested: false,
    })
    const page = mountPage()
    await flushPromises()
    await chooseStock(page)
    expect(page.findAll('legend').filter((legend) => legend.text() === '历史区间')).toHaveLength(1)
    expect(page.get('[data-testid="individual-stock-date-range"]').text()).toContain(
      '本地行情覆盖：2020-01-01 → 2025-12-31 · 2000 根日线',
    )
    await page.get('[data-testid="individual-research-form"]').trigger('submit')
    await flushPromises()

    expect(individualApi.createIndividualResearch).toHaveBeenCalledWith(expect.objectContaining({
      market: 'SH',
      code: '600000',
      date_mode: 'full',
      horizon: 10,
      risk_trials: 1000,
      candidate_limit: 10,
      value_ids: ['technical.rsi14', 'technical.ma5'],
    }))
    expect(page.text()).toContain('SH 600000 个股研究')
    expect(page.text()).toContain('验证段')
    expect(page.text()).toContain('留出段')
    expect(page.text()).toContain('MA 5')
    expect(page.text()).toContain('样本较充分')
    expect(page.text()).toContain('风险高')
    expect(page.text()).toContain('全区间回测')
    await page.get('[data-testid="individual-full-details-sharpe-0"]').trigger('click')
    expect(page.get('[data-testid="individual-full-backtest-sharpe-0"]').text()).toContain('止盈')
  })

  it('accepts both the default initial capital and round-number edits', async () => {
    const page = mountPage()
    await flushPromises()
    const input = page.get('[data-testid="individual-cash"]')

    expect((input.element as HTMLInputElement).checkValidity()).toBe(true)
    await input.setValue('100000')
    expect((input.element as HTMLInputElement).checkValidity()).toBe(true)
  })

  it('renders the logical operator for two-indicator buy and sell rules', async () => {
    vi.mocked(individualApi.createIndividualResearch).mockResolvedValue({ job_id: 'job-combo', status: 'queued' })
    const buyCombo = {
      ...candidate,
      candidate_hash: 'combo-and',
      condition_rules: {
        buy: {
          candidate_id: 'buy-combo',
          rule: {
            signals: [], combine_mode: 'all', conditions: [
              { left_value: 'technical.rsi14', operator: 'lte', right_constant: 35 },
              { left_value: 'technical.return20', operator: 'gte', right_constant: 0 },
            ],
          },
        },
        sell: {
          candidate_id: 'sell-combo',
          rule: candidate.condition_rules!.sell,
        },
      },
    }
    const configFallbackRule: NonNullable<
      IndividualResearchResult['results_by_sharpe'][number]['config']['buy_rule']
    > = {
      signals: [], combine_mode: 'any', minimum_matches: null, conditions: [
        { left_value: 'technical.rsi14', operator: 'lte', right_constant: 35, right_value: null },
        { left_value: 'technical.return20', operator: 'gte', right_constant: 0, right_value: null },
      ],
    }
    const sellCombo = {
      ...candidate,
      candidate_hash: 'combo-or',
      condition_rules: undefined,
      config: {
        ...candidate.config,
        buy_rule: configFallbackRule,
        sell_rule: configFallbackRule,
      },
    }
    const comboResult: IndividualResearchResult = {
      ...result,
      results_by_sharpe: [buyCombo],
      results_by_return: [sellCombo],
    }
    vi.mocked(individualApi.getIndividualResearchJob).mockResolvedValue({
      job_id: 'job-combo', status: 'completed', progress: 1, total_candidates: 1, total_scanned: 1,
      errors: 0, skipped: 0, error: null, result: comboResult, description: '个股研究',
      phase: 'completed', metadata: {}, cancel_requested: false,
    })

    const page = mountPage()
    await flushPromises()
    await chooseStock(page)
    await page.get('[data-testid="individual-research-form"]').trigger('submit')
    await flushPromises()

    expect(page.get('[data-testid="individual-sharpe-0"]').text()).toContain('且')
    expect(page.get('[data-testid="individual-return-0"]').text()).toContain('或')
  })

  it('saves the selected single-security candidate without launching tracking', async () => {
    vi.mocked(individualApi.createIndividualResearch).mockResolvedValue({ job_id: 'job-1', status: 'queued' })
    vi.mocked(individualApi.getIndividualResearchJob).mockResolvedValue({
      job_id: 'job-1', status: 'completed', progress: 1, total_candidates: 1, total_scanned: 1,
      errors: 0, skipped: 0, error: null, result, description: '个股研究', phase: 'completed', metadata: {}, cancel_requested: false,
    })
    const page = mountPage()
    await flushPromises()
    await chooseStock(page)
    await page.get('[data-testid="individual-research-form"]').trigger('submit')
    await flushPromises()
    await page.get('[data-testid="individual-save-sharpe-0"]').trigger('click')
    await flushPromises()

    expect(strategyApi.createStrategy).toHaveBeenCalledWith(expect.objectContaining({
      request: expect.objectContaining({
        single_security_mode: true,
        universe: 'codes',
        codes: ['SH:600000'],
        ranking_value: null,
        max_positions: 1,
      }),
      source_history_id: 'job-1',
    }))
    expect(individualApi.cancelIndividualResearch).not.toHaveBeenCalled()
    expect(page.text()).toContain('手动上线')
  })

  it('loads and deletes a completed history record while restoring its request settings', async () => {
    const historyItem = {
      history_id: 'research-1',
      backtest_type: 'individual_research' as const,
      created_at: '2026-10-01T10:00:00Z',
      started_at: '2026-10-01T10:00:00Z',
      finished_at: '2026-10-01T10:00:04Z',
      duration_seconds: 4,
      summary: {
        market: 'SH', code: '600000', name: '浦发银行', start_date: '2021-01-01',
        end_date: '2025-12-31', candidate_count: 1,
      },
    }
    vi.mocked(individualApi.fetchIndividualResearchHistory)
      .mockResolvedValueOnce([historyItem])
      .mockResolvedValueOnce([])
    vi.mocked(individualApi.getIndividualResearchHistory).mockResolvedValue({
      ...historyItem,
      request: {
        market: 'SZ', code: '000001', name: '平安银行', date_mode: 'custom',
        start_date: '2020-01-01', end_date: '2024-12-31', reversal_multiple: 2.5,
        atr_window: 14, min_bars: 5, horizon: 10, risk_trials: 2000,
        candidate_limit: 4, initial_cash: 500000, commission: 0.0002,
        min_commission: 1, stamp_tax: 0.001, slippage: 0.001,
        execution: 'next_close', position_mode: 'fixed', fixed_size: 500,
        value_ids: ['technical.rsi14'], signal_ids: ['technical.rsi_oversold'],
      },
      result,
    } as never)
    vi.mocked(individualApi.deleteIndividualResearchHistory).mockResolvedValue({ deleted: true })
    vi.spyOn(window, 'confirm').mockReturnValue(true)

    const page = mountPage()
    await flushPromises()
    expect(page.text()).toContain('2026-10-01 10:00')
    expect(page.text()).toContain('浦发银行')

    const historyButtons = page.get('[data-testid="individual-history"]').findAll('button.table-link')
    await historyButtons[0].trigger('click')
    await flushPromises()
    expect(individualApi.getIndividualResearchHistory).toHaveBeenCalledWith('research-1')
    expect(page.text()).toContain('SH 600000 个股研究')
    expect(page.text()).toContain('已载入 SZ 000001 的个股研究历史。')

    const loadedHistoryButtons = page.get('[data-testid="individual-history"]').findAll('button.table-link')
    await loadedHistoryButtons[1].trigger('click')
    await flushPromises()
    expect(individualApi.deleteIndividualResearchHistory).toHaveBeenCalledWith('research-1')
    expect(page.text()).toContain('个股研究历史已删除。')
    expect(page.text()).toContain('还没有个股研究结果')
  })

  it('validates the rule inputs and filters the local selector to stocks with bars', async () => {
    vi.mocked(marketApi.fetchLocalInstruments).mockResolvedValue({
      items: [
        { market: 'SH', code: '600000', name: '浦发银行', instrument_type: 'stock', board: 'main', bars: 2000, data_start: null, data_end: null, last_close: null, error: null },
        { market: 'SH', code: '510300', name: '沪深300ETF', instrument_type: 'fund', board: null, bars: 2000, data_start: null, data_end: null, last_close: null, error: null },
        { market: 'SZ', code: '000001', name: '无行情股票', instrument_type: 'stock', board: 'main', bars: 0, data_start: null, data_end: null, last_close: null, error: null },
      ] as unknown as LocalInstrument[],
      meta: { total: 3, page: 1, page_size: 20, pages: 1 },
    })
    const page = mountPage()
    await flushPromises()

    const pickerActions = page.findAll('.individual-picker-actions')
    await pickerActions[0].findAll('button')[1].trigger('click')
    await page.get('[data-testid="individual-research-form"]').trigger('submit')
    expect(page.text()).toContain('至少选择一个可搜索指标。')

    await pickerActions[0].findAll('button')[0].trigger('click')
    await pickerActions[1].findAll('button')[1].trigger('click')
    await pickerActions[1].findAll('button')[0].trigger('click')
    vi.useFakeTimers()
    try {
      await page.get('[data-testid="individual-stock-search"]').setValue('浦发')
      await vi.advanceTimersByTimeAsync(250)
      await flushPromises()
    } finally {
      vi.useRealTimers()
    }

    expect(marketApi.fetchLocalInstruments).toHaveBeenLastCalledWith({ keyword: '浦发', page_size: 20 })
    expect(page.findAll('.individual-instrument-option')).toHaveLength(1)
    await page.get('.individual-instrument-option').trigger('click')
    expect(page.text()).toContain('SH 600000')
  })

  it('covers date, parameter, fixed-size, and indicator selection controls', async () => {
    const page = mountPage()
    await flushPromises()
    await chooseStock(page)

    const valueInput = page.get('[data-testid="individual-value-picker"] input')
    await valueInput.setValue(false)
    await valueInput.setValue(true)
    const signalInput = page.get('[data-testid="individual-signal-picker"] input')
    await signalInput.setValue(false)
    await signalInput.setValue(true)

    await page.get('[data-testid="date-range-custom-invalid"]').trigger('click')
    await page.get('[data-testid="individual-reversal"]').setValue('2.5')
    await page.get('[data-testid="individual-atr"]').setValue('20')
    await page.get('[data-testid="individual-min-bars"]').setValue('6')
    await page.get('[data-testid="individual-horizon"]').setValue('12')
    await page.get('[data-testid="individual-risk-trials"]').setValue('2000')
    await page.get('[data-testid="individual-candidate-limit"]').setValue('4')
    await page.get('[data-testid="individual-cash"]').setValue('500000')
    await page.get('[data-testid="individual-execution"]').setValue('next_close')
    await page.get('[data-testid="individual-position-mode"]').setValue('fixed')
    await page.get('[data-testid="individual-fixed-size"]').setValue('250')
    await page.get('[data-testid="individual-research-form"]').trigger('submit')
    expect(page.text()).toContain('请选择开始日期。')
    expect(page.text()).toContain('请选择结束日期。')
    expect(page.text()).toContain('固定股数必须是 100 的正整数倍。')

    await page.get('[data-testid="date-range-custom-inverted"]').trigger('click')
    await page.get('[data-testid="individual-fixed-size"]').setValue('500')
    await page.get('[data-testid="individual-research-form"]').trigger('submit')
    expect(page.text()).toContain('结束日期不能早于开始日期。')
  })

  it('requests cancellation while a study is running and handles local-instrument errors', async () => {
    vi.mocked(individualApi.createIndividualResearch).mockResolvedValue({ job_id: 'job-cancel', status: 'queued' })
    vi.mocked(individualApi.getIndividualResearchJob)
      .mockResolvedValueOnce({
        job_id: 'job-cancel', status: 'running', progress: 0.5, total_candidates: 1,
        total_scanned: 1, errors: 0, skipped: 0, error: null, result: null,
        description: '个股研究', phase: '寻优退出风控', metadata: {}, cancel_requested: false,
      })
      .mockResolvedValueOnce({
        job_id: 'job-cancel', status: 'cancelled', progress: 0.5, total_candidates: 1,
        total_scanned: 1, errors: 0, skipped: 0, error: null, result: null,
        description: '个股研究', phase: 'cancelled', metadata: {}, cancel_requested: true,
      })
    const page = mountPage()
    await flushPromises()
    await chooseStock(page)

    vi.useFakeTimers()
    try {
      await page.get('[data-testid="individual-research-form"]').trigger('submit')
      await flushPromises()
      expect(page.find('[data-testid="individual-cancel"]').exists()).toBe(true)
      await page.get('[data-testid="individual-cancel"]').trigger('click')
      await flushPromises()
      expect(individualApi.cancelIndividualResearch).toHaveBeenCalledWith('job-cancel')
      await vi.advanceTimersByTimeAsync(500)
      await flushPromises()
      expect(page.get('[data-testid="individual-research-message"]').text()).toContain('个股研究已取消')
    } finally {
      vi.useRealTimers()
    }

    vi.mocked(marketApi.fetchLocalInstruments).mockRejectedValueOnce(new Error('目录读取失败'))
    await page.get('[data-testid="individual-stock-search-button"]').trigger('click')
    await flushPromises()
    expect(page.text()).toContain('目录读取失败')
  })
})
