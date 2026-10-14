<script setup lang="ts">
import PageIntro from '../components/PageIntro.vue'
import { computed, onMounted, reactive, ref } from 'vue'

import {
  createFactorTest,
  deleteFactorTestHistory,
  fetchFactorTestHistory,
  getFactorTest,
  getFactorTestHistory,
  getFactorTestResults,
} from '../api/factorTest'
import { fetchMetadata } from '../api/formulaScreen'
import { describeError } from '../api/client'
import { fetchActiveJobs } from '../api/jobs'
import { savePortfolioBacktestHandoff } from '../utils/portfolioBacktestHandoff'
import BacktestHistoryPanel from '../components/BacktestHistoryPanel.vue'
import DateRangeSelector from '../components/DateRangeSelector.vue'
import TermHint from '../components/TermHint.vue'
import type {
  BacktestHistorySummary,
  DateRangeMode,
  DateRangeSelection,
  FactorEvidence,
  FactorTestJobState,
  FactorTestPayload,
  FactorTestReport,
  FormulaScreenMetadata,
  ValueDefinition,
} from '../types'
import AppTopbar from '../components/AppTopbar.vue'
import { INTERACTIVE_JOB_TIMEOUT_MS, PollTimeoutError, pollUntil } from '../composables/usePolling'
import { formatNumber, formatPercent } from '../utils/format'

// 因子类指标（IC/IR/收益差）惯例 3 位小数；共享 formatNumber 的默认是 2 位。
function formatNumber3(value: unknown): string {
  return formatNumber(value, 3)
}


interface FactorFormState {
  universe: 'all' | 'sh' | 'sz'
  horizon: number
  valueIds: string[]
  includeCustom: boolean
  quantileCount: number
  minCrossSection: number
  permutations: number
  blockSize: number
  alpha: number
  limitStocks: number | null
  dateMode: DateRangeMode
  startDate: string
  endDate: string
}

const metadata = ref<FormulaScreenMetadata | null>(null)
const form = reactive<FactorFormState>({
  universe: 'all',
  horizon: 5,
  valueIds: [],
  includeCustom: true,
  quantileCount: 5,
  minCrossSection: 20,
  permutations: 200,
  blockSize: 20,
  alpha: 0.05,
  limitStocks: null,
  dateMode: 'recent5',
  startDate: '',
  endDate: '',
})
const errors = ref<Record<string, string>>({})
const message = ref('')
const loading = ref(false)
const job = ref<FactorTestJobState | null>(null)
const report = ref<FactorTestReport | null>(null)
const history = ref<BacktestHistorySummary[]>([])
const historyLoading = ref(false)
const selectedValueId = ref('')

const rankableValues = computed<ValueDefinition[]>(() => (
  metadata.value?.indicators
    .flatMap((indicator) => indicator.values ?? [])
    .filter((value) => value.rankable === true) ?? []
))
const technicalValues = computed<ValueDefinition[]>(() => (
  rankableValues.value.filter((value) => value.id.startsWith('technical.'))
))
const dateRange = computed<DateRangeSelection>({
  get: () => ({ mode: form.dateMode, startDate: form.startDate, endDate: form.endDate }),
  set: (value) => {
    form.dateMode = value.mode
    form.startDate = value.startDate
    form.endDate = value.endDate
  },
})
const canSubmit = computed(() => !loading.value && metadata.value !== null)
const selectedFactor = computed<FactorEvidence | null>(() => (
  report.value?.factors.find((item) => item.value_id === selectedValueId.value)
    ?? report.value?.factors[0]
    ?? null
))
const strongest = computed(() => report.value?.factors[0] ?? null)

function toggleValue(valueId: string): void {
  form.valueIds = form.valueIds.includes(valueId)
    ? form.valueIds.filter((item) => item !== valueId)
    : [...form.valueIds, valueId]
}

function selectAllTechnical(): void {
  form.valueIds = technicalValues.value.map((value) => value.id)
}

function clearValues(): void {
  form.valueIds = []
}

