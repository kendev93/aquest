<script setup lang="ts">
import PageGuide from '../components/PageGuide.vue'
import { computed, onMounted, reactive, ref, watch } from 'vue'

import {
  SwingResearchApiError,
  deleteSwingResearchHistory,
  fetchSwingResearchHistory,
  getSwingResearchHistory,
  runSwingIndicatorStudy,
  runSwingOverview,
  type SwingHistoryRecord,
  type SwingProgress,
} from '../api/swingResearch'
import TermHint from '../components/TermHint.vue'
import type {
  BacktestHistorySummary,
  SwingConditionTemplate,
  SwingLabel,
  SwingOverviewResult,
  SwingScenario,
  SwingStudyResult,
} from '../types'
import { saveSwingPlan } from '../utils/swingHandoff'
import { formatChinaTimestamp } from '../utils/time'

/** 第一步表单的存档形状。存的是表单本身而不是请求体，回填时才能直接赋值。 */
interface OverviewFormSnapshot {
  universe?: 'all' | 'sh' | 'sz'
  startDate?: string
  endDate?: string
  multiplesText?: string
  minBars?: number
  atrWindow?: number
  limitStocks?: number
}

const SWING_LABELS: SwingLabel[] = [
  'up_onset_ahead',
  'down_onset_ahead',
  'up_leg_start',
  'down_leg_start',
]

interface LabelOption {
  id: SwingLabel
  name: string
  tradeable: boolean
}

const LABEL_OPTIONS: LabelOption[] = [
  { id: 'up_onset_ahead', name: '近期将启动上涨', tradeable: true },
  { id: 'down_onset_ahead', name: '近期将启动下跌', tradeable: true },
  { id: 'up_leg_start', name: '波段最低点（事后归因）', tradeable: false },
  { id: 'down_leg_start', name: '波段最高点（事后归因）', tradeable: false },
]

/**
 * 默认回看 5 年。实测（300 只样本）：2 年→5 年噪声门槛从 0.0366 降到 0.0171（腰斩），
 * 5 年→8 年只从 0.0171 降到 0.0145，但耗时涨一半 —— 5 年是性价比拐点。
 * 阈值也一样：买入阈值 2 年 -0.159、5 年 -0.239、8 年 -0.237，5 年之后就不再漂了。
 */
function defaultStartDate(): string {
  const target = new Date()
  target.setFullYear(target.getFullYear() - 5)
  const month = String(target.getMonth() + 1).padStart(2, '0')
  const day = String(target.getDate()).padStart(2, '0')
  return `${target.getFullYear()}-${month}-${day}`
}

const overview = reactive({
  universe: 'all' as 'all' | 'sh' | 'sz',
  startDate: defaultStartDate(),
  endDate: '',
  multiplesText: '2, 3, 4',
  minBars: 5,
  atrWindow: 14,
  limitStocks: 1000,
})

const study = reactive({
  universe: 'all' as 'all' | 'sh' | 'sz',
  startDate: defaultStartDate(),
  endDate: '',
  reversalMultiple: 3,
  minBars: 5,
  atrWindow: 14,
  horizon: 10,
  // 四个都默认勾上。只勾「近期将启动」时，看起来像在研究「什么时候启动」，
  // 但真正研究顶和底的是下面那两条——它们的名字必须说清自己打在极值点上。
  labels: [
    'up_onset_ahead',
    'down_onset_ahead',
    'up_leg_start',
    'down_leg_start',
  ] as SwingLabel[],
  permutations: 200,
  blockSize: 20,
  alpha: 0.05,
  minCrossSection: 20,
  limitStocks: 1000,
  includeCustom: true,
})

const message = ref('')
const overviewRunning = ref(false)
const studyRunning = ref(false)
const overviewResult = ref<SwingOverviewResult | null>(null)
const studyResult = ref<SwingStudyResult | null>(null)
const copied = ref(false)
const overviewProgress = ref<SwingProgress | null>(null)
const studyProgress = ref<SwingProgress | null>(null)
// 历史列表：后端已按完成时间由近到远排好，这里只管切片分页。
const history = ref<BacktestHistorySummary[]>([])
const historyLoading = ref(false)
const activeHistoryId = ref('')
const historyPage = ref(1)
const historyPageSize = 10

function isTradeable(label: SwingLabel): boolean {
  return LABEL_OPTIONS.find((item) => item.id === label)?.tradeable ?? false
}

function labelName(label: SwingLabel): string {
  return LABEL_OPTIONS.find((item) => item.id === label)?.name ?? label
}

function parseMultiples(text: string): number[] {
  const values = text
    .split(/[,，\s]+/)
    .map((item) => Number(item.trim()))
    .filter((item) => Number.isFinite(item) && item > 0 && item <= 20)
  return values.length ? values : [3]
}

function optionalDate(value: string): string | null {
  return value.trim() ? value.trim() : null
}

function errorMessage(error: unknown): string {
  if (error instanceof SwingResearchApiError) return error.message
  if (error instanceof Error) return error.message
  return '波段研究失败，请检查参数后重试。'
}

async function runOverview(): Promise<void> {
  overviewRunning.value = true
  message.value = ''
  overviewProgress.value = { current: 0, total: 0, stage: '准备中', ratio: 0 }
  try {
    overviewResult.value = await runSwingOverview(
      {
        universe: overview.universe,
        start_date: optionalDate(overview.startDate),
        end_date: optionalDate(overview.endDate),
        reversal_multiples: parseMultiples(overview.multiplesText),
        min_bars: overview.minBars,
        atr_window: overview.atrWindow,
        limit_stocks: overview.limitStocks > 0 ? overview.limitStocks : null,
      },
      (progress) => {
        overviewProgress.value = progress
      },
    )
  } catch (error) {
    message.value = errorMessage(error)
  } finally {
    overviewRunning.value = false
  }
}

