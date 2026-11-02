import type { JobStatus } from '../types/common'

const ACTIVE_STATUSES = new Set<JobStatus>(['queued', 'running'])
const TERMINAL_STATUSES = new Set<JobStatus>(['completed', 'failed', 'cancelled'])

export function isActiveJobStatus(status: JobStatus | string | null | undefined): boolean {
  return typeof status === 'string' && ACTIVE_STATUSES.has(status as JobStatus)
}

export function isTerminalJobStatus(status: JobStatus | string | null | undefined): boolean {
  return typeof status === 'string' && TERMINAL_STATUSES.has(status as JobStatus)
}