function validate(): Record<string, string> {
  const next: Record<string, string> = {}
  if (!form.valueIds.length) next.valueIds = '请至少选择一个指标输出。'
  if (!Number.isInteger(form.horizon) || form.horizon < 1 || form.horizon > 250) {
    next.horizon = '前瞻天数必须是 1 到 250 之间的整数。'
  }
  if (!Number.isInteger(form.quantileCount) || form.quantileCount < 2 || form.quantileCount > 10) {
    next.quantileCount = '分层数必须是 2 到 10 之间的整数。'
  }
  if (!Number.isInteger(form.minCrossSection) || form.minCrossSection < 2) {
    next.minCrossSection = '每日最小横截面至少为 2。'
  }
  if (!Number.isInteger(form.permutations) || form.permutations < 0 || form.permutations > 5000) {
    next.permutations = '噪声门槛检验次数必须是 0 到 5000 之间的整数（0 表示不检验）。'
  }
  if (form.limitStocks !== null && (!Number.isInteger(form.limitStocks) || form.limitStocks < 1)) {
    next.limitStocks = '股票数量上限必须是正整数或留空。'
  }
  if (form.dateMode === 'custom') {
    if (!form.startDate) next.startDate = '请选择开始日期。'
    if (!form.endDate) next.endDate = '请选择结束日期。'
    if (form.startDate && form.endDate && form.startDate > form.endDate) {
      next.endDate = '结束日期不能早于开始日期。'
    }
  }
  return next
}

function buildPayload(): FactorTestPayload {
  return {
    universe: form.universe,
    start_date: form.startDate || null,
    end_date: form.endDate || null,
    horizon: form.horizon,
    value_ids: [...form.valueIds],
    include_custom: form.includeCustom,
    quantile_count: form.quantileCount,
    min_cross_section: form.minCrossSection,
    permutations: form.permutations,
    block_size: form.blockSize,
    alpha: form.alpha,
    limit_stocks: form.limitStocks,
  }
}

function apiMessage(error: unknown): string {
  return describeError(error, { fallback: '因子检验失败，请检查本地 DuckDB 数据和配置后重试。' })
}

async function submit(): Promise<void> {
  errors.value = validate()
  message.value = ''
  if (Object.values(errors.value).some(Boolean)) return
  loading.value = true
  report.value = null
  try {
    const created = await createFactorTest(buildPayload())
    job.value = { job_id: created.job_id, status: 'queued', progress: 0, error: null, result: null }
    await poll(created.job_id)
  } catch (error) {
    message.value = apiMessage(error)
  } finally {
    loading.value = false
  }
}

async function poll(jobId: string): Promise<void> {
  let state: FactorTestJobState
  try {
    state = await pollUntil(
      async () => {
        const current = await getFactorTest(jobId)
        job.value = current
        return current
      },
      {
        intervalMs: 220,
        timeoutMs: INTERACTIVE_JOB_TIMEOUT_MS,
        until: (current) => {
          if (current.status === 'failed') {
            message.value = current.error ?? '因子检验任务失败，请检查配置和服务状态后重试。'
            return true
          }
          return current.status === 'completed'
        },
      },
    )
  } catch (error) {
    if (error instanceof PollTimeoutError) {
      message.value = '因子检验已等待超过 15 分钟，页面停止等待；任务仍在后台运行，稍后可刷新查看结果。'
      return
    }
    throw error
  }
  if (state.status === 'completed') {
    report.value = await getFactorTestResults(jobId)
    selectedValueId.value = report.value.factors[0]?.value_id ?? ''
    message.value = report.value.reason
      ?? `因子检验完成：${report.value.value_count} 个指标，其中 ${report.value.significant_count} 个通过多重检验校正。`
    await loadHistory()
  }
}

async function resumeActive(): Promise<void> {
  try {
    const active = (await fetchActiveJobs()).find((item) => item.description === '因子检验')
    if (!active) return
    job.value = active as unknown as FactorTestJobState
    loading.value = true
    await poll(active.job_id)
  } catch (error) {
    message.value = apiMessage(error)
  } finally {
    loading.value = false
  }
}

async function loadHistory(): Promise<void> {
  historyLoading.value = true
  try {
    history.value = await fetchFactorTestHistory()
  } catch (error) {
    message.value = apiMessage(error)
  } finally {
    historyLoading.value = false
  }
}

