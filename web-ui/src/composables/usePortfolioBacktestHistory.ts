/**
 * 回测历史：列表刷新、还原某次的输入配置、查看详情、删除。
 *
 * 从 PortfolioBacktestPage 拆出来。还原（applyHistoryRequest）深度写表单：
 * 覆盖规则/排序/风控/成本的通用回填复用页面的 applyStoredCandidate，
 * 日期模式、自定义公式、ATR 与适配性阈值在这里补齐——所以 form 以窄接口注入。
 * 错误写入独立的 historyError（历史面板内展示），不占用页面顶部消息条。
 */

import { ref } from 'vue'
import type { Ref } from 'vue'

import {
  deletePortfolioBacktestHistory,
  fetchPortfolioBacktestHistory,
  getPortfolioBacktestHistory,
} from '../api/portfolioBacktest'
import type { BacktestHistorySummary, PortfolioBacktestResult } from '../types'
import type { DateRangeMode } from '../types'

/** applyHistoryRequest 会写的表单字段（页面的 reactive form 是它的超集）。 */
export interface RestorablePortfolioForm {
  mode: 'preset' | 'custom'
  formulaText: string
  formulaParameters: Record<string, number>
  dateMode: DateRangeMode
  atrPeriod: number
  fitnessRankWeight: number
  fitnessMinTrades: number
  fitnessMaxDrawdown: number
}

export interface PortfolioBacktestHistoryDeps {
  form: RestorablePortfolioForm
  /** 页面顶部的消息条（详情载入/删除的结果写这里；列表失败写 historyError）。 */
  message: Ref<string>
  /** 后端错误 → 用户可读文案。 */
  describeError: (error: unknown) => string
  /** 历史里带自定义公式时先解析再回填：它会把买卖规则重置成公式默认输出。 */
  parseCustomFormula: () => Promise<void>
  /** 覆盖规则、排序、风控与成本的通用回填。 */
  applyStoredCandidate: (raw: unknown) => void
  /** 详情载入成功后页面要展示这份结果并清掉旧的复现比对。 */
  onResultRestored: (result: PortfolioBacktestResult) => void
}

export function usePortfolioBacktestHistory(deps: PortfolioBacktestHistoryDeps) {
  const history = ref<BacktestHistorySummary[]>([])
  const historyLoading = ref(false)
  const historyError = ref('')

  async function loadHistory(): Promise<void> {
    historyLoading.value = true
    historyError.value = ''
    try {
      history.value = await fetchPortfolioBacktestHistory()
    } catch (error) {
      historyError.value = error instanceof Error ? error.message : '历史组合回测读取失败。'
    } finally {
      historyLoading.value = false
    }
  }

  /**
   * 还原某次历史回测的输入。
   *
   * 复用策略实验室的回填（`applyStoredCandidate`）覆盖规则、排序、风控与成本，这里只补它
   * 不管的三样：**日期模式、自定义公式、ATR 与适配性阈值**。
   *
   * 顺序上有两条硬约束：
   *   1. 自定义公式**先解析再回填规则**。`parseCustomFormula` 会重置买卖规则与排序值，
   *      顺序反了这一份就白还原了。
   *   2. date_mode 必须和绝对日期一起还原。只还原日期不还原 mode，下拉会显示「最近五年」
   *      而实际按自定义区间跑。
   *
   * 加载不是重跑：页面上的结果仍是当时那一版，必须重新运行才会更新。
   */
  async function applyHistoryRequest(raw: unknown, notice: string): Promise<void> {
    if (!raw || typeof raw !== 'object') return
    const config = raw as Record<string, unknown>
    const formulaText = typeof config.formula_text === 'string' ? config.formula_text.trim() : ''
    if (formulaText) {
      deps.form.mode = 'custom'
      deps.form.formulaText = formulaText
      const parameters = config.formula_parameters
      if (parameters && typeof parameters === 'object') {
        const entries = Object.entries(parameters as Record<string, unknown>)
          .filter((entry): entry is [string, number] => typeof entry[1] === 'number' && Number.isFinite(entry[1]))
        deps.form.formulaParameters = Object.fromEntries(entries)
      }
      await deps.parseCustomFormula()
    } else {
      deps.form.mode = 'preset'
    }
    deps.applyStoredCandidate(config)
    if (config.date_mode === 'recent5' || config.date_mode === 'full' || config.date_mode === 'custom') {
      deps.form.dateMode = config.date_mode
    }
    const atrPeriod = finiteNumber(config.atr_period, Number.NaN)
    if (Number.isInteger(atrPeriod) && atrPeriod >= 1) deps.form.atrPeriod = atrPeriod
    deps.form.fitnessRankWeight = finiteNumber(config.fitness_rank_weight, deps.form.fitnessRankWeight)
    deps.form.fitnessMinTrades = finiteNumber(config.fitness_min_trades, deps.form.fitnessMinTrades)
    deps.form.fitnessMaxDrawdown = finiteNumber(config.fitness_max_drawdown, deps.form.fitnessMaxDrawdown / 100) * 100
    deps.message.value = notice
  }

  async function loadHistoryDetail(historyId: string): Promise<void> {
    try {
      const loaded = await getPortfolioBacktestHistory(historyId)
      deps.onResultRestored(loaded.result)
      await applyHistoryRequest(
        loaded.request,
        `已还原历史组合回测 ${historyId} 的输入配置，请确认后重新运行。`,
      )
    } catch (error) {
      deps.message.value = deps.describeError(error)
    }
  }

  async function removeHistory(historyId: string): Promise<void> {
    try {
      await deletePortfolioBacktestHistory(historyId)
      await loadHistory()
      deps.message.value = '历史组合回测已删除。'
    } catch (error) {
      deps.message.value = deps.describeError(error)
    }
  }

  return {
    history,
    historyLoading,
    historyError,
    loadHistory,
    applyHistoryRequest,
    loadHistoryDetail,
    removeHistory,
  }
}

function finiteNumber(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback
}
