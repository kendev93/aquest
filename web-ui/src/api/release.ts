import type { ReleaseStatus } from '../types'

import { ApiError, createRequestClient } from './client'

const request = createRequestClient({
  errorClass: ApiError,
  messages: { fallback: '上游版本状态读取失败。' },
})

/**
 * 读上游版本状态。后端只回缓存（检查由后台任务按天做），所以这里不会阻塞在网络请求上；
 * 读失败由调用方静默处理——离线部署不该在顶栏点一条每天出现的错误。
 */
export async function fetchReleaseStatus(): Promise<ReleaseStatus> {
  const response = await request<{ data: ReleaseStatus }>('/api/v1/release-status')
  return response.data
}