function finiteNumber(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback
}

/**
 * 把某次历史请求的输入还原回表单。
 *
 * 这不是"重跑"：历史记录存的是当时那份输入，加载后页面上的结果仍然是旧的那一版，
 * 必须重新运行才会更新 —— 提示语要照实说，不然用户会把加载当成又跑了一次。
 *
 * `date_mode` 不在 FactorTestRequest 里（后端只存绝对日期），所以有起止日期就切成
 * 自定义区间、没有就保持默认的「最近五年」；推断不出第三种可能，也别猜。
 */
function applyStoredRequest(raw: unknown): void {
  if (!raw || typeof raw !== 'object') return
  const request = raw as Record<string, unknown>
  if (request.universe === 'all' || request.universe === 'sh' || request.universe === 'sz') {
    form.universe = request.universe
  }
  form.horizon = Math.trunc(finiteNumber(request.horizon, form.horizon))
  form.valueIds = Array.isArray(request.value_ids)
    ? request.value_ids.filter((item): item is string => typeof item === 'string')
    : form.valueIds
  if (typeof request.include_custom === 'boolean') form.includeCustom = request.include_custom
  form.quantileCount = Math.trunc(finiteNumber(request.quantile_count, form.quantileCount))
  form.minCrossSection = Math.trunc(finiteNumber(request.min_cross_section, form.minCrossSection))
  form.permutations = Math.trunc(finiteNumber(request.permutations, form.permutations))
  form.blockSize = Math.trunc(finiteNumber(request.block_size, form.blockSize))
  form.alpha = finiteNumber(request.alpha, form.alpha)
  form.limitStocks = typeof request.limit_stocks === 'number' && Number.isFinite(request.limit_stocks)
    ? request.limit_stocks
    : null
  const start = typeof request.start_date === 'string' ? request.start_date : ''
  const end = typeof request.end_date === 'string' ? request.end_date : ''
  if (start && end) {
    form.dateMode = 'custom'
    form.startDate = start
    form.endDate = end
  }
}

async function loadHistoryDetail(historyId: string): Promise<void> {
  try {
    const loaded = await getFactorTestHistory(historyId)
    report.value = loaded.result
    selectedValueId.value = report.value.factors[0]?.value_id ?? ''
    applyStoredRequest(loaded.request)
    message.value = `已还原历史因子检验 ${historyId} 的输入配置，请确认后重新运行。`
  } catch (error) {
    message.value = apiMessage(error)
  }
}

async function removeHistory(historyId: string): Promise<void> {
  try {
    await deleteFactorTestHistory(historyId)
    await loadHistory()
    message.value = '历史因子检验已删除。'
  } catch (error) {
    message.value = apiMessage(error)
  }
}

function orderText(order: FactorEvidence['order']): string {
  return order === 'high' ? '买最高一组' : '买最低一组'
}

function handoffSelectedFactor(): void {
  const factor = selectedFactor.value
  if (!factor) return
  const order = factor.order === 'high' ? 'desc' : 'asc'
  const evidence = `因子检验建议${orderText(factor.order)}；IC ${formatNumber3(factor.ic_mean)}，校正后 q 值 ${formatNumber3(factor.qvalue)}`
  if (!savePortfolioBacktestHandoff({
    kind: 'ranking',
    source: 'factor-test',
    source_label: '因子检验结论',
    value_id: factor.value_id,
    rank_order: order,
    evidence,
  })) {
    message.value = '浏览器暂时无法保存跨页配置，请检查隐私模式或存储权限。'
    return
  }
  if (import.meta.env.MODE !== 'test') window.location.assign('/portfolio-backtest')
}

function monotonicText(factor: FactorEvidence): string {
  if (!factor.layers.length) return '—'
  if (factor.layer_inversions === 0) return '单调'
  return `${factor.layer_inversions} 处倒挂`
}

