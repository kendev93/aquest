<script setup lang="ts">
import PageGuide from '../components/PageGuide.vue'
import { computed, onMounted, ref } from 'vue'

import { fetchMetadata } from '../api/formulaScreen'
import { describeError } from '../api/client'
import {
  checkInstrumentConditions,
  fetchInstrumentConditionLibrary,
  fetchInstrumentValues,
  runInstrumentBacktest,
  type InstrumentLabCheckReport,
  type InstrumentLabLibraryItem,
  type InstrumentLabValues,
} from '../api/instrumentLab'
import { fetchLocalChart, fetchLocalInstruments } from '../api/marketData'
import DateRangeSelector from '../components/DateRangeSelector.vue'
import MarketCandlestickChart from '../components/MarketCandlestickChart.vue'
import RuleGroupEditor from '../components/RuleGroupEditor.vue'
import TermHint from '../components/TermHint.vue'
import type {
  ChartMarker,
  DateRangeSelection,
  LocalInstrument,
  MarketCandle,
  MarketChartPeriod,
  RuleGroupPayload,
  ValueDefinition,
} from '../types'
import { buildChartGeometry, formatChartValue } from '../utils/chart'
import {
  buildTradeMarkers,
  differenceTone,
  formatDifferencePoints,
  formatPValue,
  formatPercent,
  libraryOptionLabel,
  percentileText,
  percentileTone,
  selectedValueLabel,
  seriesPoints,
  significanceLabel,
  significanceTone,
  sideLabel,
  summaryText,
} from '../utils/instrumentLab'
import AppTopbar from '../components/AppTopbar.vue'

const CHART_WIDTH = 820
const CHART_HEIGHT = 220
const MAX_VALUE_IDS = 12

const keyword = ref('')
const instruments = ref<LocalInstrument[]>([])
const selected = ref<LocalInstrument | null>(null)
const dateRange = ref<DateRangeSelection>({ mode: 'full', startDate: '', endDate: '' })
const message = ref('')

const indicatorValues = ref<ValueDefinition[]>([])
const pickedValues = ref<string[]>([])
const values = ref<InstrumentLabValues | null>(null)
const readingValues = ref(false)

const candles = ref<MarketCandle[]>([])
const chartPeriod = ref<MarketChartPeriod>('daily')
const loadingChart = ref(false)

const library = ref<InstrumentLabLibraryItem[]>([])
const pickedConditions = ref<string[]>([])
const checkReport = ref<InstrumentLabCheckReport | null>(null)
const checking = ref(false)
const horizon = ref(5)

const buyRule = ref<RuleGroupPayload>({
  signals: [],
  conditions: [],
  required_conditions: [],
  combine_mode: 'all',
  minimum_matches: null,
})
const sellRule = ref<RuleGroupPayload>({
  signals: [],
  conditions: [],
  required_conditions: [],
  combine_mode: 'all',
  minimum_matches: null,
})
const initialCash = ref(100_000)
const backtestResult = ref<Record<string, unknown> | null>(null)
const runningBacktest = ref(false)

const markers = computed<ChartMarker[]>(() => {
  const trades = backtestResult.value?.trades
  return Array.isArray(trades)
    ? buildTradeMarkers(trades as Record<string, unknown>[])
    : []
})

const timelineGeometry = computed(() => {
  const series = values.value?.series ?? []
  if (!series.length) return null
  return buildChartGeometry(seriesPoints(series), {
    width: CHART_WIDTH,
    height: CHART_HEIGHT,
  })
})

const timelineLegend = computed(() =>
  (values.value?.series ?? []).map((item, index) => ({
    index,
    text: selectedValueLabel(item),
    color: ['#86efac', '#67e8f9', '#c4b5fd', '#fbbf24', '#fb7185', '#f9a8d4'][index % 6],
  })),
)

const backtestPerformance = computed(() => {
  const raw = backtestResult.value?.performance
  return raw && typeof raw === 'object' ? (raw as Record<string, number | null>) : null
})

const backtestTrades = computed(() => {
  const raw = backtestResult.value?.trades
  return Array.isArray(raw) ? (raw as Record<string, unknown>[]) : []
})