async function runStudy(): Promise<void> {
  studyRunning.value = true
  message.value = ''
  studyProgress.value = { current: 0, total: 0, stage: '准备中', ratio: 0 }
  try {
    const outcome = await runSwingIndicatorStudy(
      {
        universe: study.universe,
        start_date: optionalDate(study.startDate),
        end_date: optionalDate(study.endDate),
        reversal_multiple: study.reversalMultiple,
        min_bars: study.minBars,
        atr_window: study.atrWindow,
        horizon: study.horizon,
        labels: study.labels,
        include_custom: study.includeCustom,
        permutations: study.permutations,
        block_size: study.blockSize,
        alpha: study.alpha,
        min_cross_section: study.minCrossSection,
        limit_stocks: study.limitStocks > 0 ? study.limitStocks : null,
        // 第一步的表单与结果只活在浏览器里，不一起存档的话，加载历史后
        // 第一步的校准表会是空的。后端只负责原样存起来。
        overview_request: { ...overview },
        overview_result: overviewResult.value,
      },
      (progress) => {
        studyProgress.value = progress
      },
    )
    studyResult.value = outcome.result
    await loadHistory()
    if (outcome.historyId) {
      // 新记录排在最前面；不回到第一页的话，看起来就像这次没被保存。
      historyPage.value = 1
      activeHistoryId.value = outcome.historyId
    }
  } catch (error) {
    message.value = errorMessage(error)
  } finally {
    studyRunning.value = false
  }
}

async function loadHistory(): Promise<void> {
  historyLoading.value = true
  try {
    history.value = await fetchSwingResearchHistory()
  } catch (error) {
    message.value = errorMessage(error)
  } finally {
    historyLoading.value = false
  }
}

function finiteOr(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback
}

function assignUniverse(
  target: { universe: 'all' | 'sh' | 'sz' },
  value: unknown,
): void {
  if (value === 'all' || value === 'sh' || value === 'sz') target.universe = value
}

/**
 * 把历史记录里存的两步参数回填进表单。
 *
 * 第一步优先用存档的表单快照（连候选阈值那种纯界面的输入都还原）；老记录
 * 没有快照时，用第二步的同名参数凑一个 —— 这两步的口径本来就是一致的。
 */
function applyStoredForm(record: SwingHistoryRecord): void {
  const request = record.request ?? {}
  assignUniverse(study, request.universe)
  study.startDate = typeof request.start_date === 'string' ? request.start_date : ''
  study.endDate = typeof request.end_date === 'string' ? request.end_date : ''
  study.reversalMultiple = finiteOr(request.reversal_multiple, study.reversalMultiple)
  study.minBars = finiteOr(request.min_bars, study.minBars)
  study.atrWindow = finiteOr(request.atr_window, study.atrWindow)
  study.horizon = finiteOr(request.horizon, study.horizon)
  study.permutations = finiteOr(request.permutations, study.permutations)
  study.blockSize = finiteOr(request.block_size, study.blockSize)
  study.alpha = finiteOr(request.alpha, study.alpha)
  study.minCrossSection = finiteOr(request.min_cross_section, study.minCrossSection)
  study.limitStocks = finiteOr(request.limit_stocks, study.limitStocks)
  if (typeof request.include_custom === 'boolean') study.includeCustom = request.include_custom
  const labels = Array.isArray(request.labels)
    ? request.labels.filter((item): item is SwingLabel => SWING_LABELS.includes(item as SwingLabel))
    : []
  // 一个标签都不剩会让「运行」按钮禁用，宁可保持原来的选择。
  if (labels.length) study.labels = labels

  const snapshot = (request.overview_request ?? null) as OverviewFormSnapshot | null
  assignUniverse(overview, snapshot?.universe)
  overview.startDate = typeof snapshot?.startDate === 'string'
    ? snapshot.startDate
    : study.startDate
  overview.endDate = typeof snapshot?.endDate === 'string' ? snapshot.endDate : study.endDate
  overview.multiplesText = typeof snapshot?.multiplesText === 'string'
    ? snapshot.multiplesText
    : String(study.reversalMultiple)
  overview.minBars = finiteOr(snapshot?.minBars, study.minBars)
  overview.atrWindow = finiteOr(snapshot?.atrWindow, study.atrWindow)
  overview.limitStocks = finiteOr(snapshot?.limitStocks, study.limitStocks)
}

async function loadHistoryDetail(historyId: string): Promise<void> {
  if (!historyId) return
  try {
    const record = await getSwingResearchHistory(historyId)
    applyStoredForm(record)
    overviewResult.value = record.request?.overview_result ?? null
    studyResult.value = record.result ?? null
    activeHistoryId.value = historyId
    message.value = '已加载历史波段研究，并还原当时两步的参数与结论。'
  } catch (error) {
    message.value = errorMessage(error)
  }
}

/**
 * 删除一条历史。页面上的结果刻意保留：删的是存档，不是已经算出来的结论。
 */
async function removeHistory(historyId: string): Promise<void> {
  try {
    const deleted = await deleteSwingResearchHistory(historyId)
    if (!deleted) {
      message.value = '这条历史记录不存在，或已经删掉了。'
      await loadHistory()
      return
    }
    if (activeHistoryId.value === historyId) activeHistoryId.value = ''
    await loadHistory()
    message.value = '历史记录已删除；页面上的结果保持不变。'
  } catch (error) {
    message.value = errorMessage(error)
  }
}

