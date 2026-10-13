/**
 * 轮询的两个标准形态。
 *
 * 以前 9 处任务轮询各自手写：await 式的「提交任务后等它结束」散在 5 个页面的
 * for(;;) 循环里，页面级的「持续刷新」在 MarketDataPage 里靠自调度 setTimeout
 * 加手写卸载守卫。间隔（250/500/1000/1500/2000/5000）各处不同，停止条件互相
 * 抄又不完全一致，状态读取失败时有的页重试、有的页轮询直接死掉，
 * 还出现过「卸载时清理了一个从未赋值的定时器」这种无效清理
 * （await 式轮询根本没有挂到组件生命周期上）。
 *
 * - `pollUntil`：await 式。fn 是调用方的闭包，每轮副作用（写 ref、发消息）
 *   写在里面；`until` 决定何时结束；`until` 或 fn 抛错即中止并向上传播。
 * - `useBackgroundPoll`：setup 式。组件卸载自动停；单轮失败不终止轮询——
 *   后台刷新死于一次网络抖动是最糟的失败模式。
 *
 * 例外：StrategyPoolPage 的任务轮询内嵌「标的状态刷新」编排和 token 竞态
 * 守卫（canApply），硬塞进这里的抽象只会把参数越加越多，保留手写。
 */

import { getCurrentInstance, onUnmounted } from 'vue'

/** `timeoutMs` 预算耗尽时抛出；调用方用它区分「超时」和「任务失败」。 */
export class PollTimeoutError extends Error {
  constructor(message = '轮询超时。') {
    super(message)
    this.name = 'PollTimeoutError'
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms))
}

export interface PollUntilOptions<T> {
  /** 相邻两轮之间的间隔：从上一轮 fn 完成（或重试等待结束）后开始计。 */
  intervalMs: number
  /**
   * 每轮结果判定：返回 true 则结束并 resolve 最后一次结果；
   * 在这里抛错同样中止轮询并向上传播。attempt 从 1 开始。
   */
  until: (result: T, attempt: number) => boolean
  /** 可选总预算（毫秒）。到点抛 PollTimeoutError；不设就无限等。 */
  timeoutMs?: number
  /**
   * 瞬时错误重试：返回这一轮失败后要先等的毫秒数；返回 null 表示致命错误，
   * 原样向上抛。failureCount 从 1 开始（第一次失败就是 1）。
   * 适合「进度读取暂时失败，但任务还在后台跑」的场景。
   */
  retryableError?: (error: unknown, failureCount: number) => number | null
}

export async function pollUntil<T>(
  fn: () => Promise<T>,
  options: PollUntilOptions<T>,
): Promise<T> {
  const deadline = options.timeoutMs === undefined ? null : Date.now() + options.timeoutMs
  let attempt = 0
  let failures = 0
  for (;;) {
    if (deadline !== null && Date.now() >= deadline) throw new PollTimeoutError()
    attempt += 1
    let result: T
    try {
      result = await fn()
      failures = 0
    } catch (error) {
      if (options.retryableError) {
        failures += 1
        const delay = options.retryableError(error, failures)
        if (delay !== null) {
          await sleep(delay)
          continue
        }
      }
      throw error
    }
    if (options.until(result, attempt)) return result
    await sleep(options.intervalMs)
  }
}

export interface BackgroundPollOptions {
  intervalMs: number
  /** 返回 false 时跳过这一轮，但继续按 interval 调度（例如同步进行中时让路）。 */
  shouldRun?: () => boolean
  /** 单轮失败的观察口。不提供时失败被静默跳过——fn 应当自己兜住错误。 */
  onError?: (error: unknown) => void
}

/**
 * 页面级后台轮询：挂载后按 interval 周期运行，组件卸载自动停。
 * 要在 setup 的同步路径上调用（自动注册 onUnmounted 依赖这一点）；
 * 在别的上下文使用时，用返回的 stop 函数手动停。
 */
export function useBackgroundPoll(
  fn: () => Promise<unknown>,
  options: BackgroundPollOptions,
): () => void {
  let timer: number | null = null
  let stopped = false

  function schedule(): void {
    if (stopped || timer !== null) return
    timer = window.setTimeout(() => {
      timer = null
      void tick()
    }, options.intervalMs)
  }

  async function tick(): Promise<void> {
    if (stopped) return
    if (!options.shouldRun || options.shouldRun()) {
      try {
        await fn()
      } catch (error) {
        if (options.onError) options.onError(error)
      }
    }
    schedule()
  }

  function stop(): void {
    stopped = true
    if (timer !== null) window.clearTimeout(timer)
    timer = null
  }

  schedule()
  if (getCurrentInstance() !== null) onUnmounted(stop)
  return stop
}
