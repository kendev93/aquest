import { mount } from '@vue/test-utils'
import { defineComponent, h } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { PollTimeoutError, pollUntil, useBackgroundPoll } from './usePolling'

afterEach(() => {
  vi.useRealTimers()
})

describe('pollUntil', () => {
  it('resolves with the final result once until says so', async () => {
    vi.useFakeTimers()
    let calls = 0
    const promise = pollUntil(async () => {
      calls += 1
      return calls
    }, {
      intervalMs: 100,
      until: (n) => n >= 3,
    })
    await vi.advanceTimersByTimeAsync(250)
    await expect(promise).resolves.toBe(3)
    expect(calls).toBe(3)
  })

  it('aborts when until throws', async () => {
    vi.useFakeTimers()
    const promise = pollUntil(async () => ({ status: 'failed' }), {
      intervalMs: 100,
      until: (state) => {
        if (state.status === 'failed') throw new Error('任务失败')
        return true
      },
    })
    // rejection 断言要先挂上再推进时钟，否则 promise 会先一步变成 unhandled。
    const assertion = expect(promise).rejects.toThrow('任务失败')
    await vi.advanceTimersByTimeAsync(0)
    await assertion
  })

  it('throws PollTimeoutError when the budget runs out', async () => {
    vi.useFakeTimers()
    const promise = pollUntil(async () => Date.now(), {
      intervalMs: 1_000,
      timeoutMs: 5_000,
      until: () => false,
    })
    const assertion = expect(promise).rejects.toBeInstanceOf(PollTimeoutError)
    await vi.advanceTimersByTimeAsync(30_000)
    await assertion
  })

  it('enforces the budget even when a single attempt never settles', async () => {
    // fetch 没有默认超时：状态读取挂住时，只在整个循环开始时查预算等于没查。
    vi.useFakeTimers()
    const promise = pollUntil(() => new Promise<number>(() => undefined), {
      intervalMs: 1_000,
      timeoutMs: 5_000,
      until: () => false,
    })
    const assertion = expect(promise).rejects.toBeInstanceOf(PollTimeoutError)
    await vi.advanceTimersByTimeAsync(6_000)
    await assertion
  })

  it('clears the deadline timer once an attempt settles', async () => {
    vi.useFakeTimers()
    const promise = pollUntil(async () => 'ok', {
      intervalMs: 100,
      timeoutMs: 5_000,
      until: () => true,
    })
    await vi.advanceTimersByTimeAsync(0)
    await expect(promise).resolves.toBe('ok')
    expect(vi.getTimerCount()).toBe(0)
  })

  it('retries transient failures with the caller-chosen delay and resets on success', async () => {
    vi.useFakeTimers()
    let calls = 0
    const failureNumbers: number[] = []
    const promise = pollUntil(async () => {
      calls += 1
      if (calls <= 2) throw new Error('transient')
      return 'ok'
    }, {
      intervalMs: 100,
      until: (result) => result === 'ok',
      retryableError: (_error, failureCount) => {
        failureNumbers.push(failureCount)
        return failureCount <= 2 ? 50 : null
      },
    })
    await vi.advanceTimersByTimeAsync(1_000)
    await expect(promise).resolves.toBe('ok')
    expect(failureNumbers).toEqual([1, 2])
  })

  it('gives up when retryableError returns null', async () => {
    vi.useFakeTimers()
    const promise = pollUntil(async () => {
      throw new Error('fatal')
    }, {
      intervalMs: 100,
      until: () => false,
      retryableError: () => null,
    })
    const assertion = expect(promise).rejects.toThrow('fatal')
    await vi.advanceTimersByTimeAsync(0)
    await assertion
  })

  it('does not retry when no retryableError is configured', async () => {
    vi.useFakeTimers()
    let calls = 0
    const promise = pollUntil(async () => {
      calls += 1
      throw new Error('boom')
    }, {
      intervalMs: 100,
      until: () => false,
    })
    const assertion = expect(promise).rejects.toThrow('boom')
    await vi.advanceTimersByTimeAsync(0)
    await assertion
    expect(calls).toBe(1)
  })
})

describe('useBackgroundPoll', () => {
  /** 在一个真实组件的 setup 里跑 composable，这样 onUnmounted 注册路径是真实的。 */
  function TestBody(setup: () => void) {
    return defineComponent({
      setup() {
        setup()
        return () => h('div')
      },
    })
  }

  it('runs on the interval and stops when the component unmounts', async () => {
    vi.useFakeTimers()
    let runs = 0
    const wrapper = mount(TestBody(() => {
      useBackgroundPoll(async () => {
        runs += 1
      }, { intervalMs: 100 })
    }))
    await vi.advanceTimersByTimeAsync(350)
    expect(runs).toBe(3)
    wrapper.unmount()
    await vi.advanceTimersByTimeAsync(1_000)
    expect(runs).toBe(3)
  })

  it('skips a round when shouldRun returns false but keeps scheduling', async () => {
    vi.useFakeTimers()
    let runs = 0
    let allowed = false
    mount(TestBody(() => {
      useBackgroundPoll(async () => {
        runs += 1
      }, { intervalMs: 100, shouldRun: () => allowed })
    }))
    await vi.advanceTimersByTimeAsync(250)
    expect(runs).toBe(0)
    allowed = true
    // 只推进到下一个 tick（t=300）；再多推 100ms 就会跨到再下一轮。
    await vi.advanceTimersByTimeAsync(50)
    expect(runs).toBe(1)
  })

  it('keeps polling after a failed round', async () => {
    vi.useFakeTimers()
    let runs = 0
    mount(TestBody(() => {
      useBackgroundPoll(async () => {
        runs += 1
        if (runs === 1) throw new Error('boom')
      }, { intervalMs: 100, onError: () => undefined })
    }))
    await vi.advanceTimersByTimeAsync(250)
    expect(runs).toBe(2)
  })

  it('can be stopped manually outside of a component', async () => {
    vi.useFakeTimers()
    let runs = 0
    const stop = useBackgroundPoll(async () => {
      runs += 1
    }, { intervalMs: 100 })
    await vi.advanceTimersByTimeAsync(150)
    expect(runs).toBe(1)
    stop()
    await vi.advanceTimersByTimeAsync(500)
    expect(runs).toBe(1)
  })
})
