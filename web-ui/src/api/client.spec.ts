import { afterEach, describe, expect, it, vi } from 'vitest'

import { ApiError, createApiErrorClass, createRequestClient, describeError } from './client'
import type { ApiRequestMessages, RequestClientOptions } from './client'

const ErrorClass = createApiErrorClass('SampleApiError')

/** 造一个用于测试的 client；`over` 覆盖 baseUrl / retryGet / 文案。 */
function client(
  over: Partial<Omit<RequestClientOptions, 'errorClass'>> = {},
): ReturnType<typeof createRequestClient> {
  return createRequestClient({
    errorClass: ErrorClass,
    messages: { fallback: '兜底文案', ...over.messages } as ApiRequestMessages,
    baseUrl: over.baseUrl,
    retryGet: over.retryGet,
  })
}

describe('api client', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('returns the parsed payload on success', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ ok: 1 }), { status: 200 }),
    ))
    const request = client()
    await expect(request<{ ok: number }>('/x')).resolves.toEqual({ ok: 1 })
  })

  it('prefixes relative paths with baseUrl but leaves absolute urls alone', async () => {
    // Response 的 body 只能读一次，每次调用都得给一个新的。
    const fetch = vi.fn().mockImplementation(async () => new Response('{"a":1}', { status: 200 }))
    vi.stubGlobal('fetch', fetch)

    const request = client({ baseUrl: '/api/v1/sample' })
    await request('/items')
    await client()('/api/v2/other')

    expect(fetch.mock.calls[0]?.[0]).toBe('/api/v1/sample/items')
    expect(fetch.mock.calls[1]?.[0]).toBe('/api/v2/other')
  })

  it('surfaces the structured message, then detail, then the fallback text', async () => {
    const cases: { body: unknown; expected: string }[] = [
      { body: { error: { message: '后端说的话' } }, expected: '后端说的话' },
      { body: { detail: 'detail 兜底' }, expected: 'detail 兜底' },
      { body: {}, expected: '兜底文案' },
    ]
    for (const { body, expected } of cases) {
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
        new Response(JSON.stringify(body), { status: 500 }),
      ))
      const request = client()
      await expect(request('/x')).rejects.toMatchObject({
        name: 'SampleApiError',
        message: expected,
        status: 500,
      })
    }
  })

  it('reports a network failure with status 0', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('fetch failed')))
    const request = client({ messages: { fallback: '兜底文案', offline: '连不上后端' } })
    await expect(request('/x')).rejects.toMatchObject({ message: '连不上后端', status: 0 })
  })

  it('keeps the raw body available when the response is not JSON', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      new Response('<html>502 Bad Gateway</html>', { status: 502 }),
    ))
    const request = client({
      messages: { fallback: '兜底文案', invalidBody: (_status, raw) => raw.slice(0, 20) },
    })
    await expect(request('/x')).rejects.toMatchObject({
      message: '<html>502 Bad Gatewa',
      status: 502,
    })
  })

  it('treats an empty success body as an error rather than returning nothing', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('', { status: 200 })))
    const request = client({ messages: { fallback: '兜底文案', emptyBody: '空响应' } })
    await expect(request('/x')).rejects.toMatchObject({ message: '空响应' })
  })

  it('retries GET on gateway errors but leaves POST alone', async () => {
    const gateway = () => new Response('nope', { status: 503 })
    const retryGet = { attempts: 4, retryableStatuses: new Set([502, 503]), delaysMs: [0, 0, 0] }

    const getFetch = vi.fn()
      .mockResolvedValueOnce(gateway())
      .mockResolvedValueOnce(gateway())
      .mockResolvedValue(new Response('{"ok":true}', { status: 200 }))
    vi.stubGlobal('fetch', getFetch)
    await expect(client({ retryGet })('/poll')).resolves.toEqual({ ok: true })
    expect(getFetch).toHaveBeenCalledTimes(3)

    const postFetch = vi.fn()
      .mockResolvedValueOnce(gateway())
      .mockResolvedValue(new Response('{"ok":true}', { status: 200 }))
    vi.stubGlobal('fetch', postFetch)
    await expect(client({ retryGet })('/submit', { method: 'POST' })).rejects.toMatchObject({ status: 503 })
    expect(postFetch).toHaveBeenCalledTimes(1)
  })

  it('exposes ApiError as the shared base so callers can catch everything at once', () => {
    const error = new ErrorClass('boom', 418)
    expect(error).toBeInstanceOf(ApiError)
    expect(error.name).toBe('SampleApiError')
  })
})

describe('describeError', () => {
  const fallback = '组合回测失败，请检查本地 DuckDB 数据和配置后重试。'

  it('passes backend messages through and hides local exception text by default', () => {
    expect(describeError(new ErrorClass('标的代码不能为空', 422), { fallback })).toBe('标的代码不能为空')
    expect(describeError(new TypeError('reading "codes"'), { fallback })).toBe(fallback)
    expect(describeError('boom', { fallback })).toBe(fallback)
    expect(describeError(new Error(''), { fallback })).toBe(fallback)
  })

  it('shows local exception text when the page asks for it', () => {
    expect(describeError(new Error('IndexedDB 配额不足'), { fallback, localMessage: 'raw' }))
      .toBe('IndexedDB 配额不足')
  })

  it('appends local exception text to a prefix, defaulting the prefix to the fallback', () => {
    expect(describeError(new Error('boom'), { fallback: '扫描失败', localMessage: 'prefix' }))
      .toBe('扫描失败：boom')
    expect(describeError(new Error('boom'), { fallback, localMessage: 'prefix', prefix: '策略实验失败' }))
      .toBe('策略实验失败：boom')
    expect(describeError('boom', { fallback: '扫描失败', localMessage: 'prefix' })).toBe('扫描失败')
  })
})
