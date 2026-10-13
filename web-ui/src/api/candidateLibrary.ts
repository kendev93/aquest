
import { ApiError, createRequestClient } from './client'
import type {
  CandidateLibraryItem,
  CandidatePublishOutcome,
  CandidatePublishPreview,
  CandidateStatus,
} from '../types'

const API_BASE = '/api/v1/candidate-library'

const request = createRequestClient({
  errorClass: ApiError,
  messages: { fallback: '候选库请求失败，请稍后重试。' },
})

export async function fetchCandidates(status?: CandidateStatus): Promise<CandidateLibraryItem[]> {
  const query = status ? `?status=${encodeURIComponent(status)}` : ''
  const response = await request<{ data: { items: CandidateLibraryItem[] } }>(
    `${API_BASE}${query}`,
  )
  return response.data.items
}

export async function importCandidates(payload: {
  history_id: string
  candidate_ids: string[]
  note?: string
}): Promise<{ added: CandidateLibraryItem[]; skipped: { candidate_id: string; reason: string }[] }> {
  const response = await request<{
    data: {
      added: CandidateLibraryItem[]
      skipped: { candidate_id: string; reason: string }[]
    }
  }>(`${API_BASE}/import`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  return response.data
}

export async function updateCandidateStatus(
  candidateId: string,
  payload: { status: CandidateStatus; note?: string | null },
): Promise<CandidateLibraryItem> {
  const response = await request<{ data: CandidateLibraryItem }>(
    `${API_BASE}/${encodeURIComponent(candidateId)}`,
    {
      method: 'PATCH',
      body: JSON.stringify(payload),
    },
  )
  return response.data
}

export async function deleteCandidate(candidateId: string): Promise<{ deleted: boolean }> {
  const response = await request<{ data: { deleted: boolean } }>(
    `${API_BASE}/${encodeURIComponent(candidateId)}`,
    { method: 'DELETE' },
  )
  return response.data
}

/** 发布前先看清楚要写什么——「显式确认」是让人看见，不是弹个窗问确定吗。 */
export async function fetchPublishPreview(
  candidateId: string,
): Promise<CandidatePublishPreview> {
  const response = await request<{ data: CandidatePublishPreview }>(
    `${API_BASE}/${encodeURIComponent(candidateId)}/publish-preview`,
  )
  return response.data
}

export async function publishCandidate(
  candidateId: string,
  payload: {
    name: string
    note?: string
    ranking_value?: string | null
    rank_order?: 'asc' | 'desc' | null
    max_positions?: number | null
    initial_cash?: number | null
    stop_loss_pct?: number | null
    take_profit_pct?: number | null
  },
): Promise<CandidatePublishOutcome> {
  const response = await request<{ data: CandidatePublishOutcome }>(
    `${API_BASE}/${encodeURIComponent(candidateId)}/publish`,
    {
      method: 'POST',
      body: JSON.stringify(payload),
    },
  )
  return response.data
}
