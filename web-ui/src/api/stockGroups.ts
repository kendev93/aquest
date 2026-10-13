
import { ApiError, createRequestClient } from './client'
import type {
  StockGroup,
  StockGroupCriteria,
  StockGroupMember,
  StockGroupPerformance,
  StockGroupsSummary,
  StockGroupSummary,
} from '../types'

const API_BASE = '/api/v1/stock-groups'

const request = createRequestClient({
  errorClass: ApiError,
  messages: { fallback: '股票组请求失败，请稍后重试。' },
})

export async function fetchStockGroups(): Promise<StockGroupSummary[]> {
  const response = await request<{ data: StockGroupSummary[] }>(API_BASE)
  return response.data
}

export async function createStockGroup(payload: {
  name: string
  note?: string
  items: StockGroupMember[]
  source?: 'manual' | 'screen'
  source_history_id?: string | null
  criteria?: { summary: string; payload: Record<string, unknown> | null } | null
}): Promise<StockGroup> {
  const response = await request<{ data: StockGroup }>(API_BASE, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  return response.data
}

export async function getStockGroup(groupId: string): Promise<StockGroup> {
  const response = await request<{ data: StockGroup }>(
    `${API_BASE}/${encodeURIComponent(groupId)}`,
  )
  return response.data
}

export async function renameStockGroup(groupId: string, name: string): Promise<StockGroup> {
  const response = await request<{ data: StockGroup }>(
    `${API_BASE}/${encodeURIComponent(groupId)}`,
    { method: 'PATCH', body: JSON.stringify({ name }) },
  )
  return response.data
}

/** 折叠态的阶段收益。一次拿回所有组——不是每组一个请求。 */
export async function fetchStockGroupsSummary(
  windows?: number[],
): Promise<StockGroupsSummary> {
  const query = windows && windows.length ? `?windows=${windows.join(',')}` : ''
  const response = await request<{ data: StockGroupsSummary }>(`${API_BASE}/summary${query}`)
  return response.data
}

/** 存组时的筛选条件快照。给 null 表示「这组本来就没有筛选条件」。 */
export type StockGroupCriteriaInput = Pick<StockGroupCriteria, 'summary' | 'payload'>

export async function removeStockGroupMembers(
  groupId: string,
  members: StockGroupMember[],
): Promise<StockGroup> {
  const response = await request<{ data: StockGroup }>(
    `${API_BASE}/${encodeURIComponent(groupId)}/members/remove`,
    { method: 'POST', body: JSON.stringify({ members }) },
  )
  return response.data
}

export async function deleteStockGroup(groupId: string): Promise<boolean> {
  const response = await request<{ data: { deleted: boolean } }>(
    `${API_BASE}/${encodeURIComponent(groupId)}`,
    { method: 'DELETE' },
  )
  return response.data.deleted
}

/** 入组至今的等权买入持有曲线 + 同期全市场等权对照。最坏几秒，调用方要给加载态。 */
export async function fetchStockGroupPerformance(
  groupId: string,
): Promise<StockGroupPerformance> {
  const response = await request<{ data: StockGroupPerformance }>(
    `${API_BASE}/${encodeURIComponent(groupId)}/performance`,
  )
  return response.data
}