/** 折线路径：把序列按自身范围压进画布，缺失点断开而不是连成假线。 */
function linePath(values: (number | null)[], width: number, height: number): string {
  const finite = values
    .map((value, index) => ({ index, value }))
    .filter((item): item is { index: number; value: number } => (
      typeof item.value === 'number' && Number.isFinite(item.value)
    ))
  if (finite.length < 2) return ''
  const numbers = finite.map((item) => item.value)
  const low = Math.min(...numbers, 0)
  const high = Math.max(...numbers, 0)
  const span = high - low || 1
  const step = values.length > 1 ? width / (values.length - 1) : width
  return finite
    .map((item, position) => {
      const x = item.index * step
      const y = height - ((item.value - low) / span) * height
      return `${position === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`
    })
    .join(' ')
}

const CHART_WIDTH = 640
const CHART_HEIGHT = 170

const icPath = computed(() => (
  selectedFactor.value ? linePath(selectedFactor.value.ic_series, CHART_WIDTH, CHART_HEIGHT) : ''
))
const icCumulativePath = computed(() => (
  selectedFactor.value
    ? linePath(selectedFactor.value.ic_cumulative, CHART_WIDTH, CHART_HEIGHT)
    : ''
))
const spreadPath = computed(() => (
  selectedFactor.value
    ? linePath(selectedFactor.value.long_short.cumulative, CHART_WIDTH, CHART_HEIGHT)
    : ''
))
const layerScale = computed(() => {
  const factor = selectedFactor.value
  if (!factor) return 1
  const values = factor.layers
    .map((layer) => Math.abs(layer.mean_return ?? 0))
    .filter((value) => Number.isFinite(value))
  return Math.max(...values, 1e-9)
})

function downloadResult(): void {
  if (!report.value) return
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(report.value, null, 2)], { type: 'application/json' }),
  )
  const link = document.createElement('a')
  link.href = url
  link.download = 'factor-test-report.json'
  link.click()
  window.setTimeout(() => URL.revokeObjectURL(url), 0)
}

onMounted(async () => {
  try {
    metadata.value = await fetchMetadata()
    selectAllTechnical()
    await loadHistory()
    await resumeActive()
  } catch (error) {
    message.value = apiMessage(error)
  }
})
</script>

