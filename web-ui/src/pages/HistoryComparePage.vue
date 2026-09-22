<script setup lang="ts">
import PageGuide from '../components/PageGuide.vue'
import { computed, onMounted, ref } from 'vue'

import {
  compareHistory,
  fetchHistoryCompareCandidates,
  type HistoryCompareCandidate,
  type HistoryCompareItem,
  type HistoryCompareReport,
} from '../api/historyCompare'
import { FormulaScreenApiError } from '../api/formulaScreen'
import TermHint from '../components/TermHint.vue'
import { buildChartGeometry, formatChartValue } from '../utils/chart'
import {
  axisNote,
  buildDateAxis,
  buildSeries,
  colorFor,
  dateRangeText,
  formatLastValue,
  formatMetricValue,
  formatNativeValue,
  isEquityCurve,
  lastValueTone,
  metricCell,
  metricTitle,
  normalizeNote,
  seriesLabel,
} from '../utils/historyCompare'
import { formatChinaTimestamp } from '../utils/time'

const CHART_WIDTH = 820
const CHART_HEIGHT = 240

const candidates = ref<HistoryCompareCandidate[]>([])
const selected = ref<string[]>([])
const report = ref<HistoryCompareReport | null>(null)
const loading = ref(false)
const listing = ref(false)
const message = ref('')
const typeFilter = ref('all')
const normalize = ref<'capital' | 'first'>('capital')
const xAxis = ref<'date' | 'index'>('date')

const maxItems = ref(5)
const types = computed(() => {
  const seen = new Map<string, string>()
  for (const item of candidates.value) {
    seen.set(item.backtest_type, item.type_label)
  }
  return [...seen.entries()].map(([backtest_type, type_label]) => ({ backtest_type, type_label }))
})
const visibleCandidates = computed(() =>
  typeFilter.value === 'all'
    ? candidates.value
    : candidates.value.filter((item) => item.backtest_type === typeFilter.value),
)
const canCompare = computed(
  () => selected.value.length >= 2 && selected.value.length <= maxItems.value && !loading.value,
)
const selectedItems = computed(() => report.value?.items ?? [])

const isEquity = computed(() => isEquityCurve(report.value?.curve_group.kind))

const plottedItems = computed(() => {
  const group = report.value?.curve_group
  if (!group?.history_ids.length) return []
  return selectedItems.value.filter((item) => group.history_ids.includes(item.history_id))
})

const dateAxis = computed(() => buildDateAxis(selectedItems.value))

const geometry = computed(() =>
  buildChartGeometry(
    buildSeries(plottedItems.value, {
      xAxis: xAxis.value,
      isEquity: isEquity.value,
      axis: dateAxis.value,
    }),
    { width: CHART_WIDTH, height: CHART_HEIGHT, includeZero: isEquity.value },
  ),
)

const metricRows = computed(() => selectedItems.value[0]?.metrics ?? [])

function toggle(historyId: string): void {
  const index = selected.value.indexOf(historyId)
  if (index >= 0) {
    selected.value.splice(index, 1)
    return
  }
  if (selected.value.length >= maxItems.value) {
    message.value = `一次最多对比 ${maxItems.value} 条，多了就分不清了`
    return
  }
  selected.value.push(historyId)
}

function clearSelection(): void {
  selected.value = []
  report.value = null
}

async function loadCandidates(): Promise<void> {
  listing.value = true
  try {
    const payload = await fetchHistoryCompareCandidates()
    candidates.value = payload.items
    maxItems.value = payload.max_items
    const known = new Set(payload.items.map((item) => item.history_id))
    selected.value = selected.value.filter((id) => known.has(id))
  } catch (error) {
    message.value = apiMessage(error)
  } finally {
    listing.value = false
  }
}

async function run(): Promise<void> {
  if (!canCompare.value) return
  loading.value = true
  message.value = ''
  try {
    report.value = await compareHistory({
      history_ids: selected.value,
      normalize: normalize.value,
      x_axis: xAxis.value,
    })
  } catch (error) {
    report.value = null
    message.value = apiMessage(error)
  } finally {
    loading.value = false
  }
}

function apiMessage(error: unknown): string {
  return error instanceof FormulaScreenApiError
    ? error.message
    : '读取历史记录失败，请检查本地 DuckDB 数据后重试。'
}

onMounted(async () => {
  const params = new URLSearchParams(window.location.search)
  const ids = (params.get('ids') ?? '')
    .split(',')
    .map((value) => value.trim())
    .filter(Boolean)
  if (ids.length) selected.value = ids.slice(0, maxItems.value)
  await loadCandidates()
  if (selected.value.length >= 2) await run()
})
</script>

