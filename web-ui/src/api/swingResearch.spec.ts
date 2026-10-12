import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  SwingResearchApiError,
  deleteSwingResearchHistory,
  fetchSwingResearchHistory,
  getSwingResearchHistory,
  runSwingIndicatorStudy,
  runSwingOverview,
  type SwingProgress,
} from './swingResearch'

/** 一段 SSE 响应。故意把帧从中间切开，逼解析器自己攒 buffer。 */
function sseResponse(frames: string[]): Response {
  const text = frames.map((frame) => `data: ${frame}\n\n`).join('')
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      const encoder = new TextEncoder()
      controller.enqueue(encoder.encode(text.slice(0, 6)))
      controller.enqueue(encoder.encode(text.slice(6)))
      controller.close()
    },
  })
  return new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
}

describe('swing research API', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('posts the overview with several reversal thresholds at once', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ data: { stock_count: 1000, scenarios: [] } }), { status: 200 }),
    )

    await expect(
      runSwingOverview({ universe: 'all', reversal_multiples: [2, 3, 4], limit_stocks: 1000 }),
    ).resolves.toMatchObject({ stock_count: 1000 })

    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/v1/swing-research/overview/stream')
    expect(JSON.parse(String(init?.body))).toEqual({
      universe: 'all',
      reversal_multiples: [2, 3, 4],
      limit_stocks: 1000,
    })
  })

  it('posts the indicator study and returns scores with the condition plan', async () => {
    const data = { scores: [], significant_count: 0, condition_plan: null }
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ data }), { status: 200 }),
    )

    await expect(
      runSwingIndicatorStudy({ labels: ['up_onset_ahead'], reversal_multiple: 3 }),
    ).resolves.toEqual({ result: data, historyId: null })

    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/v1/swing-research/indicator-study/stream')
    expect(JSON.parse(String(init?.body))).toEqual({ labels: ['up_onset_ahead'], reversal_multiple: 3 })
  })

  it('surfaces the backend message instead of a generic failure', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ detail: '面板需要 5.2 GB 临时空间，超出上限 3 GB' }), {
        status: 400,
      }),
    )

    await expect(runSwingIndicatorStudy({ limit_stocks: 5000 })).rejects.toMatchObject({
      name: 'SwingResearchApiError',
      message: '面板需要 5.2 GB 临时空间，超出上限 3 GB',
      status: 400,
    })
    expect(SwingResearchApiError.prototype instanceof Error).toBe(true)
  })

  it('reports each stage and resolves with the result frame', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      sseResponse([
        JSON.stringify({ type: 'progress', current: 1, total: 4, stage: '正在扫描交易日' }),
        JSON.stringify({ type: 'progress', current: 3, total: 4, stage: '正在逐日横截面打分' }),
        JSON.stringify({
          type: 'result',
          data: { scores: [1, 2] },
          history_id: 'hist-9',
        }),
      ]),
    )

    const seen: SwingProgress[] = []
    await expect(
      runSwingIndicatorStudy({}, (progress) => seen.push(progress)),
      // 后端自动存档的 id 要跟着结果回来，页面才能刷新历史列表。
    ).resolves.toEqual({ result: { scores: [1, 2] }, historyId: 'hist-9' })

    expect(seen.map((item) => item.stage)).toEqual(['正在扫描交易日', '正在逐日横截面打分'])
    expect(seen[1].ratio).toBeCloseTo(0.75)
  })

  it('turns an error frame into the same exception the sync endpoints raise', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      sseResponse([
        JSON.stringify({ type: 'progress', current: 1, total: 4, stage: '正在扫描交易日' }),
        JSON.stringify({ type: 'error', detail: '以下指标不能用于波段研究: close' }),
      ]),
    )

    await expect(runSwingIndicatorStudy({})).rejects.toMatchObject({
      name: 'SwingResearchApiError',
      message: '以下指标不能用于波段研究: close',
    })
  })

  it('fails loudly when the stream ends without a result', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      sseResponse([JSON.stringify({ type: 'progress', current: 2, total: 4, stage: '正在扫描' })]),
    )

    await expect(runSwingIndicatorStudy({})).rejects.toMatchObject({
      message: '波段研究没有返回结果，请重试。',
    })
  })

  it('lists, reads and deletes swing research history', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: [{ history_id: 'swing-1' }] }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { history_id: 'swing-1', result: {} } }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ data: { deleted: true } }), { status: 200 }))

    await expect(fetchSwingResearchHistory()).resolves.toEqual([{ history_id: 'swing-1' }])
    await expect(getSwingResearchHistory('swing-1')).resolves.toMatchObject({ history_id: 'swing-1' })
    await expect(deleteSwingResearchHistory('swing/1')).resolves.toBe(true)

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      '/api/v1/swing-research/history',
      '/api/v1/swing-research/history/swing-1',
      '/api/v1/swing-research/history/swing%2F1',
    ])
    expect(fetchMock.mock.calls[2][1]?.method).toBe('DELETE')
  })

  it('reports a connection failure when the service is unreachable', async () => {
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new TypeError('fetch failed'))

    await expect(fetchSwingResearchHistory()).rejects.toMatchObject({
      name: 'SwingResearchApiError',
      message: '无法连接波段研究服务，请确认后端已启动。',
    })
  })

  it('surfaces the server message for a failed history request', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ error: { message: '历史记录不存在' } }), { status: 404 }),
    )

    await expect(getSwingResearchHistory('missing')).rejects.toMatchObject({
      message: '历史记录不存在',
    })
  })
})