function windowDates(): { start_date: string | null; end_date: string | null } {
  if (dateRange.value.mode === 'custom') {
    return { start_date: dateRange.value.startDate || null, end_date: dateRange.value.endDate || null }
  }
  if (dateRange.value.mode === 'recent5') {
    const end = new Date()
    const start = new Date()
    start.setFullYear(start.getFullYear() - 5)
    return { start_date: start.toISOString().slice(0, 10), end_date: end.toISOString().slice(0, 10) }
  }
  return { start_date: null, end_date: null }
}

function currentScope() {
  const item = selected.value
  if (!item) return null
  const { start_date, end_date } = windowDates()
  return { market: item.market, code: item.code, start_date, end_date }
}

function apiMessage(error: unknown): string {
  return describeError(error, { fallback: '读取失败，请检查本地行情数据后重试。' })
}

async function searchInstruments(): Promise<void> {
  try {
    const payload = await fetchLocalInstruments({ keyword: keyword.value || undefined, page_size: 20 })
    instruments.value = payload.items
    if (!payload.items.length) message.value = '没有匹配的标的，先同步本地行情。'
  } catch (error) {
    message.value = apiMessage(error)
  }
}

async function pickInstrument(item: LocalInstrument): Promise<void> {
  selected.value = item
  values.value = null
  checkReport.value = null
  backtestResult.value = null
  message.value = ''
  await loadChart()
}

async function loadChart(): Promise<void> {
  const scope = currentScope()
  if (!scope) return
  loadingChart.value = true
  try {
    const chart = await fetchLocalChart({
      market: scope.market,
      code: scope.code,
      period: chartPeriod.value,
      start_date: scope.start_date ?? undefined,
      end_date: scope.end_date ?? undefined,
    })
    candles.value = chart.candles
  } catch (error) {
    message.value = apiMessage(error)
  } finally {
    loadingChart.value = false
  }
}

function toggleValue(valueId: string): void {
  const index = pickedValues.value.indexOf(valueId)
  if (index >= 0) {
    pickedValues.value.splice(index, 1)
    return
  }
  if (pickedValues.value.length >= MAX_VALUE_IDS) {
    message.value = `一次最多看 ${MAX_VALUE_IDS} 个指标`
    return
  }
  pickedValues.value.push(valueId)
}

async function readValues(): Promise<void> {
  const scope = currentScope()
  if (!scope || !pickedValues.value.length) return
  readingValues.value = true
  try {
    values.value = await fetchInstrumentValues({
      market: scope.market,
      code: scope.code,
      start_date: scope.start_date,
      end_date: scope.end_date,
      value_ids: pickedValues.value,
    })
  } catch (error) {
    values.value = null
    message.value = apiMessage(error)
  } finally {
    readingValues.value = false
  }
}

async function loadLibrary(): Promise<void> {
  try {
    const payload = await fetchInstrumentConditionLibrary()
    library.value = payload.items
  } catch (error) {
    message.value = apiMessage(error)
  }
}

function toggleCondition(key: string): void {
  const index = pickedConditions.value.indexOf(key)
  if (index >= 0) pickedConditions.value.splice(index, 1)
  else pickedConditions.value.push(key)
}

async function runCheck(): Promise<void> {
  const scope = currentScope()
  if (!scope || !pickedConditions.value.length) return
  checking.value = true
  try {
    checkReport.value = await checkInstrumentConditions({
      market: scope.market,
      code: scope.code,
      start_date: scope.start_date,
      end_date: scope.end_date,
      horizon: horizon.value,
      conditions: library.value
        .filter((item) => pickedConditions.value.includes(item.key))
        .map((item) => item.condition),
    })
  } catch (error) {
    checkReport.value = null
    message.value = apiMessage(error)
  } finally {
    checking.value = false
  }
}

async function runBacktest(): Promise<void> {
  const scope = currentScope()
  if (!scope) return
  runningBacktest.value = true
  try {
    const payload: Record<string, unknown> = {
      market: scope.market,
      code: scope.code,
      date_mode: dateRange.value.mode === 'custom' ? 'custom' : 'full',
      initial_cash: initialCash.value,
      buy_rule: buyRule.value,
      sell_rule: sellRule.value,
    }
    if (scope.start_date && scope.end_date) {
      payload.start_date = scope.start_date
      payload.end_date = scope.end_date
    }
    backtestResult.value = await runInstrumentBacktest(payload)
  } catch (error) {
    backtestResult.value = null
    message.value = apiMessage(error)
  } finally {
    runningBacktest.value = false
  }
}

