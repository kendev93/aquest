import type { ActiveJobState } from '../types'

import { ApiError, createRequestClient } from './client'

const API_BASE = '/api/v1/jobs'

const request = createRequestClient({
  errorClass: ApiError,
  messages: { fallback: '任务状态读取失败，请稍后重试。' },
})

export async function fetchActiveJobs(): Promise<ActiveJobState[]> {
  const response = await request<{ data: ActiveJobState[] }>(`${API_BASE}/active`)
  return response.data
}