const historyPageCount = computed(() => Math.max(1, Math.ceil(history.value.length / historyPageSize)))
const historyPageRows = computed(() => history.value.slice(
  (historyPage.value - 1) * historyPageSize,
  historyPage.value * historyPageSize,
))
// 删到只剩一页时把页码收回来，否则会停在一个空白页上。
watch(historyPageCount, () => {
  if (historyPage.value > historyPageCount.value) historyPage.value = historyPageCount.value
})

/** created_at 存的是朴素 UTC 时间，不换算会比北京时间慢 8 小时。 */
function formatHistoryDate(value: string): string {
  return formatChinaTimestamp(value)
}

function summaryNumber(item: BacktestHistorySummary, key: string): number | null {
  const value = item.summary?.[key]
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function summaryStrings(item: BacktestHistorySummary, key: string): string[] {
  const value = item.summary?.[key]
  if (!Array.isArray(value)) return []
  return value.filter((item): item is string => typeof item === 'string' && item.length > 0)
}

function summaryIndicator(item: BacktestHistorySummary): Record<string, unknown> | null {
  const value = item.summary?.top_indicator
  return value && typeof value === 'object' ? value as Record<string, unknown> : null
}

function indicatorNumber(item: BacktestHistorySummary, key: string): number | null {
  const value = summaryIndicator(item)?.[key]
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function indicatorText(item: BacktestHistorySummary, key: string): string {
  const value = summaryIndicator(item)?.[key]
  return typeof value === 'string' && value ? value : ''
}

function historyRangeText(item: BacktestHistorySummary): string {
  const range = item.summary?.date_range
  if (!range || typeof range !== 'object') return '区间未知'
  const start = typeof (range as Record<string, unknown>).start === 'string'
    ? String((range as Record<string, unknown>).start)
    : ''
  const end = typeof (range as Record<string, unknown>).end === 'string'
    ? String((range as Record<string, unknown>).end)
    : ''
  if (!start && !end) return '区间未知'
  return `${start || '起点'} → ${end || '最新'}`
}

function historyUniverseText(item: BacktestHistorySummary): string {
  const universe = item.summary?.universe
  if (universe === 'sh') return '仅上海'
  if (universe === 'sz') return '仅深圳'
  if (universe === 'all') return '沪深全部'
  return '范围未知'
}

function historyDurationText(item: BacktestHistorySummary): string {
  const seconds = item.duration_seconds
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) return ''
  if (seconds < 60) return `耗时 ${Math.round(seconds)} 秒`
  return `耗时 ${Math.round(seconds / 60)} 分钟`
}

onMounted(() => {
  void loadHistory()
})

function percentOf(progress: SwingProgress | null): number {
  return progress ? Math.round(progress.ratio * 100) : 0
}

function toggleLabel(label: SwingLabel): void {
  study.labels = study.labels.includes(label)
    ? study.labels.filter((item) => item !== label)
    : [...study.labels, label]
}

function formatNumber(value: number | null | undefined, digits = 2): string {
  return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(digits) : '—'
}

function formatPercent(value: number | null | undefined, digits = 1): string {
  return typeof value === 'number' && Number.isFinite(value)
    ? `${value.toFixed(digits)}%`
    : '—'
}

function median(stats: Record<string, number | null> | undefined): string {
  if (!stats) return '—'
  return formatNumber(stats.p50, 1)
}

/** 后端振幅统计已换算成百分数（8.0 就是 8%），直接显示即可。 */
function percentText(value: number | null | undefined): string {
  return typeof value === 'number' && Number.isFinite(value) ? formatPercent(value) : '—'
}

function rangeText(start: string | null, end: string | null): string {
  if (!start && !end) return '—'
  return `${start ?? '起点'} → ${end ?? '最新'}`
}

function scenarioLabel(scenario: SwingScenario): string {
  return `${formatNumber(scenario.reversal_multiple, 1)} 倍`
}

function barWidth(count: number, buckets: { count: number }[]): string {
  const peak = buckets.reduce((max, item) => Math.max(max, item.count), 0)
  if (peak <= 0 || count <= 0) return '0%'
  return `${Math.round((count / peak) * 100)}%`
}

const plan = computed(() => studyResult.value?.condition_plan ?? null)

const noiseFloor = computed<number | null>(() => {
  const scores = studyResult.value?.scores ?? []
  const values = scores
    .map((item) => item.ic_threshold)
    .filter((item): item is number => typeof item === 'number')
  return values.length ? Math.max(...values) : null
})

function operatorText(operator: string | null | undefined): string {
  return operator === 'gte' ? '≥' : '≤'
}

/** 技术面指标用短名，自定义指标用中文名（id 太长也看不懂）。 */
function conditionText(template: SwingConditionTemplate): string {
  const id = template.value_id.startsWith('technical.')
    ? template.value_id.slice('technical.'.length)
    : template.display_name
  return `${id} ${operatorText(template.operator)} ${formatNumber(template.threshold, 4)}`
}

function handoffToResearch(): void {
  const current = plan.value
  if (!current) return
  saveSwingPlan({
    buy_rule: current.buy_rule,
    sell_rule: current.sell_rule,
    ranking: current.ranking
      ? {
          value: current.ranking.value,
          order: current.ranking.order,
          display_name: current.ranking.display_name,
        }
      : null,
  })
}

async function copyPlan(): Promise<void> {
  const current = plan.value
  if (!current) return
  try {
    await navigator.clipboard.writeText(
      JSON.stringify(
        {
          buy_rule: current.buy_rule,
          sell_rule: current.sell_rule,
          ranking: current.ranking,
        },
        null,
        2,
      ),
    )
    copied.value = true
    window.setTimeout(() => {
      copied.value = false
    }, 2000)
  } catch {
    message.value = '浏览器不允许写入剪贴板，请手动选中规则文本复制。'
  }
}
</script>

<template>
  <div class="screen-page swing-research-page" data-testid="swing-research-page">
    <header class="topbar">
      <a class="brand" href="/indicator-lab" aria-label="指标实验室首页">
        <img class="brand-mark" src="/indicator-lab-mark.png" alt="" aria-hidden="true" width="34" height="34">
        <span><strong>Indicator Lab</strong><small>指标实验室</small></span>
      </a>
      <nav aria-label="主导航">
        <a class="nav-link" href="/indicator-lab">公式选股</a>
        <a class="nav-link" href="/portfolio-backtest">组合回测</a>
        <a class="nav-link" href="/strategy-lab">策略实验室</a>
        <a class="nav-link" href="/condition-research">买卖条件研究</a>
        <a class="nav-link" href="/strategy-fitness">策略适配性</a>
        <a class="nav-link" href="/market-data">本地行情</a>
        <a class="nav-link active" href="/swing-research" aria-current="page">波段研究</a>
        <a class="nav-link" href="/strategy-tracking">策略追踪</a>
        <a class="nav-link" href="/factor-test">因子检验</a>
        <a class="nav-link" href="/history-compare">历史对比</a>
        <a class="nav-link" href="/instrument-lab">标的检验台</a>
        <a class="nav-link" href="/candidate-library">候选库</a>
        <a class="nav-link" href="/factor-attribution">因子归因</a>
        <a class="nav-link" href="/user-manual">使用手册</a>
      </nav>
      <div class="topbar-status"><span class="status-dot" aria-hidden="true"></span> 本地数据模式</div>
    </header>

    <main class="content-shell">
      <section class="page-intro">
        <div>
          <p class="eyebrow">SWING RESEARCH / SEGMENTS</p>
          <h1>波段研究</h1>
          <p class="intro-copy">先把行情切成一段段涨跌，再看哪些指标在这些段的起点附近出现异常值。</p>
        </div>
        <div class="intro-note">
          <span class="note-label">它能回答什么</span>
          <strong>哪些指标伴随波段启动</strong>
          <small>给的是相关性排序，不是交易信号；结论要拿去买卖条件研究重新搜阈值才算数</small>
        </div>
      </section>

      <PageGuide page-key="swing-research" />

      <div v-if="message" class="notice error" role="alert" data-testid="swing-message">{{ message }}</div>

      <section class="panel swing-panel" data-testid="swing-overview-panel">
        <div class="panel-heading">
          <div><span class="section-kicker">01 / SEGMENTATION</span><h2>第一步 · 波段切分校准</h2></div>
          <span class="required-hint">先在少量样本上定阈值</span>
        </div>
        <p class="helper">回撤多少个 <TermHint term="swingThreshold" />算换方向，决定了后面切出多少段。先用 1000 只左右跑一遍，看每个阈值下的段数与振幅，再决定第二步用哪个。日期默认回看 5 年；清空开始日期会用到库里 1998 年以来的全部数据，耗时会成倍增长。</p>

        <div class="compact-fields swing-fields">
          <div>
            <label for="swing-overview-universe">市场范围</label>
            <select id="swing-overview-universe" v-model="overview.universe" data-testid="swing-overview-universe">
              <option value="all">沪深全部</option>
              <option value="sh">仅上海</option>
              <option value="sz">仅深圳</option>
            </select>
          </div>
          <div>
            <label for="swing-overview-multiples">候选阈值（逗号分隔）</label>
            <input id="swing-overview-multiples" v-model="overview.multiplesText" data-testid="swing-overview-multiples" type="text" inputmode="decimal">
          </div>
          <div>
            <label for="swing-overview-min-bars">最小持续天数</label>
            <input id="swing-overview-min-bars" v-model.number="overview.minBars" data-testid="swing-overview-min-bars" type="number" min="0" max="1000">
          </div>
          <div>
            <label for="swing-overview-atr">ATR 窗口</label>
            <input id="swing-overview-atr" v-model.number="overview.atrWindow" data-testid="swing-overview-atr" type="number" min="1" max="1000">
          </div>
          <div>
            <label for="swing-overview-start">开始日期（留空=全部历史）</label>
            <input id="swing-overview-start" v-model="overview.startDate" data-testid="swing-overview-start" type="date">
          </div>
          <div>
            <label for="swing-overview-end">结束日期（留空=最新）</label>
            <input id="swing-overview-end" v-model="overview.endDate" data-testid="swing-overview-end" type="date">
          </div>
          <div>
            <label for="swing-overview-limit">样本上限（只）</label>
            <input id="swing-overview-limit" v-model.number="overview.limitStocks" data-testid="swing-overview-limit" type="number" min="1" max="100000">
          </div>
        </div>

        <button class="primary-button" type="button" :disabled="overviewRunning" data-testid="swing-overview-run" @click="runOverview">
          <span v-if="overviewRunning" class="spinner" aria-hidden="true"></span>
          {{ overviewRunning ? '切分中…' : '运行切分校准' }}
        </button>

        <div v-if="overviewRunning" class="swing-progress" data-testid="swing-overview-progress">
          <div class="swing-progress-track">
            <div class="swing-progress-fill" :style="{ width: percentOf(overviewProgress) + '%' }"></div>
          </div>
          <p class="swing-progress-text">{{ overviewProgress?.stage ?? '准备中' }} · {{ percentOf(overviewProgress) }}%</p>
        </div>

        <div v-if="overviewResult" class="swing-result" data-testid="swing-overview-result">
          <div class="metrics-strip">
            <div><span>命中标的</span><strong>{{ overviewResult.stock_count }}</strong></div>
            <div><span>已扫描</span><strong>{{ overviewResult.scanned_stocks }}</strong></div>
            <div><span>覆盖区间</span><strong>{{ rangeText(overviewResult.date_range.start, overviewResult.date_range.end) }}</strong></div>
            <div><span>年数</span><strong>{{ formatNumber(overviewResult.date_range.years, 1) }}</strong></div>
          </div>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>波段阈值</th>
                  <th>段数</th>
                  <th>达标段数</th>
                  <th>每股每年段数</th>
                  <th>上涨段</th>
                  <th>下跌段</th>
                  <th>持续天数中位数</th>
                  <th>振幅中位数</th>
                  <th>分布</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="scenario in overviewResult.scenarios" :key="scenario.reversal_multiple" data-testid="swing-scenario-row">
                  <td><strong class="code">{{ scenarioLabel(scenario) }}</strong></td>
                  <td>{{ scenario.segment_count }}</td>
                  <td>{{ scenario.long_segment_count }}</td>
                  <td>{{ formatNumber(scenario.segments_per_stock_per_year, 2) }}</td>
                  <td>{{ scenario.up.count }}</td>
                  <td>{{ scenario.down.count }}</td>
                  <td>{{ median(scenario.up.duration) }} / {{ median(scenario.down.duration) }}</td>
                  <td>{{ percentText(scenario.up.amplitude?.p50) }} / {{ percentText(scenario.down.amplitude?.p50) }}</td>
                  <td>
                    <details class="swing-dist">
                      <summary>振幅分布</summary>
                      <div class="swing-dist-grid">
                        <div>
                          <span class="swing-dist-title">上涨</span>
                          <div v-for="bucket in scenario.up.amplitude_histogram" :key="`up-${bucket.bucket}`" class="swing-bar-row">
                            <span class="swing-bar-label">{{ bucket.bucket }}%</span>
                            <span class="swing-bar-track"><i :style="{ width: barWidth(bucket.count, scenario.up.amplitude_histogram) }"></i></span>
                            <span class="swing-bar-value">{{ bucket.count }}</span>
                          </div>
                        </div>
                        <div>
                          <span class="swing-dist-title">下跌</span>
                          <div v-for="bucket in scenario.down.amplitude_histogram" :key="`down-${bucket.bucket}`" class="swing-bar-row">
                            <span class="swing-bar-label">{{ bucket.bucket }}%</span>
                            <span class="swing-bar-track"><i :style="{ width: barWidth(bucket.count, scenario.down.amplitude_histogram) }"></i></span>
                            <span class="swing-bar-value">{{ bucket.count }}</span>
                          </div>
                        </div>
                      </div>
                    </details>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <p class="helper">「达标段数」是持续天数达到最小值的段；振幅一列是上涨 / 下跌两个方向的中位数。</p>
        </div>
      </section>

      <section class="panel swing-panel" data-testid="swing-study-panel">
        <div class="panel-heading">
          <div><span class="section-kicker">02 / INDICATOR STUDY</span><h2>第二步 · 指标与波段的关系</h2></div>
          <span class="required-hint">同步计算，样本越大越慢</span>
        </div>
        <p class="helper">逐日把所有股票按指标排名，再看它们的<TermHint term="ic" />，并用<TermHint term="blockPermutation" />给出<TermHint term="noiseFloor" />。默认 1000 只 × 近 5 年需要一到三分钟，期间页面会一直等着；全市场 5000 只以上会内存溢出。5 年这个长度是实测出来的拐点：再拉长噪声门槛只会从 0.017 降到 0.014，但耗时翻倍。</p>

        <div class="compact-fields swing-fields">
          <div>
            <label for="swing-study-universe">市场范围</label>
            <select id="swing-study-universe" v-model="study.universe" data-testid="swing-study-universe">
              <option value="all">沪深全部</option>
              <option value="sh">仅上海</option>
              <option value="sz">仅深圳</option>
            </select>
          </div>
          <div>
            <label for="swing-study-multiple">波段阈值（倍 ATR）</label>
            <input id="swing-study-multiple" v-model.number="study.reversalMultiple" data-testid="swing-study-multiple" type="number" min="0.1" max="20" step="0.5">
          </div>
          <div>
            <label for="swing-study-min-bars">最小持续天数</label>
            <input id="swing-study-min-bars" v-model.number="study.minBars" data-testid="swing-study-min-bars" type="number" min="0" max="1000">
          </div>
          <div>
            <label for="swing-study-atr">ATR 窗口</label>
            <input id="swing-study-atr" v-model.number="study.atrWindow" data-testid="swing-study-atr" type="number" min="1" max="1000">
          </div>
          <div>
            <label for="swing-study-horizon">预测窗口（交易日）</label>
            <input id="swing-study-horizon" v-model.number="study.horizon" data-testid="swing-study-horizon" type="number" min="1" max="250">
          </div>
          <div>
            <label for="swing-study-permutations">置换次数</label>
            <input id="swing-study-permutations" v-model.number="study.permutations" data-testid="swing-study-permutations" type="number" min="0" max="5000">
          </div>
          <div>
            <label for="swing-study-block">分块长度（交易日）</label>
            <input id="swing-study-block" v-model.number="study.blockSize" data-testid="swing-study-block" type="number" min="1" max="500">
          </div>
          <div>
            <label for="swing-study-alpha">显著性水平</label>
            <input id="swing-study-alpha" v-model.number="study.alpha" data-testid="swing-study-alpha" type="number" min="0.01" max="0.5" step="0.01">
          </div>
          <div>
            <label for="swing-study-cross">每日最少样本数</label>
            <input id="swing-study-cross" v-model.number="study.minCrossSection" data-testid="swing-study-cross" type="number" min="2" max="10000">
          </div>
          <div>
            <label for="swing-study-start">开始日期（留空=全部历史）</label>
            <input id="swing-study-start" v-model="study.startDate" data-testid="swing-study-start" type="date">
          </div>
          <div>
            <label for="swing-study-end">结束日期（留空=最新）</label>
            <input id="swing-study-end" v-model="study.endDate" data-testid="swing-study-end" type="date">
          </div>
          <div>
            <label for="swing-study-limit">样本上限（只）</label>
            <input id="swing-study-limit" v-model.number="study.limitStocks" data-testid="swing-study-limit" type="number" min="1" max="100000">
          </div>
        </div>

        <fieldset class="form-section swing-label-section">
          <legend>波段标签<TermHint term="swingLabel" /></legend>
          <div class="scope-options">
            <label v-for="option in LABEL_OPTIONS" :key="option.id" class="scope-option">
              <input
                type="checkbox"
                :checked="study.labels.includes(option.id)"
                :data-testid="`swing-label-${option.id}`"
                @change="toggleLabel(option.id)"
              >
              {{ option.name }}
              <span v-if="!option.tradeable" class="swing-tag muted">归因</span>
              <span v-else class="swing-tag">可交易</span>
            </label>
          </div>
          <p class="helper">默认四个都勾：「近期将启动」当天就算得出来，能直接拿去交易；「波段最低点 / 最高点」只有事后才知道，用来回答顶和底长什么样。注意后者的样本少得多——一波行情才有一个转折点。</p>
        </fieldset>

        <fieldset class="form-section swing-label-section">
          <legend>自定义指标</legend>
          <label class="scope-option">
            <input v-model="study.includeCustom" data-testid="swing-study-include-custom" type="checkbox">
            把主力清洗 / 庄家顶底 / 准备拉升 / 主力吸筹 / 突破信号一起研究
          </label>
          <p class="helper">
            旧公式只纳入经过筛选的输出；主力吸筹和突破信号也已加入。新指标里的进场、洗盘、突破、破位是事件线，
            可能大多数股票当天都是 0，导致横截面样本偏少或并列过多。请结合每项结果的有效样本数判断，不要只看相关性。
          </p>
        </fieldset>

        <button class="primary-button" type="button" :disabled="studyRunning || study.labels.length === 0" data-testid="swing-study-run" @click="runStudy">
          <span v-if="studyRunning" class="spinner" aria-hidden="true"></span>
          {{ studyRunning ? '计算中…' : '运行指标研究' }}
        </button>

        <div v-if="studyRunning" class="swing-progress" data-testid="swing-study-progress">
          <div class="swing-progress-track">
            <div class="swing-progress-fill" :style="{ width: percentOf(studyProgress) + '%' }"></div>
          </div>
          <p class="swing-progress-text">{{ studyProgress?.stage ?? '准备中' }} · {{ percentOf(studyProgress) }}%</p>
        </div>

        <div v-if="studyResult" class="swing-result" data-testid="swing-study-result">
          <div class="metrics-strip">
            <div><span>命中标的</span><strong>{{ studyResult.scanned_stocks }}<small> / {{ studyResult.stock_count }}</small></strong></div>
            <div><span>交易日</span><strong>{{ studyResult.date_range.days }}</strong></div>
            <div><span>覆盖区间</span><strong>{{ rangeText(studyResult.date_range.start, studyResult.date_range.end) }}</strong></div>
            <div><span>显著结论</span><strong :class="{ positive: studyResult.significant_count > 0 }">{{ studyResult.significant_count }}<small> / {{ studyResult.scores.length }}</small></strong></div>
            <div><span>噪声门槛（IC）</span><strong>{{ formatNumber(noiseFloor, 3) }}</strong></div>
            <div><span>可交易条数</span><strong>{{ plan?.tradeable_count ?? 0 }}</strong></div>
          </div>

          <div v-if="studyResult.reason" class="diagnostic" data-testid="swing-study-reason">{{ studyResult.reason }}</div>

          <section v-if="plan && (plan.buy_rule || plan.sell_rule || plan.ranking)" class="swing-plan" data-testid="swing-plan">
            <div class="section-heading"><h3>可直接带走的结论</h3></div>
            <div class="swing-plan-grid">
              <div class="swing-plan-card">
                <span class="swing-plan-title">买入条件</span>
                <p v-if="plan.buy_rule && plan.buy_rule.conditions.length" class="swing-plan-rule" data-testid="swing-plan-buy">
                  {{ plan.buy_rule.conditions[0].left_value.replace('technical.', '') }}
                  {{ operatorText(plan.buy_rule.conditions[0].operator) }}
                  {{ formatNumber(plan.buy_rule.conditions[0].right_constant, 4) }}
                </p>
                <p v-else class="swing-plan-empty">没有通过显著性检验的买入结论</p>
              </div>
              <div class="swing-plan-card">
                <span class="swing-plan-title">卖出条件</span>
                <p v-if="plan.sell_rule && plan.sell_rule.conditions.length" class="swing-plan-rule" data-testid="swing-plan-sell">
                  {{ plan.sell_rule.conditions[0].left_value.replace('technical.', '') }}
                  {{ operatorText(plan.sell_rule.conditions[0].operator) }}
                  {{ formatNumber(plan.sell_rule.conditions[0].right_constant, 4) }}
                </p>
                <p v-else class="swing-plan-empty">没有通过显著性检验的卖出结论</p>
              </div>
              <div class="swing-plan-card">
                <span class="swing-plan-title">排序建议<TermHint term="rankOrder" /></span>
                <p v-if="plan.ranking" class="swing-plan-rule" data-testid="swing-plan-ranking">
                  {{ plan.ranking.display_name }} · {{ plan.ranking.order === 'asc' ? '从小到大' : '从大到小' }}
                </p>
                <p v-else class="swing-plan-empty">暂不推荐排序指标</p>
              </div>
            </div>
            <p v-if="plan.reason" class="helper">{{ plan.reason }}</p>
            <p v-else class="helper">
              阈值取自横截面分位的中位数，会随行情强弱漂移。下面的按钮只是把它作为「起点」带去买卖条件研究，让那边在自己的三段数据上重新搜索。
            </p>
            <div class="swing-plan-actions">
              <a class="secondary-button swing-handoff" href="/condition-research" data-testid="swing-plan-handoff" @click="handoffToResearch">把结论带去买卖条件研究</a>
              <button class="text-button" type="button" data-testid="swing-plan-copy" @click="copyPlan">{{ copied ? '已复制' : '复制规则 JSON' }}</button>
            </div>
          </section>

          <div class="data-section">
            <div class="section-heading"><h3>指标打分（按显著性与信息系数排序）</h3><span class="required-hint">q 值 < 显著性水平 才算数</span></div>
            <div class="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>指标</th>
                    <th>波段标签</th>
                    <th>可交易</th>
                    <th>方向</th>
                    <th>平均信息系数<TermHint term="icMean" /></th>
                    <th>信息比率<TermHint term="icir" /></th>
                    <th>p 值</th>
                    <th>q 值<TermHint term="falseDiscovery" /></th>
                    <th>阈值提示<TermHint term="thresholdHint" /></th>
                    <th>结论</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="score in studyResult.scores" :key="`${score.value_id}-${score.label}`" data-testid="swing-score-row">
                    <td><strong>{{ score.display_name }}</strong><br><small class="muted-code">{{ score.value_id }}</small></td>
                    <td>{{ labelName(score.label) }}</td>
                    <td>
                      <span v-if="isTradeable(score.label)" class="swing-tag">可交易</span>
                      <span v-else class="swing-tag muted">归因</span>
                    </td>
                    <td>{{ score.order === 'high' ? '越大越相关' : '越小越相关' }}</td>
                    <td :class="{ positive: score.significant && score.ic_mean !== null && score.ic_mean > 0 }">{{ formatNumber(score.ic_mean, 3) }}</td>
                    <td>{{ formatNumber(score.icir, 2) }}</td>
                    <td>{{ formatNumber(score.pvalue, 3) }}</td>
                    <td>{{ formatNumber(score.qvalue, 3) }}</td>
                    <td>{{ formatNumber(score.threshold_hint, 4) }}</td>
                    <td :class="{ positive: score.significant, negative: !score.significant }">
                      {{ score.significant ? '通过检验' : '未通过检验' }}
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>

          <div v-if="plan && plan.templates.length" class="data-section">
            <div class="section-heading"><h3>可转换的条件模板</h3><span class="required-hint">一次只用一条，别叠加</span></div>
            <div class="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>方向</th>
                    <th>指标</th>
                    <th>标签</th>
                    <th>条件</th>
                    <th>可交易</th>
                    <th>平均信息系数</th>
                    <th>说明</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="template in plan.templates" :key="`${template.value_id}-${template.label}`" data-testid="swing-template-row">
                    <td><span class="direction" :class="template.side">{{ template.side === 'buy' ? '买' : '卖' }}</span></td>
                    <td>{{ template.display_name }}</td>
                    <td>{{ template.label_display }}</td>
                    <td class="code">
                      {{ conditionText(template) }}
                      <span v-if="!template.condition_supported" class="swing-tag muted" title="该输出还没登记成可筛选指标，只能照着阈值手动配置">仅参考</span>
                    </td>
                    <td>
                      <span v-if="template.tradeable" class="swing-tag">可交易</span>
                      <span v-else class="swing-tag muted">归因</span>
                    </td>
                    <td :class="{ positive: template.significant }">{{ formatNumber(template.ic_mean, 3) }}</td>
                    <td class="swing-note">{{ template.note }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </section>
      <section class="panel swing-panel swing-history" data-testid="swing-research-history">
        <div class="panel-heading">
          <div><span class="section-kicker">SAVED RESEARCH</span><h2>历史研究</h2></div>
          <div class="history-head-actions">
            <span v-if="historyLoading" class="history-status">读取中…</span>
            <button class="secondary-button" type="button" :disabled="historyLoading" data-testid="swing-history-refresh" @click="loadHistory">刷新</button>
          </div>
        </div>
        <p class="helper">第二步跑完会自动保存一条，只留最近 20 次。加载会还原当时两步的参数和结论，包括第一步的校准表。</p>
        <div v-if="!history.length" class="inline-empty">暂无历史研究记录。</div>
        <template v-else>
          <ul class="history-list">
            <li
              v-for="item in historyPageRows"
              :key="item.history_id"
              class="history-card"
              :class="{ 'history-card-active': activeHistoryId === item.history_id }"
              :data-testid="`swing-history-item-${item.history_id}`"
            >
              <div class="history-card-head">
                <strong>{{ formatHistoryDate(item.created_at) }}</strong>
                <span v-if="activeHistoryId === item.history_id" class="history-badge">当前</span>
                <span v-if="historyDurationText(item)" class="history-duration">{{ historyDurationText(item) }}</span>
              </div>
              <div class="history-row">
                <span class="history-chip">{{ historyRangeText(item) }}</span>
                <span class="history-chip">{{ historyUniverseText(item) }}</span>
                <span v-if="summaryNumber(item, 'stock_count') !== null" class="history-chip">{{ summaryNumber(item, 'stock_count') }} 只</span>
                <span v-if="summaryNumber(item, 'reversal_multiple') !== null" class="history-chip">{{ formatNumber(summaryNumber(item, 'reversal_multiple'), 1) }} 倍 ATR</span>
                <span v-if="summaryNumber(item, 'min_bars') !== null" class="history-chip">持续 ≥ {{ summaryNumber(item, 'min_bars') }} 天</span>
                <span v-if="summaryNumber(item, 'horizon') !== null" class="history-chip">预测 {{ summaryNumber(item, 'horizon') }} 日</span>
              </div>
              <div class="history-row">
                <span class="history-metric">显著 <strong>{{ summaryNumber(item, 'significant_count') ?? 0 }} / {{ summaryNumber(item, 'value_count') ?? 0 }}</strong></span>
                <span class="history-metric">最强 <strong class="history-condition">{{ indicatorText(item, 'display_name') || '—' }}</strong></span>
                <span class="history-metric">信息系数 <strong>{{ formatNumber(indicatorNumber(item, 'ic_mean'), 3) }}</strong></span>
                <span class="history-metric">噪声门槛 <strong>{{ formatNumber(summaryNumber(item, 'noise_floor'), 3) }}</strong></span>
                <span class="history-metric">可交易 <strong>{{ summaryNumber(item, 'tradeable_count') ?? 0 }}</strong> 条</span>
                <span v-for="label in summaryStrings(item, 'label_names')" :key="label" class="history-chip">{{ label }}</span>
              </div>
              <div class="history-card-actions">
                <button class="table-link" type="button" :disabled="historyLoading" :data-testid="`swing-history-load-${item.history_id}`" @click="loadHistoryDetail(item.history_id)">加载</button>
                <button class="table-link danger-link" type="button" :disabled="historyLoading" :data-testid="`swing-history-remove-${item.history_id}`" @click="removeHistory(item.history_id)">删除</button>
              </div>
            </li>
          </ul>
          <div v-if="historyPageCount > 1" class="history-pagination" data-testid="swing-history-pagination">
            <span>共 {{ history.length }} 条 · 第 {{ historyPage }} / {{ historyPageCount }} 页</span>
            <div>
              <button class="secondary-button" type="button" :disabled="historyPage <= 1" data-testid="swing-history-prev" @click="historyPage -= 1">上一页</button>
              <button class="secondary-button" type="button" :disabled="historyPage >= historyPageCount" data-testid="swing-history-next" @click="historyPage += 1">下一页</button>
            </div>
          </div>
        </template>
      </section>
    </main>
  </div>
</template>

<style scoped>
.swing-panel {
  margin-bottom: 20px;
  padding: 22px;
}

.swing-history {
  margin-bottom: 0;
}

.swing-fields {
  grid-template-columns: repeat(3, minmax(0, 1fr));
  margin-top: 14px;
}

.swing-label-section {
  margin-top: 4px;
}

.scope-option {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  color: var(--text) !important;
  font-size: 11px;
}

.swing-tag {
  padding: 2px 5px;
  border: 1px solid rgba(34, 197, 94, 0.35);
  border-radius: 3px;
  background: var(--accent-soft);
  color: var(--accent);
  font-size: 9px;
}

.swing-tag.muted {
  border-color: rgba(148, 163, 184, 0.35);
  background: transparent;
  color: var(--subtle);
}

.swing-result {
  margin-top: 18px;
}

.swing-progress {
  margin-top: 12px;
}

.swing-progress-track {
  height: 6px;
  border-radius: 3px;
  background: rgba(148, 163, 184, 0.18);
  overflow: hidden;
}

.swing-progress-fill {
  height: 100%;
  border-radius: 3px;
  background: var(--accent);
  transition: width 0.25s ease-out;
}

.swing-progress-text {
  margin-top: 6px;
  color: var(--subtle);
  font-size: 11px;
}

.swing-plan {
  margin-top: 18px;
  padding: 14px 16px;
  border: 1px solid rgba(34, 197, 94, 0.35);
  background: rgba(34, 197, 94, 0.06);
}

.swing-plan-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
  margin-top: 10px;
}

.swing-plan-card {
  display: grid;
  gap: 6px;
  padding: 10px 11px;
  border: 1px solid var(--border);
  background: rgba(8, 15, 32, 0.6);
}

.swing-plan-title {
  color: var(--subtle);
  font-size: 10px;
}

.swing-plan-rule {
  margin: 0;
  color: var(--accent);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  overflow-wrap: anywhere;
}

.swing-plan-empty {
  margin: 0;
  color: var(--subtle);
  font-size: 11px;
}

.swing-plan-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 10px;
}

.swing-handoff {
  display: inline-flex;
  align-items: center;
  min-height: 34px;
  margin-top: 0;
  text-decoration: none;
}

.swing-dist summary {
  color: var(--accent);
  cursor: pointer;
  font-size: 10px;
}

.swing-dist-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  margin-top: 8px;
}

.swing-dist-title {
  color: var(--subtle);
  font-size: 9px;
}

.swing-bar-row {
  display: grid;
  grid-template-columns: 44px minmax(0, 1fr) 42px;
  align-items: center;
  gap: 6px;
  margin-top: 3px;
  font-size: 9px;
}

.swing-bar-label {
  color: var(--subtle);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}

.swing-bar-track {
  display: block;
  height: 6px;
  background: rgba(148, 163, 184, 0.16);
}

.swing-bar-track i {
  display: block;
  height: 100%;
  background: var(--accent);
}

.swing-bar-value {
  color: var(--muted);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  text-align: right;
}

.swing-note {
  max-width: 420px;
  color: var(--muted);
  font-size: 10px;
  line-height: 1.5;
}
</style>
