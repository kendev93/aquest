/**
 * 刷新页面后接管仍在运行的后台任务：查一次活动任务 → 校验形状 → 交给页面 → 继续轮询。
 *
 * 这段逻辑此前在八个页面里各抄一份，每份都靠 `as unknown as 页面自己的状态类型`
 * 把后端返回的通用任务形状硬塞进去；而 `ActiveJobState` 里 `result`、`skipped`
 * 等字段都是可选的，页面类型却按必填来读——刷新一下就装上了没校验过的形状。
 * 收口到这里之后，运行时校验只做一次，页面剩下的那一行断言有明确的依据。
 *
 * 一个页面可能有多类任务（策略实验室的寻优、二次优化，以及随后的全量复核），
 * 因此候选按顺序匹配、只接管第一个命中的：仍然只查一次活动任务，不会出现
 * 「前一页签查到 A、后一签查到 B」的错位。
 */

import { fetchActiveJobs } from '../api/jobs'
import type { ActiveJobState } from '../types/market'

export interface ActiveJobCandidate {
  /** 后端写进任务记录的描述文本，用来认出这是本页面的哪一类任务。 */
  descriptions: readonly string[]
  /** 页面接管任务：把校验过的通用形状落成页面自己的状态。需要先拉详情才能落状态
  的页面可以不传，自己在 `poll` 里完成。 */
  adopt?: (job: ActiveJobState) => void
  /** 从任务的当前状态继续轮询到结束。 */
  poll: (job: ActiveJobState) => Promise<void>
  /** 该类任务轮询期间的加载态；不传表示页面没有这条状态。 */
  setLoading?: (loading: boolean) => void
  /**
   * 接管那一刻页面就会读的字段。注意别填成页面类型的全部必填字段：`/jobs/active`
   * 只给通用任务形状，`result` 这类要等 poll 拉到详情才有，校验过严会把恢复打断。
   */
  requiredFields?: readonly string[]
}

export interface ActiveJobResumeOptions {
  /** 按顺序匹配，第一个命中的任务被接管。 */
  candidates: readonly ActiveJobCandidate[]
  /** 后端错误 → 用户可读文案，写页面顶部的消息条。 */
  reportError: (error: unknown) => void
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
  /** 返回是否接管到了任务，页面可据此决定要不要继续做后面的收尾。 */
  async function resumeActiveJob(): Promise<boolean> {
    try {
      const activeJobs = await fetchActiveJobs()
      for (const candidate of options.candidates) {
        const active = activeJobs.find((item) => candidate.descriptions.includes(item.description))
        if (!active) continue
        const missing = missingFields(active, candidate.requiredFields ?? [])
        if (missing.length) {
          throw new Error(`后台任务缺少页面需要的字段：${missing.join('、')}`)
        }
        candidate.adopt?.(active)
        candidate.setLoading?.(true)
        await candidate.poll(active)
        return true
      }
      return false
    } catch (error) {
      options.reportError(error)
      return false
    } finally {
      // 收口前每份实现都是「无论有没有接管到都清一次加载态」，多类任务的页面还会
      // 把自己所有加载态一起归位。保持这个语义，页面不会留下转个不停的圈。
      for (const candidate of options.candidates) {
        candidate.setLoading?.(false)
      }
    }
  }

  return { resumeActiveJob }
}