onMounted(async () => {
  try {
    const metadata = await fetchMetadata()
    indicatorValues.value = metadata.indicators.flatMap((item) => item.values ?? [])
  } catch (error) {
    message.value = apiMessage(error)
  }
  await Promise.all([searchInstruments(), loadLibrary()])
  const params = new URLSearchParams(window.location.search)
  const code = params.get('code')
  if (code) {
    const hit = instruments.value.find((item) => item.code === code)
    if (hit) await pickInstrument(hit)
  }
})
</script>

<template>
  <div class="screen-page lab-page" data-testid="instrument-lab-page">
    <AppTopbar />

    <main class="content-shell">
      <section class="page-intro">
        <div>
          <p class="eyebrow">INSTRUMENT / WORKBENCH</p>
          <h1>标的检验台</h1>
          <p class="intro-copy">一只票，三种读数：指标在它自己历史上的位置、一组规则在它身上的回测、已通过置换检验的条件在它身上是否还成立。</p>
        </div>
        <div class="intro-note">
          <span class="note-label">先把话说前头</span>
          <strong>这里不打分</strong>
          <small>综合评分的权重没有可证伪的依据，所以只给可核对的读数：取值、命中、收益差、置换 p 值</small>
        </div>
      </section>

      <PageGuide page-key="instrument-lab" />

      <div v-if="message" class="notice" role="alert" data-testid="lab-message">{{ message }}</div>

      <section class="panel" data-testid="lab-picker">
        <div class="panel-heading">
          <div><span class="section-kicker">01 / PICK</span><h2>选择标的</h2></div>
          <div class="lab-heading-actions">
            <input v-model="keyword" class="lab-search" type="search" placeholder="代码或名称" data-testid="lab-keyword" @keyup.enter="searchInstruments">
            <button class="secondary-button" type="button" data-testid="lab-search" @click="searchInstruments">搜索</button>
          </div>
        </div>
        <p v-if="selected" class="helper">当前标的：<strong class="code">{{ selected.market }}{{ selected.code }} {{ selected.name ?? '' }}</strong> · 日线 {{ selected.bars }} 根</p>
        <p v-else class="helper">先在下面挑一只票。</p>
        <div class="table-wrap">
          <table>
            <caption class="sr-only">本地标的</caption>
            <thead><tr><th>代码</th><th>名称</th><th>日线</th><th>数据区间</th><th>操作</th></tr></thead>
            <tbody>
              <tr v-for="item in instruments" :key="`${item.market}-${item.code}`">
                <td class="code">{{ item.market }}{{ item.code }}</td>
                <td>{{ item.name ?? '—' }}</td>
                <td>{{ item.bars }}</td>
                <td class="muted-code">{{ item.data_start ?? '—' }} → {{ item.data_end ?? '—' }}</td>
                <td><button class="link-button" type="button" :data-testid="`lab-pick-${item.code}`" @click="pickInstrument(item)">选它</button></td>
              </tr>
            </tbody>
          </table>
        </div>
        <DateRangeSelector v-model="dateRange" prefix="lab" testid="lab-date-range" title="检验区间" helper="全历史看得更全，自定义区间只看一段。" />
      </section>

      <section v-if="selected" class="panel" data-testid="lab-chart">
        <div class="panel-heading"><div><span class="section-kicker">02 / BARS</span><h2>行情与买卖点</h2></div></div>
        <MarketCandlestickChart
          :candles="candles"
          :ma-keys="['ma5', 'ma20']"
          :period="chartPeriod"
          :show-macd="false"
          :show-rsi="true"
          :formula-panels="[]"
          :markers="markers"
        />
        <p v-if="loadingChart" class="helper">K 线读取中…</p>
      </section>

      <section v-if="selected" class="panel" data-testid="lab-values">
        <div class="panel-heading">
          <div><span class="section-kicker">03 / READINGS</span><h2>指标读数</h2></div>
          <button class="primary-button" type="button" :disabled="!pickedValues.length || readingValues" data-testid="lab-read-values" @click="readValues">
            <span v-if="readingValues" class="spinner" aria-hidden="true"></span>{{ readingValues ? '读取中…' : `看这 ${pickedValues.length} 个指标` }}
          </button>
        </div>
        <p class="helper">
          已选 {{ pickedValues.length }} / {{ MAX_VALUE_IDS }} 个。
          <TermHint term="selfPercentile" /> 分位是这只票跟它自己的历史比，不是全市场排名。
        </p>
        <div class="lab-value-picker">
          <label v-for="item in indicatorValues" :key="item.id" class="lab-value-option">
            <input type="checkbox" :data-testid="`lab-value-${item.id}`" :checked="pickedValues.includes(item.id)" @change="toggleValue(item.id)">
            {{ item.display_name }}
          </label>
        </div>

        <template v-if="values">
          <figure class="lab-chart" data-testid="lab-values-chart">
            <figcaption><span>指标取值时间轴</span><small>{{ values.start_date }} → {{ values.end_date }} · {{ values.bars }} 个交易日{{ values.thinned ? '（已等距抽样显示）' : '' }}</small></figcaption>
            <svg v-if="timelineGeometry" :viewBox="`0 0 ${CHART_WIDTH} ${CHART_HEIGHT}`" role="img" aria-label="指标取值时间轴">
              <line v-for="tick in timelineGeometry.ticks" :key="`lab-grid-${tick.y}`" x1="46" :x2="CHART_WIDTH - 12" :y1="tick.y" :y2="tick.y" class="lab-grid" />
              <text v-for="tick in timelineGeometry.ticks" :key="`lab-tick-${tick.y}`" x="6" :y="tick.y + 4" class="lab-axis-text">{{ formatChartValue(tick.value, 2) }}</text>
              <polyline
                v-for="item in timelineLegend"
                :key="`lab-line-${item.index}`"
                :points="timelineGeometry.lines[item.index]"
                :stroke="item.color"
                class="lab-line"
                fill="none"
              />
            </svg>
            <div class="lab-legend">
              <span v-for="item in timelineLegend" :key="`lab-legend-${item.index}`" class="lab-legend-item">
                <i :style="{ background: item.color }" aria-hidden="true"></i>{{ item.text }}
              </span>
            </div>
          </figure>
          <div class="table-wrap">
            <table>
              <caption class="sr-only">指标最新读数</caption>
              <thead><tr><th>指标</th><th>最新值</th><th>自身历史分位</th><th>说明</th></tr></thead>
              <tbody>
                <tr v-for="item in values.series" :key="`row-${item.value_id}`">
                  <td>{{ item.label }}</td>
                  <td class="code">{{ item.last_value === null ? '—' : item.last_value.toFixed(3) }}</td>
                  <td :class="`lab-tone-${percentileTone(item.percentile)}`">{{ percentileText(item.percentile) }}</td>
                  <td class="muted-code">{{ item.note ?? '—' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <ul class="lab-notes">
            <li v-for="note in values.notes" :key="note">{{ note }}</li>
          </ul>
        </template>
      </section>

      <section v-if="selected" class="panel" data-testid="lab-backtest">
        <div class="panel-heading">
          <div><span class="section-kicker">04 / RULES</span><h2>把规则放到这只票上</h2></div>
          <button class="primary-button" type="button" :disabled="runningBacktest" data-testid="lab-run-backtest" @click="runBacktest">
            <span v-if="runningBacktest" class="spinner" aria-hidden="true"></span>{{ runningBacktest ? '回测中…' : '跑一遍' }}
          </button>
        </div>
        <div class="lab-cash-field">
          <label for="lab-cash">初始资金</label>
          <input id="lab-cash" v-model.number="initialCash" type="number" min="1000" step="1000" data-testid="lab-cash">
        </div>
        <RuleGroupEditor v-model="buyRule" :values="indicatorValues" title="买入条件" testid="lab-buy-rule" />
        <RuleGroupEditor v-model="sellRule" :values="indicatorValues" title="卖出条件" testid="lab-sell-rule" />
        <template v-if="backtestPerformance">
          <dl class="lab-performance">
            <div><dt>总收益</dt><dd>{{ formatPercent(backtestPerformance.total_return ?? null) }}</dd></div>
            <div><dt>最大回撤</dt><dd>{{ formatPercent(backtestPerformance.max_drawdown ?? null) }}</dd></div>
            <div><dt>成交次数</dt><dd>{{ backtestTrades.length }}</dd></div>
            <div><dt>买卖点</dt><dd>已画在上方 K 线（<span class="lab-buy">▲</span> 买 / <span class="lab-sell">▼</span> 卖）</dd></div>
          </dl>
          <div class="table-wrap">
            <table>
              <caption class="sr-only">成交明细</caption>
              <thead><tr><th>日期</th><th>方向</th><th>价格</th><th>数量</th><th>盈亏</th></tr></thead>
              <tbody>
                <tr v-for="(trade, index) in backtestTrades.slice(0, 50)" :key="`trade-${index}`">
                  <td class="muted-code">{{ String(trade.datetime ?? '').slice(0, 10) }}</td>
                  <td>{{ trade.direction === 'buy' ? '买入' : '卖出' }}</td>
                  <td class="code">{{ Number(trade.price ?? 0).toFixed(2) }}</td>
                  <td class="code">{{ trade.size }}</td>
                  <td class="code">{{ trade.pnl === 0 || trade.pnl ? Number(trade.pnl).toFixed(2) : '—' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </template>
      </section>

      <section v-if="selected" class="panel" data-testid="lab-conditions">
        <div class="panel-heading">
          <div><span class="section-kicker">05 / CONDITIONS</span><h2>已验证条件在这只票上还成立吗</h2></div>
          <button class="primary-button" type="button" :disabled="!pickedConditions.length || checking" data-testid="lab-run-check" @click="runCheck">
            <span v-if="checking" class="spinner" aria-hidden="true"></span>{{ checking ? '检验中…' : `检验这 ${pickedConditions.length} 条` }}
          </button>
        </div>
        <p class="helper">
          条件来自买卖条件研究里<strong>通过了置换检验</strong>的候选。全市场成立不等于这只票成立——这里就是把这件事摊开看。
          <TermHint term="blockPermutation" />
        </p>
        <div class="lab-cash-field">
          <label for="lab-horizon">前瞻天数</label>
          <input id="lab-horizon" v-model.number="horizon" type="number" min="1" max="60" data-testid="lab-horizon">
        </div>
        <div v-if="!library.length" class="inline-empty" data-testid="lab-library-empty">还没有通过置换检验的条件，先去做一次买卖条件研究。</div>
        <div v-else class="table-wrap">
          <table>
            <caption class="sr-only">可选条件</caption>
            <thead><tr><th>勾选</th><th>条件</th><th>侧</th><th>挖掘时的提升</th><th>当时 p 值</th></tr></thead>
            <tbody>
              <tr v-for="item in library" :key="item.key">
                <td><input type="checkbox" :data-testid="`lab-condition-${item.key}`" :checked="pickedConditions.includes(item.key)" @change="toggleCondition(item.key)"></td>
                <td>{{ libraryOptionLabel(item) }}</td>
                <td>{{ sideLabel(item.side) }}</td>
                <td class="code">{{ item.lift === null ? '—' : `${item.lift.toFixed(2)} 倍` }}</td>
                <td class="muted-code">{{ formatPValue(item.pvalue) }}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <template v-if="checkReport">
          <div class="table-wrap">
            <table>
              <caption class="sr-only">条件检验结果</caption>
              <thead><tr><th>条件</th><th>命中 / 样本</th><th>命中后收益</th><th>全样本收益</th><th>差值</th><th>置换 p</th><th>结论</th></tr></thead>
              <tbody>
                <tr v-for="item in checkReport.items" :key="`check-${item.key}`">
                  <td>{{ item.label }}<small v-if="item.note" class="lab-item-note">{{ item.note }}</small></td>
                  <td class="code">{{ item.hit_count }} / {{ item.sample_days }}</td>
                  <td class="code">{{ formatPercent(item.hit_mean_return) }}</td>
                  <td class="code">{{ formatPercent(item.base_mean_return) }}</td>
                  <td :class="`lab-tone-${differenceTone(item.difference)}`">{{ formatDifferencePoints(item.difference) }}</td>
                  <td class="muted-code">{{ formatPValue(item.adjusted_p_value ?? item.p_value) }}</td>
                  <td :class="`lab-tone-${significanceTone(item)}`">{{ significanceLabel(item) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <ul class="lab-notes">
            <li v-for="note in checkReport.notes" :key="note">{{ note }}</li>
          </ul>
          <p class="helper">{{ checkReport.items.map(summaryText).join('；') }}</p>
        </template>
      </section>
    </main>
  </div>
</template>
