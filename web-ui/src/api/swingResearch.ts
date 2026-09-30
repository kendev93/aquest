import type {
  BacktestHistorySummary,
  SwingOverviewResult,
  SwingStudyResult,
} from '../types'

const API_BASE = '/api/v1/swing-research'

interface ApiErrorPayload {
  error?: { message?: string }
  detail?: string
}

/**
 * 历史记录里存的研究请求：第二步的参数，外加第一步的表单与结果快照。
 *
 * 第一步的校准只跑在浏览器里，不一起存档的话，加载历史后第一步的表格会是空的。
 */
export interface SwingStoredRequest {
  universe?: 'all' | 'sh' | 'sz'
  start_date?: string | null
  end_date?: string | null
  reversal_multiple?: number
  min_bars?: number
  atr_window?: number
  horizon?: number
  labels?: string[]
  include_custom?: boolean
  min_cross_section?: number
  permutations?: number
  block_size?: number
  alpha?: number
  limit_stocks?: number | null
  overview_request?: Record<string, unknown> | null
  overview_result?: SwingOverviewResult | null
}

export interface SwingHistoryRecord extends BacktestHistorySummary {
  request: SwingStoredRequest
  result: SwingStudyResult
}

export class SwingResearchApiError extends Error {
  constructor(message: string, public readonly status: number) {
    super(message)
    this.name = 'SwingResearchApiError'
  }
}

export interface SwingOverviewPayload {
  universe?: 'all' | 'sh' | 'sz'
  start_date?: string | null
  end_date?: string | null
  reversal_multiples?: number[]
  min_bars?: number
  atr_window?: number
  include_pending?: boolean
  limit_stocks?: number | null
}

/** 一次研究的进度。后端按「读日历 → 填面板 → 逐日打分」三段报，stage 直接显示。 */
export interface SwingProgress {
  current: number
  total: number
  stage: string
  /** 0~1，前端画进度条用。 */
  ratio: number
}

interface StreamEvent {
  type: 'progress' | 'result' | 'error'
  current?: number
  total?: number
  stage?: string
  data?: unknown
  detail?: string
  /** 只有第二步有：这次研究被存成的历史记录 id。 */
  history_id?: string
}

/** 一次流式调用的产出：结果本体，外加后端自动存档的历史 id（第一步没有）。 */
interface StreamOutcome<T> {
  data: T
  historyId: string | null
}

/**
 * 读一条 SSE 流：一路回调进度，最后返回结果。
 *
 * 用 fetch 而不是 EventSource —— 研究参数是 POST 的，EventSource 只发 GET。
 * 后端返回的是 `data: {...}\n\n` 帧，可能一次收到半帧或好几帧，所以要攒 buffer 按空行切。
 */
async function streamRequest<T>(
  path: string,
  payload: SwingOverviewPayload | SwingStudyPayload,
  onProgress?: (progress: SwingProgress) => void,
): Promise<StreamOutcome<T>> {
  const response = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  if (!response.ok) {
    const failed = (await response.json().catch(() => ({}))) as ApiErrorPayload
    throw new SwingResearchApiError(
      failed.error?.message ?? failed.detail ?? '波段研究请求失败，请检查参数后重试。',
      response.status,
    )
  }
  // 老镜像还没有流式端点：拿到的就不是事件流，退回一次性 JSON。
  if (!(response.headers.get('content-type') ?? '').includes('text/event-stream')) {
    const plain = (await response.json()) as { data: T; history_id?: string }
    return { data: plain.data, historyId: plain.history_id ?? null }
  }

  const reader = (response.body as ReadableStream<Uint8Array>).getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let result: T | null = null
  let historyId: string | null = null
  let failure: string | null = null

  const handle = (frame: string): void => {
    const line = frame.split('\n').find((item) => item.startsWith('data: '))
    if (!line) return
    const event = JSON.parse(line.slice('data: '.length)) as StreamEvent
    if (event.type === 'progress') {
      const current = event.current ?? 0
      const total = Math.max(event.total ?? 0, 1)
      onProgress?.({
        current,
        total,
        stage: event.stage ?? '计算中',
        ratio: Math.min(Math.max(current / total, 0), 1),
      })
    } else if (event.type === 'result') {
      result = event.data as T
      historyId = typeof event.history_id === 'string' && event.history_id ? event.history_id : null
    } else if (event.type === 'error') {
      failure = event.detail ?? '波段研究失败，请检查参数后重试。'
    }
  }

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let split = buffer.indexOf('\n\n')
    while (split >= 0) {
      handle(buffer.slice(0, split))
      buffer = buffer.slice(split + 2)
      split = buffer.indexOf('\n\n')
    }
  }
  handle(buffer)

  if (failure !== null) throw new SwingResearchApiError(failure, 400)
  if (result === null) throw new SwingResearchApiError('波段研究没有返回结果，请重试。', 500)
  return { data: result, historyId }
}

