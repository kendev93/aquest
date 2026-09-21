import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import * as formulaScreenApi from '../api/formulaScreen'
import * as instrumentLabApi from '../api/instrumentLab'
import type {
  InstrumentLabCheckReport,
  InstrumentLabLibraryItem,
  InstrumentLabValues,
} from '../api/instrumentLab'
import * as marketDataApi from '../api/marketData'
import type { LocalInstrument } from '../types'
import InstrumentLabPage from './InstrumentLabPage.vue'

vi.mock('../api/formulaScreen', async () => {
  const actual = await vi.importActual<typeof import('../api/formulaScreen')>('../api/formulaScreen')
  return { ...actual, fetchMetadata: vi.fn() }
})

vi.mock('../api/marketData', async () => {
  const actual = await vi.importActual<typeof import('../api/marketData')>('../api/marketData')
  return { ...actual, fetchLocalInstruments: vi.fn(), fetchLocalChart: vi.fn() }
})

vi.mock('../api/instrumentLab', async () => {
  const actual = await vi.importActual<typeof import('../api/instrumentLab')>('../api/instrumentLab')
  return {
    ...actual,
    fetchInstrumentValues: vi.fn(),
    fetchInstrumentConditionLibrary: vi.fn(),
    checkInstrumentConditions: vi.fn(),
    runInstrumentBacktest: vi.fn(),
  }
})

const instrument: LocalInstrument = {
  market: 'SH',
  code: '600000',
  name: '测试股份',
  instrument_type: 'stock',
  board: 'main',
  provider: 'local',
  bars: 240,
  data_start: '2024-01-02',
  data_end: '2024-12-31',
  last_close: 10.5,
  error: null,
}

const libraryItem: InstrumentLabLibraryItem = {
  key: 'technical.rsi14|lte||30',
  label: 'RSI14 ≤ 30',
  side: 'buy',
  condition: {
    left_value: 'technical.rsi14',
    operator: 'lte',
    right_value: null,
    right_constant: 30,
  },
  lift: 1.8,
  pvalue: 0.01,
  support_count: 300,
  instrument_count: 120,
  history_id: 'h1',
  created_at: '2026-09-01T10:00:00',
  scanned_stocks: 500,
}

function values(): InstrumentLabValues {
  return {
    market: 'SH',
    code: '600000',
    name: '测试股份',
    start_date: '2024-01-02',
    end_date: '2024-12-31',
    bars: 240,
    series: [
      {
        value_id: 'technical.rsi14',
        label: 'RSI14',
        dates: ['2024-01-02', '2024-01-03', '2024-01-04'],
        values: [30, 45, 60],
        last_value: 60,
        percentile: 88.4,
        note: null,
      },
    ],
    thinned: false,
    notes: ['自身历史分位：当前值在这只票自身历史取值中的位置'],
  }
}

function checkReport(overrides: Partial<InstrumentLabCheckReport['items'][number]> = {}) {
  return {
    market: 'SH',
    code: '600000',
    name: '测试股份',
    horizon: 5,
    start_date: '2024-01-02',
    end_date: '2024-12-31',
    sample_days: 235,
    permutations: 200,
    block_size: 20,
    items: [
      {
        key: 'technical.rsi14|lte||30',
        label: 'RSI14 ≤ 30',
        hit_count: 0,
        sample_days: 235,
        hit_mean_return: null,
        base_mean_return: 0.001,
        difference: null,
        p_value: null,
        adjusted_p_value: null,
        significant: null,
        hit_dates: [],
        note: '该标的上从未命中这个条件',
        ...overrides,
      },
    ],
    notes: [],
  }
}