<template>
  <div class="screen-page factor-page" data-testid="factor-test-page">
    <AppTopbar />

    <main class="content-shell">
      <PageIntro
        eyebrow="FACTOR TEST / FORWARD RETURN"
        title="因子检验"
        copy="每一天、每一只股票都在样本里：看指标能不能把股票按「未来 N 天的收益」排个先后。"
        note-label="它能回答什么"
        guide-key="factor-test"
      >
        <strong>这个指标有没有预测力</strong>
        <small>无条件全样本，和条件研究（只看信号日）、波段研究（只看事件）不是一回事</small>
      </PageIntro>

      <div v-if="message" class="notice" role="alert" data-testid="factor-message">{{ message }}</div>

      <div class="workspace-grid factor-grid">
        <form class="config-panel panel factor-config" data-testid="factor-config" @submit.prevent="submit">
          <div class="panel-heading">
            <div><span class="section-kicker">01 / CONFIGURE</span><h2>检验配置</h2></div>
          </div>

          <fieldset class="form-section">
            <legend>数据范围</legend>
            <div class="compact-fields">
              <div>
                <label for="factor-universe">品种范围</label>
                <select id="factor-universe" v-model="form.universe" data-testid="factor-universe">
                  <option value="all">沪深全部品种</option>
                  <option value="sh">仅上海</option>
                  <option value="sz">仅深圳</option>
                </select>
              </div>
              <div>
                <label for="factor-limit-stocks">股票数上限</label>
                <input id="factor-limit-stocks" v-model.number="form.limitStocks" data-testid="factor-limit-stocks" type="number" min="1" step="50" placeholder="留空=全部">
              </div>
            </div>
            <DateRangeSelector v-model="dateRange" prefix="factor" testid="factor-date-range" helper="读取本地 DuckDB 已导入的日线。区间越长、股票越多、指标越多，面板越大——跑不动时先减指标数。" />
            <p v-if="errors.startDate" class="field-error">{{ errors.startDate }}</p>
            <p v-if="errors.endDate" class="field-error">{{ errors.endDate }}</p>
            <p v-if="errors.limitStocks" class="field-error">{{ errors.limitStocks }}</p>
          </fieldset>

          <fieldset class="form-section">
            <legend>标签</legend>
            <div class="compact-fields">
              <div>
                <label for="factor-horizon">前瞻天数<TermHint term="forwardHorizon" />（交易日）</label>
                <input id="factor-horizon" v-model.number="form.horizon" data-testid="factor-horizon" type="number" min="1" max="250" step="1">
              </div>
              <div>
                <label for="factor-quantile">分层数</label>
                <input id="factor-quantile" v-model.number="form.quantileCount" data-testid="factor-quantile" type="number" min="2" max="10" step="1">
              </div>
            </div>
            <p class="helper">标签 = 从当日收盘到 N 个交易日后的收盘的收益。最后 N 天没有未来，自动退出样本。</p>
            <p v-if="errors.horizon" class="field-error">{{ errors.horizon }}</p>
            <p v-if="errors.quantileCount" class="field-error">{{ errors.quantileCount }}</p>
          </fieldset>

          <fieldset class="form-section">
            <legend>待检验指标</legend>
            <div class="factor-value-actions">
              <button type="button" class="table-link" data-testid="factor-select-technical" @click="selectAllTechnical">全选技术指标</button>
              <button type="button" class="table-link" data-testid="factor-clear-values" @click="clearValues">清空</button>
              <label class="factor-inline-check">
                <input v-model="form.includeCustom" data-testid="factor-include-custom" type="checkbox">
                <span>纳入自定义公式的连续输出</span>
              </label>
            </div>
            <div class="factor-value-list" data-testid="factor-picker">
              <label v-for="value in rankableValues" :key="value.id" class="factor-value-item">
                <input type="checkbox" :data-testid="`factor-value-${value.id}`" :checked="form.valueIds.includes(value.id)" @change="toggleValue(value.id)">
                <span>{{ value.display_name }}</span>
              </label>
            </div>
            <p class="helper">只列可跨股票比较的输出。已选 {{ form.valueIds.length }} 个。</p>
            <p v-if="errors.valueIds" class="field-error">{{ errors.valueIds }}</p>
          </fieldset>

          <fieldset class="form-section">
            <legend>统计口径</legend>
            <div class="compact-fields">
              <div>
                <label for="factor-min-cross-section">每日最小横截面</label>
                <input id="factor-min-cross-section" v-model.number="form.minCrossSection" data-testid="factor-min-cross-section" type="number" min="2" step="5">
              </div>
              <div>
                <label for="factor-permutations">噪声门槛检验次数</label>
                <input id="factor-permutations" v-model.number="form.permutations" data-testid="factor-permutations" type="number" min="0" max="5000" step="50">
              </div>
            </div>
            <p class="helper">0 表示不做检验，此时没有 p 值与多重检验校正。整块打乱日期，不是打散单元格。</p>
            <p v-if="errors.minCrossSection" class="field-error">{{ errors.minCrossSection }}</p>
            <p v-if="errors.permutations" class="field-error">{{ errors.permutations }}</p>
          </fieldset>

          <div class="action-stack">
            <button class="primary-button" data-testid="start-factor-test" type="button" :disabled="!canSubmit" @click="submit">
              <span v-if="loading" class="spinner" aria-hidden="true"></span>{{ loading ? '检验中…' : '开始因子检验' }}
            </button>
          </div>
          <p class="privacy-note">只使用每个时间点之前的数据；结果用于研究，不是投资建议。</p>
        </form>

        <section class="results-panel panel" data-testid="factor-results" aria-live="polite">
          <div class="panel-heading results-heading">
            <div><span class="section-kicker">02 / RESULTS</span><h2>检验结果</h2></div>
            <button v-if="report" class="secondary-button export-backtest" type="button" data-testid="export-factor-test" @click="downloadResult">导出 JSON</button>
          </div>

          <div v-if="!report" class="empty-state" data-testid="factor-empty">
            <div class="empty-icon" aria-hidden="true">/</div>
            <h3>{{ loading ? '正在构建面板并逐日计算信息系数…' : '还没有检验结果' }}</h3>
            <p>{{ loading ? '两遍读取行情：第一遍建日期轴，第二遍算指标与前瞻收益。' : '选择指标和前瞻天数后开始。' }}</p>
          </div>

          <template v-else>
            <div class="metrics-strip factor-metrics">
              <div><span>股票 / 扫描</span><strong>{{ report.stock_count }} / {{ report.scanned_stocks }}</strong></div>
              <div><span>交易日</span><strong>{{ report.date_range.days }}</strong></div>
              <div><span>前瞻天数</span><strong>{{ report.horizon }}</strong></div>
              <div><span>通过校正<TermHint term="factorSignificant" /></span><strong data-testid="factor-significant-count">{{ report.significant_count }} / {{ report.value_count }}</strong></div>
            </div>

            <ul v-if="report.boundaries.length" class="factor-boundaries" data-testid="factor-boundaries">
              <li v-for="line in report.boundaries" :key="line">{{ line }}</li>
            </ul>

            <div v-if="!report.factors.length" class="inline-empty">没有指标形成有效的横截面样本。</div>
            <div v-else class="table-wrap">
              <table>
                <caption class="sr-only">因子检验结果</caption>
                <thead>
                  <tr>
                    <th>指标</th>
                    <th>方向</th>
                    <th>信息系数<TermHint term="ic" /></th>
                    <th>信息比率<TermHint term="icir" /></th>
                    <th>同向占比</th>
                    <th>分层<TermHint term="quantile" /></th>
                    <th>多空日均<TermHint term="longShort" /></th>
                    <th>多空胜率</th>
                    <th>p 值</th>
                    <th>q 值</th>
                    <th>结论</th>
                  </tr>
                </thead>
                <tbody>
                  <tr
                    v-for="factor in report.factors"
                    :key="factor.value_id"
                    :class="{ 'factor-row-active': selectedFactor?.value_id === factor.value_id }"
                  >
                    <td>
                      <button class="table-link" type="button" :data-testid="`factor-pick-${factor.value_id}`" @click="selectedValueId = factor.value_id">
                        {{ factor.display_name }}
                      </button>
                      <span v-if="factor.significant" class="factor-badge" data-testid="factor-significant-badge">通过</span>
                    </td>
                    <td class="factor-nowrap">{{ orderText(factor.order) }}</td>
                    <td>{{ formatNumber3(factor.ic_mean) }}</td>
                    <td>{{ formatNumber(factor.icir, 2) }}</td>
                    <td>{{ formatPercent(factor.ic_positive_rate, 0) }}</td>
                    <td class="factor-nowrap">{{ monotonicText(factor) }}</td>
                    <td>{{ formatPercent(factor.long_short.mean_spread, 3) }}</td>
                    <td>{{ formatPercent(factor.long_short.win_rate, 0) }}</td>
                    <td>{{ formatNumber3(factor.pvalue) }}</td>
                    <td>{{ formatNumber3(factor.qvalue) }}</td>
                    <td class="factor-note">{{ factor.note }}</td>
                  </tr>
                </tbody>
              </table>
            </div>

            <div v-if="selectedFactor" class="factor-detail" data-testid="factor-detail">
              <div class="factor-detail-heading">
                <h3>{{ selectedFactor.display_name }} · {{ orderText(selectedFactor.order) }}</h3>
                <p>{{ selectedFactor.note }}</p>
              </div>

              <div class="factor-charts">
                <figure class="factor-chart">
                  <figcaption>每日信息系数（横轴 {{ report.date_range.start }} → {{ report.date_range.end }}）</figcaption>
                  <svg :viewBox="`0 0 ${CHART_WIDTH} ${CHART_HEIGHT}`" preserveAspectRatio="none" role="img" aria-label="每日信息系数曲线">
                    <line :x1="0" :x2="CHART_WIDTH" :y1="CHART_HEIGHT / 2" :y2="CHART_HEIGHT / 2" class="factor-axis" />
                    <path v-if="icPath" :d="icPath" class="factor-line" />
                  </svg>
                </figure>
                <figure class="factor-chart">
                  <figcaption>信息系数累计（持续为正＝方向稳定）</figcaption>
                  <svg :viewBox="`0 0 ${CHART_WIDTH} ${CHART_HEIGHT}`" preserveAspectRatio="none" role="img" aria-label="信息系数累计曲线">
                    <line :x1="0" :x2="CHART_WIDTH" :y1="CHART_HEIGHT / 2" :y2="CHART_HEIGHT / 2" class="factor-axis" />
                    <path v-if="icCumulativePath" :d="icCumulativePath" class="factor-line" />
                  </svg>
                </figure>
                <figure class="factor-chart">
                  <figcaption>多空累计价差（首档减末档，累加非复利）</figcaption>
                  <svg :viewBox="`0 0 ${CHART_WIDTH} ${CHART_HEIGHT}`" preserveAspectRatio="none" role="img" aria-label="多空累计价差曲线">
                    <line :x1="0" :x2="CHART_WIDTH" :y1="CHART_HEIGHT / 2" :y2="CHART_HEIGHT / 2" class="factor-axis" />
                    <path v-if="spreadPath" :d="spreadPath" class="factor-line-spread" />
                  </svg>
                </figure>
              </div>

              <div class="factor-layers" data-testid="factor-layers">
                <div class="factor-layers-heading">
                  <span>分层收益（第 1 档 = 该买的一组）</span>
                  <span>前后半段信息系数：{{ formatNumber3(selectedFactor.ic_first_half) }} / {{ formatNumber3(selectedFactor.ic_second_half) }}</span>
                </div>
                <div v-for="layer in selectedFactor.layers" :key="layer.index" class="factor-layer-row">
                  <span class="factor-layer-index">第 {{ layer.index }} 档</span>
                  <span class="factor-layer-bar" :style="{ width: `${Math.min(100, (Math.abs(layer.mean_return ?? 0) / layerScale) * 100)}%` }" :class="{ negative: (layer.mean_return ?? 0) < 0 }"></span>
                  <span class="factor-layer-value">{{ formatPercent(layer.mean_return, 3) }}</span>
                  <span class="factor-layer-size">日均 {{ layer.size === null ? '—' : Math.round(layer.size) }} 只</span>
                </div>
              </div>

              <dl class="factor-facts">
                <div><dt>多空日均价差</dt><dd>{{ formatPercent(selectedFactor.long_short.mean_spread, 3) }}</dd></div>
                <div><dt>价差 t 值</dt><dd>{{ formatNumber(selectedFactor.long_short.t_stat, 2) }}</dd></div>
                <div><dt>累计价差</dt><dd>{{ formatPercent(selectedFactor.long_short.final, 2) }}</dd></div>
                <div><dt>最大回撤</dt><dd>{{ formatPercent(selectedFactor.long_short.max_drawdown, 2) }}</dd></div>
                <div><dt>噪声门槛<TermHint term="noiseFloor" /></dt><dd>{{ formatNumber3(selectedFactor.ic_threshold) }}</dd></div>
                <div><dt>有效交易日</dt><dd>{{ selectedFactor.ic_days }}</dd></div>
              </dl>
              <div class="factor-handoff-actions">
                <button class="secondary-button" type="button" data-testid="factor-to-portfolio" @click="handoffSelectedFactor">
                  把该指标与方向带入组合回测排序
                </button>
                <p class="helper">只带入排序指标和升降方向；回测标的、日期、买卖规则与风控保持原设置。因子检验结果是候选依据，应用后请自行复核回测表现。</p>
              </div>
            </div>
          </template>
        </section>
      </div>

      <div v-if="loading && job" class="task-progress task-progress-panel" data-testid="factor-progress">
        <div class="task-progress-heading"><span>因子检验进度</span><strong>{{ Math.round(job.progress * 100) }}%</strong></div>
        <div class="progress-track"><span :style="{ width: `${Math.round(job.progress * 100)}%` }"></span></div>
      </div>

      <p v-if="strongest" class="scope-helper">最强的一条是 {{ strongest.display_name }}：{{ strongest.note }}</p>

      <BacktestHistoryPanel
        :items="history"
        :loading="historyLoading"
        testid="factor-history"
        title="历史因子检验"
        empty-text="暂无历史因子检验。"
        @refresh="loadHistory"
        @load="loadHistoryDetail"
        @remove="removeHistory"
      />
    </main>
  </div>
