/**
 * REST 调用的公共底座。
 *
 * 以前每个 `api/*.ts` 都自带一份 `request<T>()`：同样的 fetch、同样的 JSON 解析、
 * 同样的错误三段文案，一共抄了 20 份（`formulaScreen.ts` 里还多一份完全相同的
 * `requestAt`）。其中 16 份连抛出的异常类都是共用的 `FormulaScreenApiError` ——
 * 于是行情同步失败也会打着「公式选股」的名字出现在 UI 上。
 *
 * 现在不变的部分只留这一份，真正的差异（异常类名、文案、要不要重试 GET）交给
 * `createRequestClient` 的参数表达。每个模块对外抛出的异常类仍是各自那一个，
 * `instanceof` 判定和异常名都不变，所以调用方与既有测试无需改动。
 */

/** 所有 API 异常的公共基类：调用方想统一兜底时可以只认这一个。 */
export class ApiError extends Error {
  constructor(message: string, public readonly status: number, name = 'ApiError') {
    super(message)
    this.name = name
  }
}

/** 后端在失败响应体里能给到的字段。可能有其中之一，也可能都没有。 */
export interface ApiErrorPayload {
  error?: { code?: string; message?: string }
  detail?: string
}

export interface ApiRequestMessages {
  /** 后端给了响应但没带结构化 message 时的兜底文案。必填。 */
  fallback: string
  /**
   * 响应体不是合法 JSON（网关拦截页、Nginx 错误页之类）。
   * `raw` 是原始响应体，需要把网关的报错原文透给用户时用得上。
   * 缺省回落到 fallback。
   */
  invalidBody?: (status: number, raw: string) => string
  /** 响应体为空。缺省回落到 fallback。 */
  emptyBody?: string
  /** fetch 本身抛错：后端没起来、断网。此时 status 为 0。缺省回落到 fallback。 */
  offline?: string
}

/**
 * GET 的重试策略。
 *
 * 目前只有策略实验室这类「提交后长时间轮询」的读接口需要它：寻优一跑就是几十分钟，
 * 期间容器重启或网关抖动会让某次 GET 拿到 502/503，直接判失败会让用户白等一场。
 * GET 幂等，退避重试几次远比中途报红字划算。
 *
 * POST 一律不重试 —— 重复提交研究任务等于浪费几十分钟算力。
 */
export interface GetRetryPolicy {
  attempts: number
  retryableStatuses: ReadonlySet<number>
  delaysMs: readonly number[]
}

export type ApiErrorConstructor = new (message: string, status: number) => ApiError

export interface RequestClientOptions {
  /** 抛出的异常类，`error.name` 取自它的类名。 */
  errorClass: ApiErrorConstructor
  messages: ApiRequestMessages
  retryGet?: GetRetryPolicy
  /**
   * 相对路径前缀。给了之后，传给 request 的 `/xxx` 会被拼成 `${baseUrl}/xxx`；
   * 不设则要求调用方自己传完整 URL。
   */
  baseUrl?: string
}

export type RequestFn = <T>(url: string, init?: RequestInit) => Promise<T>

/** 造一个带指定名字的 ApiError 子类，供各 api 模块导出自己的异常类型。 */
export function createApiErrorClass(errorName: string): ApiErrorConstructor {
  return class extends ApiError {
    constructor(message: string, status: number) {
      super(message, status, errorName)
    }
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => {
    globalThis.setTimeout(resolve, ms)
  })
}

async function fetchOnce(
  url: string,
  init: RequestInit | undefined,
  ErrorClass: ApiErrorConstructor,
  messages: ApiRequestMessages,
): Promise<Response> {
  try {
    return await fetch(url, {
      ...init,
      headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
    })
  } catch {
    throw new ErrorClass(messages.offline ?? messages.fallback, 0)
  }
}

async function fetchWithRetry(
  url: string,
  init: RequestInit | undefined,
  retryGet: GetRetryPolicy | undefined,
  ErrorClass: ApiErrorConstructor,
  messages: ApiRequestMessages,
): Promise<Response> {
  const isGet = (init?.method ?? 'GET').toUpperCase() === 'GET'
  if (!retryGet || !isGet) {
    return fetchOnce(url, init, ErrorClass, messages)
  }
  let response = await fetchOnce(url, init, ErrorClass, messages)
  for (let attempt = 0; attempt < retryGet.attempts - 1; attempt += 1) {
    if (!retryGet.retryableStatuses.has(response.status)) break
    await sleep(retryGet.delaysMs[attempt] ?? 100)
    response = await fetchOnce(url, init, ErrorClass, messages)
  }
  return response
}

async function readPayload<T>(
  response: Response,
  ErrorClass: ApiErrorConstructor,
  messages: ApiRequestMessages,
): Promise<T> {
  const raw = await response.text()
  let payload: (T & ApiErrorPayload) | null = null
  try {
    payload = raw ? (JSON.parse(raw) as T & ApiErrorPayload) : null
  } catch {
    const message = messages.invalidBody?.(response.status, raw) ?? messages.fallback
    throw new ErrorClass(message, response.status)
  }
  if (!response.ok) {
    throw new ErrorClass(
      payload?.error?.message ?? payload?.detail ?? messages.fallback,
      response.status,
    )
  }
  if (payload === null) {
    throw new ErrorClass(messages.emptyBody ?? messages.fallback, response.status)
  }
  return payload as T
}

/** 按模块配置生成一个 `request<T>()`，替换原先各写一份的实现。 */
export function createRequestClient(options: RequestClientOptions): RequestFn {
  const ErrorClass = options.errorClass
  const { messages, retryGet, baseUrl } = options
  return async function request<T>(url: string, init?: RequestInit): Promise<T> {
    const target = baseUrl ? `${baseUrl}${url}` : url
    const response = await fetchWithRetry(target, init, retryGet, ErrorClass, messages)
    return readPayload<T>(response, ErrorClass, messages)
  }
}

export interface ErrorTextOptions {
  /** 后端没给文案、本地异常也给不出可用信息时的兜底句。 */
  fallback: string
  /**
   * 非 ApiError 的本地异常怎么显示：
   * `hide`（默认，只给兜底句，不把内部异常文本抛给用户）、
   * `raw`（直接用它的 message）、
   * `prefix`（拼成「prefix：message」，prefix 省略时用兜底句）。
   */
  localMessage?: 'hide' | 'raw' | 'prefix'
  /** `localMessage: 'prefix'` 时的前缀。 */
  prefix?: string
}

/**
 * catch 到的未知异常 → 界面文案。
 *
 * ApiError 的 message 来自后端响应体，是最有帮助的一句，任何情况下都直接透出。
 * 其余异常是前端本地抛的，要不要把它的原始文本露给用户是个逐页决定，所以规则只写
 * 在这里一次，各页只声明自己的兜底句和显示方式，不再各抄一份 `instanceof` 判断。
 */
export function describeError(error: unknown, options: ErrorTextOptions): string {
  const { fallback, localMessage = 'hide', prefix } = options
  if (error instanceof ApiError) return error.message
  if (localMessage !== 'hide' && error instanceof Error && error.message) {
    return localMessage === 'raw' ? error.message : `${prefix ?? fallback}：${error.message}`
  }
  return fallback
}


