export type SignalBackfillOutcome = 'completed' | 'failed' | 'cancelled'

export interface SignalBackfillDeps {
  /** 提交某一天的信号回放任务，返回任务 id。 */
  submit: (day: string) => Promise<string>
  /** 等这个任务到终态。 */
  poll: (jobId: string) => Promise<SignalBackfillOutcome>
  onProgress?: (done: number, total: number) => void
}

/** 逐日回放缺失交易日；任一天失败或取消就停，后面的日子不再提交。 */
export async function runSignalBackfill(
  days: string[],
  deps: SignalBackfillDeps,
): Promise<SignalBackfillOutcome> {
  for (let index = 0; index < days.length; index += 1) {
    const jobId = await deps.submit(days[index]!)
    const outcome = await deps.poll(jobId)
    if (outcome !== 'completed') return outcome
    deps.onProgress?.(index + 1, days.length)
  }
  return 'completed'
}