</template>

<style scoped>
.factor-value-actions {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  flex-wrap: wrap;
  margin-bottom: 0.5rem;
}

.factor-handoff-actions { margin-top: 12px; }
.factor-handoff-actions .helper { margin: 6px 0 0; }

.factor-inline-check {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  font-size: 0.85rem;
}

.factor-value-list {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
  gap: 0.25rem 0.75rem;
  max-height: 220px;
  overflow-y: auto;
  border: 1px solid var(--border-subtle);
  border-radius: 8px;
  padding: 0.5rem 0.75rem;
}

.factor-value-item {
  display: flex;
  align-items: center;
  gap: 0.35rem;
  font-size: 0.85rem;
}

.factor-boundaries {
  margin: 0 0 0.75rem;
  padding-left: 1.1rem;
  font-size: 0.8rem;
  color: var(--subtle);
}

.factor-row-active {
  background: var(--accent-soft);
}

.factor-nowrap {
  white-space: nowrap;
}

/* 描边而非绿底：这是「通过校正」的标记，不是涨跌，染成红/绿会被读成收益。 */
.factor-badge {
  margin-left: 0.35rem;
  padding: 0.05rem 0.35rem;
  border: 1px solid var(--accent);
  border-radius: 999px;
  font-size: 0.68rem;
  color: var(--accent);
  white-space: nowrap;
}

