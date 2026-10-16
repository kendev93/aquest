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
 *   交互型短任务用 `timeoutMs` 设预算（见 `INTERACTIVE_JOB_TIMEOUT_MS`），
 *   小时级的全量任务有意不设——预算到点只会停止等待，不会取消后台任务。
 * - `useBackgroundPoll`：setup 式。组件卸载自动停；单轮失败不终止轮询——
 *   后台刷新死于一次网络抖动是最糟的失败模式。
 *
 * 例外：StrategyPoolPage 的任务轮询内嵌「标的状态刷新」编排和 token 竞态
 * 守卫（canApply），硬塞进这里的抽象只会把参数越加越多，保留手写。
 */

import { getCurrentInstance, onUnmounted } from 'vue'

import { ApiError } from '../api/client'

/** `timeoutMs` 预算耗尽时抛出；调用方用它区分「超时」和「任务失败」。超时只停止前端等待，后台任务照常跑完。 */
export class PollTimeoutError extends Error {
  constructor(message = '等待任务结束超时，任务仍在后台运行。') {
    super(message)
    this.name = 'PollTimeoutError'
  }
}

/**
 * 「这是任务自己的终态，不该当成读状态失败去重试」的标记。
 *
 * `until` 里判出任务失败时抛的就是它：不区分的話，配了 `retryableError` 的轮询会把
 * 一次真失败当成网络抖动重试十次，失败原因永远到不了用户面前。继承 ApiError 是为了
 * 让 `describeError` 照旧把 message 原样显示出来。
 */
export class PollFatalError extends ApiError {
  constructor(message: string, status = 500) {
    super(message, status)
    this.name = 'PollFatalError'
  }
}

/**
 * 交互型短任务的等待预算：个股研究、因子检验、因子归因、行情体检、公式选股。
 *
 * 这些任务正常是秒级到分钟级，用户盯着进度条等；超过 15 分钟基本是后端卡住或
 * 排到了长任务，继续空等只会把页面锁死在「运行中」。
 * 全量行情同步和策略实验室是小时级任务，有意不设预算——见 MarketDataPage 的
 * 「没有固定尝试上限」测试。
 */
export const INTERACTIVE_JOB_TIMEOUT_MS = 15 * 60 * 1000

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms))
}

/**
 * 跑一轮，但到点就撤——预算必须真的能终止等待。
 *
 * fetch 没有默认超时，某一次状态读取挂住时只在循环开头查预算等于没查：页面会一直
 * 卡在「运行中」。让这一轮和截止时刻赛跑，超时先到；悬挂的那一轮已经被 race 订阅过，
 * 之后失败也不会变成未处理的 rejection，结果直接丢掉即可。
 */
async function runWithinBudget<T>(fn: () => Promise<T>, deadline: number | null): Promise<T> {
  if (deadline === null) return fn()
  const remainingMs = deadline - Date.now()
  if (remainingMs <= 0) throw new PollTimeoutError()
  let timer: number | undefined
  try {
    return await Promise.race([
      fn(),
      new Promise<never>((_resolve, reject) => {
        timer = window.setTimeout(() => reject(new PollTimeoutError()), remainingMs)
      }),
    ])
  } finally {
    if (timer !== undefined) window.clearTimeout(timer)
  }
}

export interface PollUntilOptions<T> {
  /** 相邻两轮之间的间隔：从上一轮 fn 完成（或重试等待结束）后开始计。 */
  intervalMs: number
  /**
   * 每轮结果判定：返回 true 则结束并 resolve 最后一次结果；
   * 在这里抛错同样中止轮询并向上传播。attempt 从 1 开始。
   */
  until: (result: T, attempt: number) => boolean
  /** 可选总预算（毫秒）。到点抛 PollTimeoutError，卡住的单轮也会被撤掉；不设就无限等。 */
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
      result = await runWithinBudget(fn, deadline)
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
