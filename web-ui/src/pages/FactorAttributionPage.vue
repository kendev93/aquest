<script setup lang="ts">
import PageGuide from '../components/PageGuide.vue'
import { computed, onMounted, reactive, ref } from 'vue'

import {
  createFactorAttribution,
  deleteFactorAttributionHistory,
  fetchAttributionSources,
  fetchFactorAttributionHistory,
  getFactorAttribution,
  getFactorAttributionHistory,
  getFactorAttributionResults,
} from '../api/factorAttribution'
import { fetchMetadata } from '../api/formulaScreen'
import { ApiError } from '../api/client'
import { fetchActiveJobs } from '../api/jobs'
import { savePortfolioBacktestHandoff } from '../utils/portfolioBacktestHandoff'
import BacktestHistoryPanel from '../components/BacktestHistoryPanel.vue'
import TermHint from '../components/TermHint.vue'
import type {
  AttributionFactor,
  BacktestHistorySummary,
  FactorAttributionJobState,
  FactorAttributionPayload,
  FactorAttributionReport,
  FormulaScreenMetadata,
  ValueDefinition,
} from '../types'
import { formatChinaTimestamp } from '../utils/time'
import AppTopbar from '../components/AppTopbar.vue'

const JOB_DESCRIPTION = '因子归因'

interface AttributionFormState {
  sourceHistoryId: string
  valueIds: string[]
  includeCustom: boolean
  minGroupTrades: number
  permutations: number
  blockSize: number
  alpha: number
}

const metadata = ref<FormulaScreenMetadata | null>(null)
const sources = ref<BacktestHistorySummary[]>([])
const form = reactive<AttributionFormState>({
  sourceHistoryId: '',
  valueIds: [],
  includeCustom: true,
  minGroupTrades: 20,
  permutations: 200,
  blockSize: 20,
  alpha: 0.05,
})
const errors = ref<Record<string, string>>({})
const message = ref('')
const loading = ref(false)
const job = ref<FactorAttributionJobState | null>(null)
const report = ref<FactorAttributionReport | null>(null)
const history = ref<BacktestHistorySummary[]>([])
const historyLoading = ref(false)

const rankableValues = computed<ValueDefinition[]>(() => (
  metadata.value?.indicators
    .flatMap((indicator) => indicator.values ?? [])
    .filter((value) => value.rankable === true) ?? []
))
const technicalValues = computed<ValueDefinition[]>(() => (
  rankableValues.value.filter((value) => value.id.startsWith('technical.'))
))
const canSubmit = computed(() => !loading.value && metadata.value !== null)
const strongest = computed<AttributionFactor | null>(() => report.value?.factors[0] ?? null)

function sourceLabel(item: BacktestHistorySummary): string {
  const when = typeof item.finished_at === 'string' ? formatChinaTimestamp(item.finished_at) : '—'
  return `${item.backtest_type} · ${when} · ${item.history_id.slice(0, 8)}`
}

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
  if (!form.sourceHistoryId) next.sourceHistoryId = '请先选择一条回测记录。'
  if (!Number.isInteger(form.minGroupTrades) || form.minGroupTrades < 2) {
    next.minGroupTrades = '每组最少成交笔数至少为 2。'
  }
  if (!Number.isInteger(form.permutations) || form.permutations < 0 || form.permutations > 5000) {
    next.permutations = '噪声门槛检验次数必须是 0 到 5000 之间的整数（0 表示不检验）。'
  }
  if (!(form.alpha > 0 && form.alpha < 1)) next.alpha = '显著性水平必须介于 0 与 1 之间。'
  if (!Number.isInteger(form.blockSize) || form.blockSize < 1) {
    next.blockSize = '置换块长度至少为 1 个交易日。'
  }
  return next
}

function buildPayload(): FactorAttributionPayload {
  return {
    source_history_id: form.sourceHistoryId,
    value_ids: [...form.valueIds],
    include_custom: form.includeCustom,
    min_group_trades: form.minGroupTrades,
    permutations: form.permutations,
    block_size: form.blockSize,
    alpha: form.alpha,
  }
}

function apiMessage(error: unknown): string {
  return error instanceof ApiError
    ? error.message
    : '因子归因失败，请检查本地 DuckDB 数据和配置后重试。'
}

