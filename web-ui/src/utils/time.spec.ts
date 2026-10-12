import { describe, expect, it } from 'vitest'

import { formatChinaTimestamp } from './time'

describe('formatChinaTimestamp', () => {
  it('formats UTC timestamps as China Standard Time', () => {
    expect(formatChinaTimestamp('2026-09-11 06:57:45')).toBe('2026-09-11 14:57:45')
    expect(formatChinaTimestamp('2026-09-11T06:57:45Z')).toBe('2026-09-11 14:57:45')
  })

  it('preserves invalid values for diagnostics', () => {
    expect(formatChinaTimestamp('not-a-timestamp')).toBe('not-a-timestamp')
  })
})
