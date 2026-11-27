import { expect, it, vi } from 'vitest'

import { runSignalBackfill } from './signalBackfill'

it('replays missing days in order and reports progress', async () => {
  const submitted: string[] = []
  const progress: string[] = []
  const outcome = await runSignalBackfill(['2026-11-24', '2026-11-25', '2026-11-26'], {
    submit: async (day) => {
      submitted.push(day)
      return `job-${day}`
    },
    poll: vi.fn(async () => 'completed' as const),
    onProgress: (done, total) => progress.push(`${done}/${total}`),
  })

  expect(outcome).toBe('completed')
  expect(submitted).toEqual(['2026-11-24', '2026-11-25', '2026-11-26'])
  expect(progress).toEqual(['1/3', '2/3', '3/3'])
})

it('stops submitting after a failed or cancelled day', async () => {
  const submitted: string[] = []
  const poll = vi.fn(async () => 'failed' as const)
  const outcome = await runSignalBackfill(['2026-11-24', '2026-11-25'], {
    submit: async (day) => {
      submitted.push(day)
      return `job-${day}`
    },
    poll,
  })

  expect(outcome).toBe('failed')
  expect(submitted).toEqual(['2026-11-24'])
  expect(poll).toHaveBeenCalledTimes(1)

  const cancelled = await runSignalBackfill(['2026-11-24'], {
    submit: async () => 'job',
    poll: async () => 'cancelled',
  })
  expect(cancelled).toBe('cancelled')
})

it('does nothing when there is no gap', async () => {
  const submit = vi.fn(async () => 'job')
  const outcome = await runSignalBackfill([], { submit, poll: async () => 'completed' })
  expect(outcome).toBe('completed')
  expect(submit).not.toHaveBeenCalled()
})