async function submit(): Promise<void> {
  errors.value = validate()
  message.value = ''
  if (Object.values(errors.value).some(Boolean)) return
  loading.value = true
  report.value = null
  try {
    const created = await createFactorAttribution(buildPayload())
    job.value = { job_id: created.job_id, status: 'queued', progress: 0, error: null, result: null }
    await poll(created.job_id)
  } catch (error) {
    message.value = apiMessage(error)
  } finally {
    loading.value = false
  }
}

async function poll(jobId: string): Promise<void> {
  for (;;) {
    const state = await getFactorAttribution(jobId)
    job.value = state
    if (state.status === 'completed') {
      report.value = await getFactorAttributionResults(jobId)
      message.value = report.value.reason
        ?? `因子归因完成：${report.value.value_count} 个指标，其中 ${report.value.significant_count} 个通过多重检验校正。`
      await loadHistory()
      return
    }
    if (state.status === 'failed') {
      message.value = state.error ?? '因子归因任务失败，请检查配置和服务状态后重试。'
      return
    }
    await new Promise((resolve) => window.setTimeout(resolve, 220))
  }
}

async function resumeActive(): Promise<void> {
  try {
    const active = (await fetchActiveJobs()).find((item) => item.description === JOB_DESCRIPTION)
    if (!active) return
    job.value = active as unknown as FactorAttributionJobState
    loading.value = true
    await poll(active.job_id)
  } catch (error) {
    message.value = apiMessage(error)
  } finally {
    loading.value = false
  }
}

async function loadSources(): Promise<void> {
  try {
    sources.value = await fetchAttributionSources()
  } catch (error) {
    message.value = apiMessage(error)
  }
}

async function loadHistory(): Promise<void> {
  historyLoading.value = true
  try {
    history.value = await fetchFactorAttributionHistory()
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
 * 还原某次归因的输入。
 *
 * 加载的是"当时那份输入"，不是新跑一版结果：页面上的报告仍然是旧的，必须重新运行
 * 才会更新，提示语照实写。
 *
 * 来源回测记录（`source_history_id`）不在受影响范围内的假设之上——历史记录会被淘汰，
 * 还原后提交时后端会重新校验它还在不在，这里只负责把选择搬回表单。
 */
function applyStoredRequest(raw: unknown): void {
  if (!raw || typeof raw !== 'object') return
  const request = raw as Record<string, unknown>
  if (typeof request.source_history_id === 'string' && request.source_history_id) {
    form.sourceHistoryId = request.source_history_id
  }
  form.valueIds = Array.isArray(request.value_ids)
    ? request.value_ids.filter((item): item is string => typeof item === 'string')
    : form.valueIds
  if (typeof request.include_custom === 'boolean') form.includeCustom = request.include_custom
  form.minGroupTrades = Math.trunc(finiteNumber(request.min_group_trades, form.minGroupTrades))
  form.permutations = Math.trunc(finiteNumber(request.permutations, form.permutations))
  form.blockSize = Math.trunc(finiteNumber(request.block_size, form.blockSize))
  form.alpha = finiteNumber(request.alpha, form.alpha)
}

async function loadHistoryDetail(historyId: string): Promise<void> {
  try {
    const loaded = await getFactorAttributionHistory(historyId)
    report.value = loaded.result
    applyStoredRequest(loaded.request)
    message.value = `已还原历史因子归因 ${historyId} 的输入配置，请确认后重新运行。`
  } catch (error) {
    message.value = apiMessage(error)
  }
}

async function removeHistory(historyId: string): Promise<void> {
  try {
    await deleteFactorAttributionHistory(historyId)
    await loadHistory()
    message.value = '历史因子归因已删除。'
  } catch (error) {
    message.value = apiMessage(error)
  }
}

function formatNumber(value: number | null | undefined, digits = 3): string {
  return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(digits) : '—'
}

function formatCount(value: number | null | undefined): string {
  return typeof value === 'number' && Number.isFinite(value) ? String(Math.round(value)) : '—'
}

function verdictText(factor: AttributionFactor): string {
  if (factor.significant === true) return '通过校正'
  if (factor.significant === false) return '不显著'
  return factor.note ?? '未判断'
}

function canApplyAttributionFactor(factor: AttributionFactor): boolean {
  return typeof factor.difference === 'number'
    && Number.isFinite(factor.difference)
    && factor.difference !== 0
    && rankableValues.value.some((value) => value.id === factor.value_id)
}

function handoffAttributionFactor(factor: AttributionFactor): void {
  const difference = factor.difference
  if (!canApplyAttributionFactor(factor) || typeof difference !== 'number') return
  const order = difference > 0 ? 'desc' : 'asc'
  const evidence = `胜组均值减败组均值为 ${formatNumber(difference)}，${verdictText(factor)}；方向仅表示本次样本中的关联`
  if (!savePortfolioBacktestHandoff({
    kind: 'ranking',
    source: 'factor-attribution',
    source_label: '因子归因结论',
    value_id: factor.value_id,
    rank_order: order,
    evidence,
  })) {
    message.value = '浏览器暂时无法保存跨页配置，请检查隐私模式或存储权限。'
    return
  }
  if (import.meta.env.MODE !== 'test') window.location.assign('/portfolio-backtest')
}

function downloadResult(): void {
  if (!report.value) return
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(report.value, null, 2)], { type: 'application/json' }),
  )
  const link = document.createElement('a')
  link.href = url
  link.download = 'factor-attribution-report.json'
  link.click()
  window.setTimeout(() => URL.revokeObjectURL(url), 0)
}