<template>
  <div class="screen-page compare-page" data-testid="history-compare-page">
    <header class="topbar">
      <a class="brand" href="/indicator-lab" aria-label="指标实验室首页"><img class="brand-mark" src="/indicator-lab-mark.png" alt="" aria-hidden="true" width="34" height="34"><span><strong>Indicator Lab</strong><small>指标实验室</small></span></a>
      <nav aria-label="主导航">
        <a class="nav-link" href="/indicator-lab">公式选股</a>
        <a class="nav-link" href="/portfolio-backtest">组合回测</a>
        <a class="nav-link" href="/strategy-lab">策略实验室</a>
        <a class="nav-link" href="/condition-research">买卖条件研究</a>
        <a class="nav-link" href="/strategy-fitness">策略适配性</a>
        <a class="nav-link" href="/market-data">本地行情</a>
        <a class="nav-link" href="/swing-research">波段研究</a>
        <a class="nav-link" href="/strategy-tracking">策略追踪</a>
        <a class="nav-link" href="/factor-test">因子检验</a>
        <a class="nav-link active" href="/history-compare" aria-current="page">历史对比</a>
        <a class="nav-link" href="/instrument-lab">标的检验台</a>
        <a class="nav-link" href="/candidate-library">候选库</a>
        <a class="nav-link" href="/factor-attribution">因子归因</a>
      </nav>
      <div class="topbar-status"><span class="status-dot" aria-hidden="true"></span> 本地数据模式</div>
    </header>

    <main class="content-shell">
      <section class="page-intro">
        <div>
          <p class="eyebrow">HISTORY / COMPARE</p>
          <h1>回测历史对比</h1>
          <p class="intro-copy">把做过的实验摆在一起看：曲线叠绘 + 指标并排。只读历史，不重跑任何回测。</p>
        </div>
        <div class="intro-note">
          <span class="note-label">先把话说前头</span>
          <strong>不同口径不排在一列里比大小</strong>
          <small>跨类型时收益类数字只能定性参照；界面会写明每个数字来自哪一口径</small>
        </div>
      </section>

      <PageGuide page-key="history-compare" />

      <div v-if="message" class="notice" role="alert" data-testid="compare-message">{{ message }}</div>

      <section class="panel compare-config" data-testid="compare-config">
        <div class="panel-heading">
          <div><span class="section-kicker">01 / SELECT</span><h2>选择记录</h2></div>
          <div class="compare-heading-actions">
            <button class="secondary-button" type="button" :disabled="listing" data-testid="compare-refresh" @click="loadCandidates">{{ listing ? '读取中…' : '刷新' }}</button>
            <button class="secondary-button" type="button" data-testid="compare-clear" @click="clearSelection">清空</button>
            <button class="primary-button" type="button" :disabled="!canCompare" data-testid="compare-run" @click="run">
              <span v-if="loading" class="spinner" aria-hidden="true"></span>{{ loading ? '对比中…' : `对比这 ${selected.length} 条` }}
            </button>
          </div>
        </div>

        <div class="compare-options">
          <div>
            <label for="compare-normalize">归一化方式<TermHint term="historyNormalize" /></label>
            <select id="compare-normalize" v-model="normalize" data-testid="compare-normalize">
              <option value="capital">除以本金（收益率）</option>
              <option value="first">除以首值（起点为 0%）</option>
            </select>
          </div>
          <div>
            <label for="compare-x-axis">横轴</label>
            <select id="compare-x-axis" v-model="xAxis" data-testid="compare-x-axis">
              <option value="date">按日期</option>
              <option value="index">按第 N 个交易日</option>
            </select>
          </div>
          <div>
            <label for="compare-type">只看某一类</label>
            <select id="compare-type" v-model="typeFilter" data-testid="compare-type">
              <option value="all">全部类型</option>
              <option v-for="option in types" :key="option.backtest_type" :value="option.backtest_type">{{ option.type_label }}</option>
            </select>
          </div>
        </div>

        <p class="helper">已选 {{ selected.length }} / {{ maxItems }} 条。同类型的多次实验才是可比的；跨类型只能比规模与显著性。</p>

        <div v-if="!visibleCandidates.length" class="inline-empty" data-testid="compare-empty-candidates">还没有任何历史记录，先去做一次回测或研究。</div>
        <div v-else class="table-wrap">
          <table>
            <caption class="sr-only">可选的历史记录</caption>
            <thead><tr><th>勾选</th><th>记录</th><th>类型</th><th>数据区间</th><th>曲线</th><th>完成时间</th></tr></thead>
            <tbody>
              <tr v-for="item in visibleCandidates" :key="item.history_id">
                <td><input type="checkbox" :data-testid="`compare-pick-${item.history_id}`" :checked="selected.includes(item.history_id)" @change="toggle(item.history_id)"></td>
                <td><strong class="code">{{ item.label }}</strong><small class="history-id">{{ item.subtitle }}</small></td>
                <td>{{ item.type_label }}</td>
                <td class="muted-code">{{ item.date_range.start && item.date_range.end ? `${item.date_range.start} → ${item.date_range.end}` : '区间未记录' }}</td>
                <td class="muted-code">{{ item.curve_kind_label || '无曲线' }}</td>
                <td class="muted-code">{{ formatChinaTimestamp(item.finished_at) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <section v-if="report" class="results-panel panel" data-testid="compare-results" aria-live="polite">
        <div class="panel-heading results-heading">
          <div><span class="section-kicker">02 / COMPARE</span><h2>对比结果</h2></div>
        </div>

        <ul v-if="report.warnings.length" class="compare-warnings" data-testid="compare-warnings">
          <li v-for="warning in report.warnings" :key="warning">{{ warning }}</li>
        </ul>

        <figure class="compare-chart" data-testid="compare-chart" :class="{ 'compare-chart-empty': !geometry }">
          <figcaption>
            <span>{{ report.curve_group.kind_label || '没有可绘制的曲线' }} · {{ report.curve_group.unit || '—' }}</span>
            <small>{{ report.curve_group.note }}</small>
          </figcaption>
          <svg v-if="geometry" :viewBox="`0 0 ${CHART_WIDTH} ${CHART_HEIGHT}`" role="img" :aria-label="`${report.curve_group.kind_label}叠绘`">
            <line v-for="tick in geometry.ticks" :key="`grid-${tick.y}`" :x1="46" :x2="CHART_WIDTH - 12" :y1="tick.y" :y2="tick.y" class="compare-grid" />
            <line v-if="geometry.zeroY !== null" :x1="46" :x2="CHART_WIDTH - 12" :y1="geometry.zeroY" :y2="geometry.zeroY" class="compare-zero" />
            <text v-for="tick in geometry.ticks" :key="`label-${tick.y}`" :x="6" :y="tick.y + 4" class="compare-axis-text">{{ formatChartValue(tick.value, isEquity ? 1 : 3) }}</text>
            <polyline
              v-for="item in plottedItems"
              :key="item.history_id"
              :points="geometry.lines[plottedItems.indexOf(item)]"
              :stroke="colorFor(plottedItems, item.history_id)"
              class="compare-line"
              fill="none"
            />
          </svg>
          <p v-else class="inline-empty">没有可绘制的曲线{{ report.curve_group.note ? `：${report.curve_group.note}` : '' }}</p>
          <div class="compare-legend">
            <span v-for="item in plottedItems" :key="`legend-${item.history_id}`" class="compare-legend-item">
              <i :style="{ background: colorFor(plottedItems, item.history_id) }" aria-hidden="true"></i>
              {{ seriesLabel(item) }}
              <strong :class="lastValueTone(item, isEquity)">{{ formatLastValue(item, isEquity) }}</strong>
            </span>
            <small class="compare-axis-note">{{ axisNote(xAxis) }} · {{ normalizeNote(normalize, report?.curve_group.kind ?? null) }}</small>
          </div>
        </figure>

        <div class="table-wrap">
          <table>
            <caption class="sr-only">指标并排</caption>
            <thead>
              <tr>
                <th>指标</th>
                <th v-for="item in selectedItems" :key="`head-${item.history_id}`">{{ item.type_label }}<small class="history-id">{{ item.subtitle }} · {{ item.short_id }}</small></th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in metricRows" :key="row.key">
                <th scope="row">{{ row.label }}</th>
                <td v-for="item in selectedItems" :key="`${row.key}-${item.history_id}`">
                  <span :title="metricTitle(item, row.key)" :class="{ 'compare-missing': metricCell(item, row.key)?.value === null }">{{ formatMetricValue(metricCell(item, row.key)) }}</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <p class="helper">每一格鼠标悬停可以看到这个数字来自哪一口径；显示「—」的格子表示这一口径该类型不产出，不是 0。</p>

        <div class="compare-native-grid">
          <article v-for="item in selectedItems" :key="`native-${item.history_id}`" class="compare-native-card">
            <h3>{{ item.type_label }}专属指标</h3>
            <p class="muted-code">{{ item.label }}</p>
            <dl>
              <div v-for="metric in item.native_metrics" :key="`${item.history_id}-${metric.key}`">
                <dt :title="metric.note ?? metric.source">{{ metric.label }}</dt>
                <dd>{{ formatNativeValue(metric) }}</dd>
              </div>
            </dl>
            <p class="compare-native-note">{{ dateRangeText(item) }}</p>
            <p v-if="item.curve_unavailable_reason" class="compare-native-note">曲线：{{ item.curve_unavailable_reason }}</p>
          </article>
        </div>
      </section>
    </main>
  </div>
</template>
