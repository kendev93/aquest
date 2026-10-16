/**
 * 刷新页面后接管仍在运行的后台任务：查活动任务 → 校验形状 → 交给页面 → 继续轮询。
 *
 * 这段逻辑此前在八个页面里各抄一份，每份都靠 `as unknown as 页面自己的状态类型`
 * 把后端返回的通用任务形状硬塞进去；而 `ActiveJobState` 里 `result`、`skipped`
 * 等字段都是可选的，页面类型却按必填来读——刷新一下就装上了没校验过的形状。
 * 收口到这里之后，运行时校验只做一次，页面剩下的那一行断言有明确的依据。
 */

import { fetchActiveJobs } from '../api/jobs'
import type { ActiveJobState } from '../types/market'

export interface ActiveJobResumeOptions {
  /** 后端写进任务记录的描述文本，用来认出这是本页面的任务。 */
  descriptions: readonly string[]
  /** 页面接管任务：把校验过的通用形状落成页面自己的状态。 */
  adopt: (job: ActiveJobState) => void
  /** 从任务的当前状态继续轮询到结束。 */
  poll: (job: ActiveJobState) => Promise<void>
  /** 后端错误 → 用户可读文案，写页面顶部的消息条。 */
  reportError: (error: unknown) => void
  /** 轮询期间的加载态；不传表示页面没有这条状态。 */
  setLoading?: (loading: boolean) => void
  /**
   * 接管那一刻页面就会读的字段。注意别填成页面类型的全部必填字段：`/jobs/active`
   * 只给通用任务形状，`result` 这类要等 poll 拉到详情才有，校验过严会把恢复打断。
   * 缺任何一个就不接管，而不是让它以 undefined 进页面状态。
   */
  requiredFields?: readonly string[]
}

/** 页面状态类型比通用形状更严，校验过字段之后的这次登记式放宽集中在这里。 */
export function adoptActiveJob<T>(job: ActiveJobState): T {
  return job as unknown as T
}

function missingFields(job: ActiveJobState, requiredFields: readonly string[]): string[] {
  const record = job as unknown as Record<string, unknown>
  return requiredFields.filter((field) => !(field in record))
}

export function useActiveJobResume(options: ActiveJobResumeOptions) {
  async function resumeActiveJob(): Promise<void> {
    try {
      const active = (await fetchActiveJobs()).find((item) =>
        options.descriptions.includes(item.description),
      )
      if (!active) return
      const missing = missingFields(active, options.requiredFields ?? [])
      if (missing.length) {
        throw new Error(`后台任务缺少页面需要的字段：${missing.join('、')}`)
      }
      options.adopt(active)
      options.setLoading?.(true)
      await options.poll(active)
    } catch (error) {
      options.reportError(error)
    } finally {
      options.setLoading?.(false)
    }
  }

  return { resumeActiveJob }
}