/** 第一步：把行情切成波段，看看几个候选阈值分别切出多少段、每段走多远。 */
export async function runSwingOverview(
  payload: SwingOverviewPayload = {},
  onProgress?: (progress: SwingProgress) => void,
): Promise<SwingOverviewResult> {
  // 第一步只是校准，不进历史列表。
  return (await streamRequest<SwingOverviewResult>('/overview/stream', payload, onProgress)).data
}

export interface SwingStudyPayload {
  universe?: 'all' | 'sh' | 'sz'
  start_date?: string | null
  end_date?: string | null
  reversal_multiple?: number
  min_bars?: number
  atr_window?: number
  horizon?: number
  labels?: string[]
  /** 是否研究白名单自定义指标，包括主力清洗、庄家顶底、准备拉升、主力吸筹和突破信号。 */
  include_custom?: boolean
  min_cross_section?: number
  permutations?: number
  block_size?: number
  alpha?: number
  limit_stocks?: number | null
  /**
   * 存档用，不参与计算。第一步（切分校准）的表单与结果只存在于浏览器里，
   * 不一起带过来的话，加载历史后第一步的表格会是空的。
   */
  overview_request?: Record<string, unknown> | null
  overview_result?: SwingOverviewResult | null
}

/**
 * 第二步：把无量纲指标和波段标签逐日做横截面打分。
 * 走流式接口，全市场跑一次是分钟级，中途会不断回调进度。
 *
 * 跑完后端会自动存一条历史，id 随结果一起回来，页面据此刷新历史列表。
 */
export async function runSwingIndicatorStudy(
  payload: SwingStudyPayload = {},
  onProgress?: (progress: SwingProgress) => void,
): Promise<{ result: SwingStudyResult; historyId: string | null }> {
  const outcome = await streamRequest<SwingStudyResult>(
    '/indicator-study/stream',
    payload,
    onProgress,
  )
  return { result: outcome.data, historyId: outcome.historyId }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
    })
  } catch {
    throw new SwingResearchApiError('无法连接波段研究服务，请确认后端已启动。', 0)
  }
  const payload = (await response.json().catch(() => ({}))) as T & ApiErrorPayload
  if (!response.ok) {
    throw new SwingResearchApiError(
      payload.error?.message ?? payload.detail ?? '波段研究历史请求失败，请重试。',
      response.status,
    )
  }
  return payload as T
}

/** 历史列表：后端已按完成时间由近到远排好，这里只管渲染。 */
export async function fetchSwingResearchHistory(): Promise<BacktestHistorySummary[]> {
  const response = await request<{ data: BacktestHistorySummary[] }>('/history')
  return response.data
}

export async function getSwingResearchHistory(historyId: string): Promise<SwingHistoryRecord> {
  const response = await request<{ data: SwingHistoryRecord }>(
    '/history/' + encodeURIComponent(historyId),
  )
  return response.data
}

export async function deleteSwingResearchHistory(historyId: string): Promise<boolean> {
  const response = await request<{ data: { deleted: boolean } }>(
    '/history/' + encodeURIComponent(historyId),
    { method: 'DELETE' },
  )
  return response.data.deleted
}
