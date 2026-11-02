import { describe, expect, it } from 'vitest'

import { isActiveJobStatus, isTerminalJobStatus } from './jobStatus'

describe('jobStatus', () => {
  it('classifies active and terminal statuses', () => {
    expect(isActiveJobStatus('queued')).toBe(true)
    expect(isActiveJobStatus('running')).toBe(true)
    expect(isActiveJobStatus('completed')).toBe(false)
    expect(isTerminalJobStatus('completed')).toBe(true)
    expect(isTerminalJobStatus('failed')).toBe(true)
    expect(isTerminalJobStatus('cancelled')).toBe(true)
    expect(isTerminalJobStatus('running')).toBe(false)
    expect(isTerminalJobStatus(null)).toBe(false)
  })
})