describe('InstrumentLabPage', () => {
  beforeEach(() => {
    vi.mocked(formulaScreenApi.fetchMetadata).mockResolvedValue({
      indicators: [
        {
          id: 'technical',
          display_name: '技术指标',
          minimum_bars: 60,
          recommended_bars: 250,
          signals: [],
          values: [
            {
              id: 'technical.rsi14',
              display_name: 'RSI14',
              description: '相对强弱',
              bidirectional: true,
              price_scale: false,
              rankable: true,
            },
          ],
        },
      ],
      combine_modes: [{ value: 'all', label: '同时满足' }],
      condition_operators: [{ value: 'lte', label: '小于等于' }],
      supported_markets: ['SH', 'SZ'],
      supported_universe: [{ value: 'all', label: '全部' }],
      periods: [{ value: 'daily', label: '日线' }],
      data_directory_help: '',
    })
    vi.mocked(marketDataApi.fetchLocalInstruments).mockResolvedValue({
      items: [instrument],
      meta: { total: 1, page: 1, page_size: 20, pages: 1 },
    })
    vi.mocked(marketDataApi.fetchLocalChart).mockResolvedValue({
      market: 'SH',
      code: '600000',
      period: 'daily',
      total_daily_bars: 240,
      bars: 240,
      available_data_start: '2024-01-02',
      available_data_end: '2024-12-31',
      data_start: '2024-01-02',
      data_end: '2024-12-31',
      candles: [
        {
          date: '2024-01-02',
          open: 10,
          high: 10.5,
          low: 9.8,
          close: 10.2,
          volume: 1000,
          amount: 10200,
          ma: { ma5: 10.1, ma20: 10 },
          rsi14: 45,
          macd: 0.1,
          macd_signal: 0.05,
          macd_histogram: 0.05,
        },
      ],
      formula_panels: [],
    })
    vi.mocked(instrumentLabApi.fetchInstrumentConditionLibrary).mockResolvedValue({
      items: [libraryItem],
    })
    vi.mocked(instrumentLabApi.fetchInstrumentValues).mockResolvedValue(values())
    vi.mocked(instrumentLabApi.checkInstrumentConditions).mockResolvedValue(checkReport())
    vi.mocked(instrumentLabApi.runInstrumentBacktest).mockResolvedValue({
      market: 'SH',
      code: '600000',
      bars: 240,
      performance: { total_return: 0.08, max_drawdown: 0.12 },
      trades: [{ datetime: '2024-01-02 00:00:00', direction: 'buy', price: 10.2, size: 100, pnl: 0 }],
      equity_curve: [],
    })
    window.history.replaceState({}, '', '/instrument-lab')
  })

  async function mountPage() {
    const wrapper = mount(InstrumentLabPage)
    await flushPromises()
    return wrapper
  }

  it('starts from the instrument list and reveals the sections once picked', async () => {
    const wrapper = await mountPage()

    expect(wrapper.find('[data-testid="lab-pick-600000"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="lab-values"]').exists()).toBe(false)

    await wrapper.find('[data-testid="lab-pick-600000"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-testid="lab-values"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="lab-conditions"]').exists()).toBe(true)
  })

  it('reads the picked indicators and shows the self-history percentile', async () => {
    const wrapper = await mountPage()
    await wrapper.find('[data-testid="lab-pick-600000"]').trigger('click')
    await flushPromises()

    await wrapper.find('[data-testid="lab-value-technical.rsi14"]').setValue(true)
    await wrapper.find('[data-testid="lab-read-values"]').trigger('click')
    await flushPromises()

    expect(instrumentLabApi.fetchInstrumentValues).toHaveBeenCalledWith(
      expect.objectContaining({ code: '600000', value_ids: ['technical.rsi14'] }),
    )
    expect(wrapper.text()).toContain('自身历史 P88')
    expect(wrapper.find('[data-testid="lab-values-chart"]').exists()).toBe(true)
  })

  it('runs a condition check and says plainly when it never hits', async () => {
    const wrapper = await mountPage()
    await wrapper.find('[data-testid="lab-pick-600000"]').trigger('click')
    await flushPromises()

    await wrapper.find('[data-testid="lab-condition-technical.rsi14|lte||30"]').setValue(true)
    await wrapper.find('[data-testid="lab-run-check"]').trigger('click')
    await flushPromises()

    expect(instrumentLabApi.checkInstrumentConditions).toHaveBeenCalledWith(
      expect.objectContaining({ horizon: 5, conditions: [libraryItem.condition] }),
    )
    expect(wrapper.text()).toContain('没有命中')
    expect(wrapper.text()).toContain('该标的上从未命中这个条件')
  })

  it('draws buy and sell markers on the candlestick chart after a backtest', async () => {
    const wrapper = await mountPage()
    await wrapper.find('[data-testid="lab-pick-600000"]').trigger('click')
    await flushPromises()

    await wrapper.find('[data-testid="lab-run-backtest"]').trigger('click')
    await flushPromises()

    expect(instrumentLabApi.runInstrumentBacktest).toHaveBeenCalled()
    expect(wrapper.text()).toContain('+8.00%')
    const markers = wrapper.findAll('.trade-markers polygon')
    expect(markers.length).toBe(1)
    expect(markers[0].classes()).toContain('marker-buy')
  })
})