onMounted(async () => {
  try {
    metadata.value = await fetchMetadata()
    selectAllTechnical()
    await loadSources()
    await loadHistory()
    await resumeActive()
  } catch (error) {
    message.value = apiMessage(error)
  }
})
</script>

<template>
  <div class="screen-page attribution-page" data-testid="factor-attribution-page">
    <AppTopbar />

    <main class="content-shell">
      <section class="page-intro">
        <div>
          <p class="eyebrow">FACTOR ATTRIBUTION / WHY THESE WON</p>
          <h1>因子归因</h1>
          <p class="intro-copy">拿一次已经跑完的回测，把赢的单和输的单分开，看它们在买入当天的指标长什么样。</p>
        </div>
        <div class="intro-note">
          <span class="note-label">它能回答什么</span>
          <strong>这批单为什么赢</strong>
          <small>事后解释，不是预测；和因子检验（指标能不能预测未来收益）方向相反</small>
        </div>
      </section>

      <PageGuide page-key="factor-attribution" />

      <div v-if="message" class="notice" role="alert" data-testid="attribution-message">{{ message }}</div>

      <div class="workspace-grid attribution-grid">
        <form class="config-panel panel attribution-config" data-testid="attribution-config" @submit.prevent="submit">
          <div class="panel-heading">
            <div><span class="section-kicker">01 / CONFIGURE</span><h2>归因配置</h2></div>
          </div>

          <fieldset class="form-section">
            <legend>来源回测</legend>
            <div>
              <label for="attribution-source">要解释哪一次回测</label>
              <select id="attribution-source" v-model="form.sourceHistoryId" data-testid="attribution-source">
                <option value="">请选择…</option>
                <option v-for="item in sources" :key="item.history_id" :value="item.history_id">{{ sourceLabel(item) }}</option>
              </select>
            </div>
            <p class="helper">只列结果里带成交明细的记录（组合回测、条件研究）。每种类型只保留最近 20 条，太久以前的会自动淘汰。<strong>不重跑回测</strong>，直接读当时存下来的成交。</p>
            <p v-if="!sources.length" class="helper">还没有可归因的回测记录：先去组合回测或条件研究跑一次。</p>
            <p v-if="errors.sourceHistoryId" class="field-error">{{ errors.sourceHistoryId }}</p>
          </fieldset>

          <fieldset class="form-section">
            <legend>参与比较的指标</legend>
            <div class="attribution-value-actions">
              <button type="button" class="table-link" data-testid="attribution-select-technical" @click="selectAllTechnical">全选技术指标</button>
              <button type="button" class="table-link" data-testid="attribution-clear-values" @click="clearValues">清空</button>
              <label class="attribution-inline-check">
                <input v-model="form.includeCustom" data-testid="attribution-include-custom" type="checkbox">
                <span>纳入自定义公式的连续输出</span>
              </label>
            </div>
            <div class="attribution-value-list" data-testid="attribution-picker">
              <label v-for="value in rankableValues" :key="value.id" class="attribution-value-item">
                <input type="checkbox" :data-testid="`attribution-value-${value.id}`" :checked="form.valueIds.includes(value.id)" @change="toggleValue(value.id)">
                <span>{{ value.display_name }}</span>
              </label>
            </div>
            <p class="helper">留空＝全部技术指标 + 自定义连续输出。已选 {{ form.valueIds.length }} 个。</p>
          </fieldset>

          <fieldset class="form-section">
            <legend>统计口径</legend>
            <div class="compact-fields">
              <div>
                <label for="attribution-min-group">每组最少成交笔数</label>
                <input id="attribution-min-group" v-model.number="form.minGroupTrades" data-testid="attribution-min-group" type="number" min="2" step="5">
              </div>
              <div>
                <label for="attribution-permutations">噪声门槛检验次数<TermHint term="blockPermutation" /></label>
                <input id="attribution-permutations" v-model.number="form.permutations" data-testid="attribution-permutations" type="number" min="0" max="5000" step="50">
              </div>
              <div>
                <label for="attribution-block">置换块长度（交易日）</label>
                <input id="attribution-block" v-model.number="form.blockSize" data-testid="attribution-block" type="number" min="1" max="500" step="5">
              </div>
              <div>
                <label for="attribution-alpha">显著性水平</label>
                <input id="attribution-alpha" v-model.number="form.alpha" data-testid="attribution-alpha" type="number" min="0.001" max="0.5" step="0.01">
              </div>
            </div>
            <p class="helper">胜/败按净收益<TermHint term="netReturn" />分；比较的是入场信号日<TermHint term="signalDay" />的取值。组太小只描述、不判显著。</p>
            <p v-if="errors.minGroupTrades" class="field-error">{{ errors.minGroupTrades }}</p>
            <p v-if="errors.permutations" class="field-error">{{ errors.permutations }}</p>
            <p v-if="errors.blockSize" class="field-error">{{ errors.blockSize }}</p>
            <p v-if="errors.alpha" class="field-error">{{ errors.alpha }}</p>
          </fieldset>

          <div class="action-stack">
            <button class="primary-button" data-testid="start-attribution" type="button" :disabled="!canSubmit" @click="submit">
              <span v-if="loading" class="spinner" aria-hidden="true"></span>{{ loading ? '归因中…' : '开始因子归因' }}
            </button>
          </div>
          <p class="privacy-note">只用已经发生的成交做解释，结果用于研究，不是投资建议。</p>
        </form>

        <section class="results-panel panel" data-testid="attribution-results" aria-live="polite">
          <div class="panel-heading results-heading">
            <div><span class="section-kicker">02 / RESULTS</span><h2>归因结果</h2></div>
            <button v-if="report" class="secondary-button export-backtest" type="button" data-testid="export-attribution" @click="downloadResult">导出 JSON</button>
          </div>

          <div v-if="!report" class="empty-state" data-testid="attribution-empty">
            <div class="empty-icon" aria-hidden="true">/</div>
            <h3>{{ loading ? '正在读取成交并回看入场日的指标…' : '还没有归因结果' }}</h3>
            <p>{{ loading ? '先配对成交，再按标的批量读行情、整段算指标。' : '选择一条回测记录后开始。' }}</p>
          </div>

          <template v-else>
            <div class="metrics-strip attribution-metrics">
              <div><span>平仓交易</span><strong data-testid="attribution-trades">{{ formatCount(report.samples.trades) }}</strong></div>
              <div><span>胜 / 败</span><strong>{{ formatCount(report.samples.wins) }} / {{ formatCount(report.samples.losses) }}</strong></div>
              <div><span>未平仓<TermHint term="openPosition" /></span><strong>{{ formatCount(report.samples.open_positions) }}</strong></div>
              <div><span>入场信号日</span><strong>{{ formatCount(report.samples.signal_days) }}</strong></div>
              <div><span>涉及标的</span><strong>{{ formatCount(report.samples.stocks) }}</strong></div>
              <div><span>平均持有（交易日）</span><strong>{{ formatNumber(report.samples.holding_days, 1) }}</strong></div>
              <div><span>通过校正<TermHint term="factorSignificant" /></span><strong data-testid="attribution-significant-count">{{ report.significant_count }} / {{ report.value_count }}</strong></div>
            </div>

            <p v-if="report.reason" class="attribution-reason" data-testid="attribution-reason">{{ report.reason }}</p>

            <ul v-if="report.notes.length" class="attribution-notes" data-testid="attribution-notes">
              <li v-for="note in report.notes" :key="note">{{ note }}</li>
            </ul>

            <ul v-if="report.boundaries.length" class="attribution-boundaries" data-testid="attribution-boundaries">
              <li v-for="line in report.boundaries" :key="line">{{ line }}</li>
            </ul>

            <div v-if="!report.factors.length" class="inline-empty">没有指标形成可用样本。</div>
            <div v-else class="table-wrap">
              <table>
                <caption class="sr-only">因子归因结果</caption>
                <thead>
                  <tr>
                    <th class="attribution-nowrap">指标</th>
                    <th class="attribution-nowrap">胜组均值</th>
                    <th class="attribution-nowrap">败组均值</th>
                    <th class="attribution-nowrap">差值</th>
                    <th class="attribution-nowrap">标准化差<TermHint term="standardizedDifference" /></th>
                    <th class="attribution-nowrap">胜 / 败笔数</th>
                    <th class="attribution-nowrap">缺失</th>
                    <th class="attribution-nowrap">p 值</th>
                    <th class="attribution-nowrap">校正后 p</th>
                    <th class="attribution-nowrap">结论</th>
                    <th class="attribution-nowrap">操作</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="factor in report.factors" :key="factor.value_id" :data-testid="`attribution-row-${factor.value_id}`">
                    <td class="attribution-name">
                      {{ factor.display_name }}
                      <span v-if="factor.significant" class="attribution-badge" data-testid="attribution-significant-badge">通过</span>
                    </td>
                    <td>{{ formatNumber(factor.win_mean) }}</td>
                    <td>{{ formatNumber(factor.loss_mean) }}</td>
                    <td>{{ formatNumber(factor.difference) }}</td>
                    <td>{{ formatNumber(factor.standardized, 2) }}</td>
                    <td class="attribution-nowrap">{{ factor.win_count }} / {{ factor.loss_count }}</td>
                    <td>{{ factor.missing }}</td>
                    <td>{{ formatNumber(factor.p_value) }}</td>
                    <td>{{ formatNumber(factor.adjusted_p_value) }}</td>
                    <td class="attribution-note">{{ verdictText(factor) }}</td>
                    <td>
                      <button
                        class="table-link"
                        type="button"
                        :data-testid="`attribution-to-portfolio-${factor.value_id}`"
                        :disabled="!canApplyAttributionFactor(factor)"
                        :title="canApplyAttributionFactor(factor) ? '按胜组与败组均值差推断排序方向' : '该指标没有可用方向或不可用于跨标的排序'"
                        @click="handoffAttributionFactor(factor)"
                      >带入排序</button>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </template>
        </section>
      </div>

      <div v-if="loading && job" class="task-progress task-progress-panel" data-testid="attribution-progress">
        <div class="task-progress-heading"><span>因子归因进度</span><strong>{{ Math.round(job.progress * 100) }}%</strong></div>
        <div class="progress-track"><span :style="{ width: `${Math.round(job.progress * 100)}%` }"></span></div>
      </div>

      <p v-if="strongest" class="scope-helper">区分最强的一条是 {{ strongest.display_name }}：{{ verdictText(strongest) }}。</p>

      <BacktestHistoryPanel
        :items="history"
        :loading="historyLoading"
        testid="attribution-history"
        title="历史因子归因"
        empty-text="暂无历史因子归因。"
        @refresh="loadHistory"
        @load="loadHistoryDetail"
        @remove="removeHistory"
      />
    </main>
  </div>