.factor-note {
  min-width: 260px;
  font-size: 0.78rem;
  color: var(--subtle);
}

.factor-detail {
  margin-top: 1rem;
  border-top: 1px solid var(--border-subtle);
  padding-top: 0.75rem;
}

.factor-detail-heading h3 {
  margin: 0 0 0.25rem;
  font-size: 1rem;
}

.factor-detail-heading p {
  margin: 0 0 0.75rem;
  font-size: 0.8rem;
  color: var(--subtle);
}

.factor-charts {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 0.75rem;
}

.factor-chart {
  margin: 0;
}

.factor-chart figcaption {
  font-size: 0.78rem;
  color: var(--subtle);
  margin-bottom: 0.25rem;
}

.factor-chart svg {
  width: 100%;
  height: 120px;
  border: 1px solid var(--border-subtle);
  border-radius: 8px;
  background: rgba(148, 163, 184, 0.06);
}

.factor-axis {
  stroke: rgba(148, 163, 184, 0.5);
  stroke-width: 1;
  stroke-dasharray: 4 4;
}

.factor-line,
.factor-line-spread {
  fill: none;
  stroke-width: 1.5;
}

.factor-line {
  stroke: #2563eb;
}

.factor-line-spread {
  stroke: #dc2626;
}

.factor-layers {
  margin-top: 0.85rem;
}

.factor-layers-heading {
  display: flex;
  justify-content: space-between;
  gap: 1rem;
  flex-wrap: wrap;
  font-size: 0.8rem;
  color: var(--subtle);
  margin-bottom: 0.35rem;
}

.factor-layer-row {
  display: grid;
  grid-template-columns: 70px 1fr 90px 110px;
  align-items: center;
  gap: 0.5rem;
  margin-bottom: 0.25rem;
  font-size: 0.82rem;
}

.factor-layer-bar {
  height: 10px;
  border-radius: 5px;
  background: #dc2626;
}

.factor-layer-bar.negative {
  background: #16a34a;
}

.factor-layer-value,
.factor-layer-size {
  font-variant-numeric: tabular-nums;
}

.factor-layer-size {
  color: var(--subtle);
}

.factor-facts {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 0.5rem;
  margin: 0.85rem 0 0;
}

.factor-facts div {
  border: 1px solid var(--border-subtle);
  border-radius: 8px;
  padding: 0.45rem 0.6rem;
}

.factor-facts dt {
  font-size: 0.75rem;
  color: var(--subtle);
}

.factor-facts dd {
  margin: 0.15rem 0 0;
  font-size: 0.95rem;
  font-variant-numeric: tabular-nums;
}
</style>
