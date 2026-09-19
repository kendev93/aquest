import type { SwingOverviewResult, SwingStudyResult } from '../types'

const API_BASE = '/api/v1/swing-research'

interface ApiErrorPayload {
  error?: { message?: string }
  detail?: string
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
): Promise<T> {
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
    const plain = (await response.json()) as { data: T }
    return plain.data
  }

  const reader = (response.body as ReadableStream<Uint8Array>).getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let result: T | null = null
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
  return result
}

/** 第一步：把行情切成波段，看看几个候选阈值分别切出多少段、每段走多远。 */
export async function runSwingOverview(
  payload: SwingOverviewPayload = {},
  onProgress?: (progress: SwingProgress) => void,
): Promise<SwingOverviewResult> {
  return streamRequest<SwingOverviewResult>('/overview/stream', payload, onProgress)
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
  /** 是否把三个自定义公式（主力清洗 / 庄家顶底 / 准备拉升）的连续输出一起研究。 */
  include_custom?: boolean
  min_cross_section?: number
  permutations?: number
  block_size?: number
  alpha?: number
  limit_stocks?: number | null
}

/**
 * 第二步：把无量纲指标和波段标签逐日做横截面打分。
 * 走流式接口，全市场跑一次是分钟级，中途会不断回调进度。
 */
export async function runSwingIndicatorStudy(
  payload: SwingStudyPayload = {},
  onProgress?: (progress: SwingProgress) => void,
): Promise<SwingStudyResult> {
  return streamRequest<SwingStudyResult>('/indicator-study/stream', payload, onProgress)
}