</template>

<style scoped>
.attribution-value-actions {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  flex-wrap: wrap;
  margin-bottom: 0.5rem;
}

.attribution-inline-check {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  font-size: 0.85rem;
}

.attribution-value-list {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
  gap: 0.25rem 0.75rem;
  max-height: 220px;
  overflow-y: auto;
  border: 1px solid var(--border-subtle, rgba(148, 163, 184, 0.25));
  border-radius: 8px;
  padding: 0.5rem 0.75rem;
}

.attribution-value-item {
  display: flex;
  align-items: center;
  gap: 0.35rem;
  font-size: 0.85rem;
}

.attribution-reason {
  margin: 0.6rem 0 0.4rem;
  font-size: 0.9rem;
}

.attribution-notes,
.attribution-boundaries {
  margin: 0 0 0.75rem;
  padding-left: 1.1rem;
  font-size: 0.8rem;
  color: var(--text-muted, #64748b);
}

/* 中文表头会逐字换行，短标签统一不换行，放不下交给表格横向滚动。 */
.attribution-nowrap {
  white-space: nowrap;
}

.attribution-name {
  min-width: 9rem;
}

.attribution-note {
  font-size: 0.8rem;
  color: var(--text-muted, #64748b);
}

/* 描边而非绿底：这是「通过校正」的标记，不是涨跌，染成红/绿会被读成收益。 */
.attribution-badge {
  margin-left: 0.35rem;
  padding: 0.05rem 0.35rem;
  border: 1px solid var(--accent);
  border-radius: 999px;
  font-size: 0.68rem;
  color: var(--accent);
  white-space: nowrap;
}
</style>
