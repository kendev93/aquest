import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  checkInstrumentConditions,
  fetchInstrumentConditionLibrary,
  fetchInstrumentValues,
  runInstrumentBacktest,
} from './instrumentLab'

describe('instrument lab API', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('posts the indicator timeline request to /values', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ data: { bars: 10, series: [], notes: [] } }), { status: 200 }),
    )

    await fetchInstrumentValues({ market: 'SH', code: '600000', value_ids: ['rsi14'] })

    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/v1/instrument-lab/values')
    expect(JSON.parse(String(init?.body))).toEqual({
      market: 'SH',
      code: '600000',
      value_ids: ['rsi14'],
    })
  })

  it('reads the verified condition library with a limit', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ data: { items: [] } }), { status: 200 }),
    )

    await fetchInstrumentConditionLibrary(20)

    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/instrument-lab/conditions/library?limit=20')
  })

  it('posts the selected conditions with the horizon', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ data: { items: [], sample_days: 100 } }), { status: 200 }),
    )
    const condition = {
      left_value: 'technical.rsi14',
      operator: 'lte',
      right_value: null,
      right_constant: 30,
    }

    await checkInstrumentConditions({
      market: 'SH',
      code: '600000',
      horizon: 5,
      conditions: [condition],
    })

    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/v1/instrument-lab/conditions/check')
    expect(JSON.parse(String(init?.body))).toMatchObject({ horizon: 5, conditions: [condition] })
  })

  it('posts the backtest payload to /backtest', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ data: { bars: 3, trades: [] } }), { status: 200 }),
    )

    await runInstrumentBacktest({ market: 'SH', code: '600000', initial_cash: 100000 })

    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/v1/instrument-lab/backtest')
    expect(JSON.parse(String(init?.body))).toMatchObject({ code: '600000' })
  })

  it('surfaces the server message on a bad indicator', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ error: { message: '未知指标: nope' } }), { status: 422 }),
    )

    await expect(
      fetchInstrumentValues({ market: 'SH', code: '600000', value_ids: ['nope'] }),
    ).rejects.toThrow('nope')
  })
})
