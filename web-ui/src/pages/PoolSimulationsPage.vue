<script setup lang="ts">
import { computed, onActivated, onDeactivated, ref, watch } from 'vue'
import AppTopbar from '../components/AppTopbar.vue'
import IndividualResearchBacktestDetail from '../components/IndividualResearchBacktestDetail.vue'
import PageGuide from '../components/PageGuide.vue'
import { createRequestClient, ApiError } from '../api/client'
import { formatNumber, formatPercent, formatSignedPercent } from '../utils/format'
import type { IndividualResearchFullBacktest } from '../types'

const endpoint = '/api/v1/strategy-pool/simulations'
const request = createRequestClient({ errorClass: ApiError, messages: { fallback: '模拟盘请求失败' } })
type Objective = 'annual_return' | 'sharpe'
type Trade = { date: string; signal_date: string; symbol: string; direction: string; price: number; size: number; reason: string; rank?: number; pnl?: number }
type Position = { market: string; code: string; name: string | null; size: number; entry_price: number; mark: number; entry_date: string }
type PeriodRow = {
  key: string; label: string; start?: string | null; days?: number
  strategy: number | null; benchmark: number | null; holdings: number | null
  excess_benchmark: number | null; excess_holdings: number | null
}
type SimulationMetrics = {
  trading: {
    closed: number; win_rate: number | null; payoff_ratio: number | null
    profit_factor: number | null; avg_win: number | null; avg_loss: number | null
    best_trade: number | null; worst_trade: number | null; expectancy: number | null
    avg_trade_return: number | null; best_trade_return: number | null; worst_trade_return: number | null
    max_win_streak: number; max_loss_streak: number; avg_holding_days: number | null
    realized_pnl: number; unrealized_pnl: number; avg_rank: number | null
  }
  risk: {
    max_drawdown: number; max_drawdown_days: number; avg_drawdown: number | null
    current_drawdown: number; underwater_ratio: number | null; sample_days: number
    annual_return: number | null; volatility: number | null; downside_volatility: number | null
    sharpe: number | null; sortino: number | null; calmar: number | null; var95: number | null
    cvar95: number | null; recovery_factor: number | null; ulcer: number | null
    martin: number | null; omega: number | null; gain_to_pain: number | null
    skew: number | null; kurtosis: number | null; tail_ratio: number | null
    max_daily_gain: number | null; max_daily_loss: number | null
    daily_win_rate: number | null; avg_daily_return: number | null
    max_up_streak: number; max_down_streak: number
  }
  relative: {
    days: number; beta: number | null; correlation: number | null
    tracking_error: number | null; information_ratio: number | null
    up_capture: number | null; down_capture: number | null; excess_max_drawdown: number | null
    alpha: number | null; treynor: number | null
  }
  concentration: {
    max_weight: number | null; top3_weight: number | null
    hhi: number | null; cash_ratio: number | null
  }
  ranges: PeriodRow[]
  stages: PeriodRow[]
}
type Simulation = {
  id: string; name: string; objective: Objective; ranking: string; min_days: number; direction: string
  start_date: string; initial_cash: number; paused: boolean; warning?: string
  cash: number; value: number; total_return: number; max_drawdown: number
  turnover: number; holding_count: number; max_positions?: number
  equity: { date: string; value: number }[]
  positions?: Position[]; trades?: Trade[]; trades_total?: number; all_trades?: Trade[]; rebalance_band_pct?: number
  benchmark?: { label?: string; dates: string[]; totals: number[] } | null
  holdings_benchmark?: { label?: string; dates: string[]; totals: number[] } | null
  day_return?: number | null; benchmark_return?: number | null; holdings_return?: number | null
  excess_benchmark?: number | null; excess_holdings?: number | null
  metrics?: SimulationMetrics
}

const rankLabels = {
  default: '默认顺序（关注置顶）', win_rate: '全区间胜率', payoff_ratio: '全区间盈亏比',
  annual_return: '全区间年化收益', sharpe: '全区间夏普', composite: '全区间综合',
  postLaunchReturn: '上线后收益', benchmark: '同期买入持有', excess: '超额',
  holdingReturn: '当前持仓收益', createdReturn: '创建至今收益',
} as const
const signalRanks = ['default', 'win_rate', 'payoff_ratio', 'annual_return', 'sharpe', 'composite'] as const
type Rank = keyof typeof rankLabels
function signalRank(value: string | null): Rank {
  return signalRanks.find(rank => rank === value) ?? 'default'
}
const labels = { annual_return: '年化收益', sharpe: '夏普' }
const query = new URLSearchParams(window.location.search)
const objective = ref<Objective>(query.get('objective') === 'sharpe' ? 'sharpe' : 'annual_return')
const initialRank = query.get('ranking')
const ranking = ref<Rank>(signalRank(initialRank))
const name = ref('')
const cash = ref(1_000_000)
const maxPositions = ref(5)
const rebalanceBand = ref(5)
const loadingDays = ref(false)
const days = ref<string[]>([])
const requestedDate = ref(query.get('start_date') ?? '')
const startDate = ref(requestedDate.value)
const creating = ref(query.get('create') === '1')
const busy = ref(false)
const error = ref('')
const items = ref<Simulation[]>([])
const detail = ref<Simulation | null>(null)
const offset = ref(0)
const pageSize = 50
const colors = ['#34d399', '#60a5fa', '#fbbf24', '#f472b6', '#a78bfa', '#fb923c']
let interval: number | undefined
let previousSearch = window.location.search
let daysRequest = 0
let detailRequest = 0

function showError(exc: unknown): void {
  error.value = exc instanceof Error ? exc.message : '模拟盘请求失败'
}

async function loadDays(): Promise<void> {
  const token = ++daysRequest
  loadingDays.value = true
  try {
    const response = await request<{ data: string[] }>(`${endpoint}/signal-days?objective=${objective.value}&ranking=${ranking.value}`)
    if (token !== daysRequest) return
    days.value = response.data
    if (!days.value.includes(startDate.value)) {
      startDate.value = requestedDate.value ? '' : days.value[0] ?? ''
    }
  } catch (exc) { if (token === daysRequest) showError(exc) }
  finally { if (token === daysRequest) loadingDays.value = false }
}

async function refresh(): Promise<void> {
  try { items.value = (await request<{ data: Simulation[] }>(endpoint)).data }
  catch (exc) { showError(exc) }
}

async function select(id: string, pageOffset = 0): Promise<void> {
  const token = ++detailRequest
  try {
    const response = await request<{ data: Simulation }>(`${endpoint}/${id}?offset=${pageOffset}&limit=${pageSize}`)
    if (token !== detailRequest) return
    detail.value = response.data
    offset.value = pageOffset
  } catch (exc) { if (token === detailRequest) showError(exc) }
}

async function create(): Promise<void> {
  busy.value = true
  error.value = ''
  try {
    const response = await request<{ data: { id: string } }>(endpoint, {
      method: 'POST', body: JSON.stringify({
        name: name.value, objective: objective.value, ranking: ranking.value,
        start_date: startDate.value, initial_cash: cash.value, max_positions: maxPositions.value,
        rebalance_band_pct: rebalanceBand.value,
      }),
    })
    creating.value = false
    name.value = ''
    await refresh()
    await select(response.data.id)
  } catch (exc) { showError(exc) }
  finally { busy.value = false }
}

async function toggle(item: Simulation): Promise<void> {
  busy.value = true
  error.value = ''
  try {
    await request(`${endpoint}/${item.id}`, { method: 'PATCH', body: JSON.stringify({ paused: !item.paused }) })
    await refresh()
    if (detail.value?.id === item.id) await select(item.id, offset.value)
  } catch (exc) { showError(exc) }
  finally { busy.value = false }
}

async function rename(item: Simulation): Promise<void> {
  const next = window.prompt('输入新的模拟盘名称', item.name)?.trim()
  if (!next || next === item.name) return
  busy.value = true
  error.value = ''
  try {
    await request(`${endpoint}/${item.id}`, { method: 'PATCH', body: JSON.stringify({ name: next }) })
    if (detail.value?.id === item.id) detail.value = { ...detail.value, name: next }
    await refresh()
  } catch (exc) { showError(exc) }
  finally { busy.value = false }
}

async function remove(item: Simulation): Promise<void> {
  if (!window.confirm(`删除模拟盘「${item.name}」？该盘的持仓、成交和净值记录将一并删除，且无法恢复。`)) return
  busy.value = true
  error.value = ''
  try {
    await request(`${endpoint}/${item.id}`, { method: 'DELETE' })
    if (detail.value?.id === item.id) detail.value = null
    await refresh()
  } catch (exc) { showError(exc) }
  finally { busy.value = false }
}

watch([objective, ranking], () => { void loadDays() })
onActivated(() => {
  const search = window.location.search
  if (search !== previousSearch) {
    previousSearch = search
    const params = new URLSearchParams(search)
    if (params.has('ranking') || params.get('create') === '1') {
      creating.value = params.get('create') === '1'
      objective.value = params.get('objective') === 'sharpe' ? 'sharpe' : 'annual_return'
      const rank = params.get('ranking')
      ranking.value = signalRank(rank)
      requestedDate.value = params.get('start_date') ?? ''
      startDate.value = requestedDate.value
    }
  }
  void refresh()
  void loadDays()
  interval = window.setInterval(() => {
    void refresh()
    if (detail.value) void select(detail.value.id, offset.value)
  }, 30_000)
})
onDeactivated(() => window.clearInterval(interval))

const chart = computed(() => {
  const values = items.value.flatMap(item => item.equity.map(point => ({
    date: Date.parse(point.date), value: point.value / item.initial_cash - 1,
  })))
  if (!values.length) return null
  const start = Math.min(...values.map(p => p.date)), end = Math.max(...values.map(p => p.date))
  const low = Math.min(0, ...values.map(p => p.value)), high = Math.max(0, ...values.map(p => p.value))
  const span = Math.max(0.001, high - low)
  // 只有一天净值时把横轴前后各铺两天：日期照常按整段写，点位仍在中间。
  const pad = start === end ? 2 * 86_400_000 : 0
  const axisStart = start - pad, axisEnd = end + pad
  const x = (time: number) => 62 + (time - axisStart) / Math.max(1, axisEnd - axisStart) * 712
  const xTicks = start === end
    ? Array.from({ length: 5 }, (_, index) => {
      const time = axisStart + (axisEnd - axisStart) * index / 4
      return {
        x: x(time),
        label: new Date(time).toISOString().slice(0, 10),
        anchor: index === 0 ? 'start' : index === 4 ? 'end' : 'middle',
      }
    })
    : [
      { x: x(start), label: new Date(start).toISOString().slice(0, 10), anchor: 'start' },
      { x: x(end), label: new Date(end).toISOString().slice(0, 10), anchor: 'end' },
    ]
  return {
    low, high, xTicks,
    lines: items.value.map((item, index) => {
      // 只有一天净值时 polyline 画不出线：把点放中间并单独画圆点。
      const coords = item.equity.map(p => ({
        x: x(Date.parse(p.date)),
        y: 198 - (p.value / item.initial_cash - 1 - low) / span * 176,
      }))
      return {
        id: item.id, name: item.name, color: colors[index % colors.length],
        points: coords.map(c => `${c.x.toFixed(1)},${c.y.toFixed(1)}`).join(' '),
        dots: coords.length === 1 ? coords : [],
      }
    }).filter(line => line.points),
  }
})

type CurveRange = '1m' | '3m' | '6m' | '1y' | 'ytd' | 'all'
const curveRanges: { key: CurveRange; label: string }[] = [
  { key: '1m', label: '近 1 月' },
  { key: '3m', label: '近 3 月' },
  { key: '6m', label: '近半年' },
  { key: '1y', label: '近 1 年' },
  { key: 'ytd', label: '今年以来' },
  { key: 'all', label: '成立以来' },
]
const curveRange = ref<CurveRange>('all')

function curveStart(end: string): string {
  if (curveRange.value === 'all') return ''
  const [year, month, day] = end.split('-').map(Number)
  if (curveRange.value === 'ytd') return `${year}-01-01`
  const months = { '1m': 1, '3m': 3, '6m': 6, '1y': 12 }[curveRange.value]
  const start = new Date(Date.UTC(year, month - 1 - months, 1))
  const monthEnd = new Date(Date.UTC(start.getUTCFullYear(), start.getUTCMonth() + 1, 0))
  start.setUTCDate(Math.min(day, monthEnd.getUTCDate()))
  return start.toISOString().slice(0, 10)
}

const detailCurve = computed<IndividualResearchFullBacktest | null>(() => {
  const item = detail.value
  if (!item || !item.equity.length) return null
  const end = item.equity[item.equity.length - 1].date
  const start = curveStart(end)
  const points = item.equity.filter(point => point.date >= start)
  if (!points.length) return null
  // 子区间以窗口首日净值为基准，成立以来以初始资金为基准，起点都从 0 开始。
  const baseline = start ? points[0].value : item.initial_cash
  const trades = (item.all_trades ?? []).filter(trade => trade.date >= start)
  const events = new Map<string, Trade[]>()
  for (const trade of trades) events.set(trade.date, [...(events.get(trade.date) ?? []), trade])
  return {
    start_date: points[0].date,
    end_date: end,
    bars: points.length,
    closed_trades: trades.length,
    performance: {},
    benchmark: item.benchmark
      ? {
          label: item.benchmark.label ?? '对比基准',
          instrument_count: 1,
          total_return: null,
          annual_return: null,
          max_drawdown: null,
          dates: item.benchmark.dates,
          totals: item.benchmark.totals,
        }
      : null,
    equity_curve: points.map(point => ({
      date: point.date,
      return_pct: point.value / baseline - 1,
      trade_directions: (events.get(point.date) ?? []).map(trade => trade.direction),
      trade_events: events.get(point.date) ?? [],
    })),
    trades: [],
  }
})
</script>

<template>
  <div class="screen-page simulation-page">
    <AppTopbar />
    <main class="content-shell simulation-shell">
      <a class="simulation-back" href="/strategy-pool">← 返回全市场策略池</a>
      <PageGuide page-key="strategy-pool" />
      <header class="simulation-header">
        <div>
          <p class="eyebrow">STRATEGY POOL / SIMULATIONS</p>
          <h1>榜单模拟盘</h1>
          <p class="simulation-copy">按买入信号榜建仓，默认持有上限为 5 只。次日开盘先卖出，再补空出的仓位；单只权重偏离目标（1/持仓上限）超过设定阈值时，同日开盘再平衡。按开盘价成交，不计佣金、印花税和滑点。</p>
        </div>
        <button v-if="!creating" class="button-primary" type="button" @click="creating = true">创建模拟盘</button>
      </header>
      <p v-if="error" class="notice error-notice" role="alert">{{ error }}</p>
      <section v-if="creating" class="panel-card">
        <div class="simulation-section-heading">
          <h2>创建模拟盘</h2>
          <button class="text-button" type="button" @click="creating = false">收起创建表单</button>
        </div>
        <form class="simulation-form" @submit.prevent="create">
          <label>名称<input v-model.trim="name" type="text" placeholder="例如：全区间综合榜" required maxlength="80" /></label>
          <label>策略目标<select v-model="objective"><option value="annual_return">年化收益</option><option value="sharpe">夏普</option></select></label>
          <label>榜单<select v-model="ranking"><option v-for="key in signalRanks" :key="key" :value="key">{{ rankLabels[key] }}</option></select></label>
          <label>首批信号日<select v-model="startDate" required :disabled="loadingDays || !days.length"><option value="" disabled>{{ loadingDays ? '正在读取…' : days.length ? '请选择信号日' : '暂无可用日期' }}</option><option v-for="day in days" :key="day" :value="day">{{ day }}</option></select></label>
          <label>持仓上限（只）<input v-model.number="maxPositions" data-testid="simulation-max-positions" type="number" min="1" max="100" step="1" required /></label>
          <label>再平衡阈值（百分点）<input v-model.number="rebalanceBand" data-testid="simulation-rebalance-band" type="number" min="0" max="100" step="1" placeholder="0 表示关闭" required /></label>
          <label>初始资金<input v-model.number="cash" type="number" min="1000" max="100000000" required /></label>
          <div class="simulation-form-footer">
            <p v-if="!loadingDays && !days.length" class="simulation-copy">
              暂无可用日期。旧版信号缺少排序归档，请到 <a class="simulation-back" href="/strategy-pool">全市场策略池</a> 完成一轮新版信号扫描。
            </p>
            <p v-else-if="!loadingDays && requestedDate && !days.includes(requestedDate)" class="simulation-copy">原选日期没有此排序的归档；指标排序需该日完成新版信号扫描，旧信号日只能按默认顺序回放。</p>
            <button class="button-secondary" type="button" :disabled="loadingDays" @click="loadDays">刷新日期</button>
            <button class="button-primary" type="submit" :disabled="busy || loadingDays || !startDate">{{ busy ? '正在创建…' : '创建并回放' }}</button>
          </div>
        </form>
      </section>

      <section class="panel-card">
        <div class="simulation-section-heading">
          <h2>净值对比</h2>
          <small>{{ items.length }} 个模拟盘</small>
        </div>
        <template v-if="chart">
          <div class="simulation-legend"><span v-for="line in chart.lines" :key="line.id" :style="{ color: line.color }">{{ line.name }}</span></div>
          <svg class="simulation-chart" viewBox="0 0 800 225" role="img" aria-label="模拟盘累计收益对比">
            <path d="M62 22V198H774" fill="none" stroke="currentColor" />
            <text x="54" y="24" text-anchor="end">{{ formatPercent(chart.high) }}</text>
            <text x="54" y="199" text-anchor="end">{{ formatPercent(chart.low) }}</text>
            <text v-for="tick in chart.xTicks" :key="tick.x" :x="tick.x" y="218" :text-anchor="tick.anchor">{{ tick.label }}</text>
            <g v-for="line in chart.lines" :key="line.id">
              <polyline :points="line.points" :stroke="line.color" fill="none" stroke-width="2.5" />
              <circle v-for="(dot, index) in line.dots" :key="index" :cx="dot.x" :cy="dot.y" r="3.5" :fill="line.color" />
            </g>
          </svg>
        </template>
        <p v-else class="simulation-empty">{{ items.length ? '等待下一交易日行情与信号齐备后生成净值。' : '还没有模拟盘，创建后可在这里对比收益。' }}</p>
        <div v-if="items.length" class="table-wrap">
          <table>
            <thead><tr><th>模拟盘</th><th>榜单</th><th>持仓</th><th>累计收益</th><th>日收益</th><th>沪深300同期</th><th>持仓等权同期</th><th>最大回撤</th><th>换手倍数</th><th>状态</th></tr></thead>
            <tbody>
              <tr v-for="item in items" :key="item.id">
                <td><button class="text-button simulation-name" type="button" :aria-pressed="detail?.id === item.id" @click="select(item.id)">{{ item.name }}</button><button class="text-button simulation-rename" type="button" :disabled="busy" @click="rename(item)">改名</button></td>
                <td>{{ labels[item.objective] }} · {{ rankLabels[item.ranking as Rank] }}</td>
                <td>{{ item.holding_count }}/{{ item.max_positions ?? 5 }}</td>
                <td>{{ formatSignedPercent(item.total_return) }}<small class="simulation-excess">较300 {{ formatSignedPercent(item.excess_benchmark ?? null) }} · 较持仓 {{ formatSignedPercent(item.excess_holdings ?? null) }}</small></td>
                <td>{{ formatSignedPercent(item.day_return ?? null) }}</td>
                <td>{{ formatSignedPercent(item.benchmark_return ?? null) }}</td>
                <td>{{ formatSignedPercent(item.holdings_return ?? null) }}</td>
                <td>{{ formatPercent(item.max_drawdown) }}</td><td>{{ formatNumber(item.turnover) }} 倍</td>
                <td><button class="button-secondary" type="button" :disabled="busy" @click="toggle(item)">{{ item.paused ? '恢复' : '暂停' }}</button><button class="button-secondary simulation-delete" type="button" :disabled="busy" @click="remove(item)">删除</button><small v-if="item.warning">{{ item.warning }}</small></td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <section v-if="detail" class="panel-card">
        <h2>{{ detail.name }} · 持仓与成交</h2>
        <dl class="simulation-metrics">
          <div><dt>总资产</dt><dd>{{ formatNumber(detail.value) }}</dd></div>
          <div><dt>现金</dt><dd>{{ formatNumber(detail.cash) }}</dd></div>
          <div><dt>持仓</dt><dd>{{ detail.holding_count }} / {{ detail.max_positions ?? 5 }}</dd></div>
        </dl>
        <div class="simulation-curve-heading">
          <h3>收益曲线</h3>
          <div v-if="detail.equity.length" class="segmented-tabs" role="group" aria-label="收益曲线区间">
            <button v-for="option in curveRanges" :key="option.key" :aria-pressed="curveRange === option.key" @click="curveRange = option.key">{{ option.label }}</button>
          </div>
        </div>
        <IndividualResearchBacktestDetail v-if="detailCurve" :backtest="detailCurve" :benchmark-label="detail.benchmark?.label" :secondary-benchmark="detail.holdings_benchmark" hide-metrics hide-trades return-curve />
        <p v-else class="simulation-empty">等待下一交易日行情与信号齐备后生成净值。</p>
        <template v-if="detail.metrics">
          <h3>区间表现</h3>
          <div class="table-wrap">
            <table>
              <thead><tr><th>区间</th><th>策略</th><th>沪深300</th><th>持仓等权</th><th>较300</th><th>较持仓</th></tr></thead>
              <tbody>
                <tr v-for="row in detail.metrics.ranges" :key="row.key">
                  <td>{{ row.label }}</td>
                  <td>{{ formatSignedPercent(row.strategy) }}</td>
                  <td>{{ formatSignedPercent(row.benchmark) }}</td>
                  <td>{{ formatSignedPercent(row.holdings) }}</td>
                  <td>{{ formatSignedPercent(row.excess_benchmark) }}</td>
                  <td>{{ formatSignedPercent(row.excess_holdings) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <h3>阶段收益</h3>
          <div class="table-wrap">
            <table>
              <thead><tr><th>区间</th><th>策略</th><th>沪深300</th><th>持仓等权</th></tr></thead>
              <tbody>
                <tr v-for="row in detail.metrics.stages" :key="row.key">
                  <td>{{ row.label }}（{{ row.days }} 个净值日）</td>
                  <td>{{ formatSignedPercent(row.strategy) }}</td>
                  <td>{{ formatSignedPercent(row.benchmark) }}</td>
                  <td>{{ formatSignedPercent(row.holdings) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <h3>收益与回撤</h3>
          <dl class="simulation-metrics">
            <div><dt>年化收益</dt><dd>{{ formatPercent(detail.metrics.risk.annual_return) }}</dd></div>
            <div><dt>最大回撤</dt><dd>{{ formatPercent(detail.metrics.risk.max_drawdown) }}</dd></div>
            <div><dt>最长回撤持续</dt><dd>{{ detail.metrics.risk.max_drawdown_days }} 天</dd></div>
            <div><dt>当前回撤</dt><dd>{{ formatPercent(detail.metrics.risk.current_drawdown) }}</dd></div>
            <div><dt>平均回撤</dt><dd>{{ formatPercent(detail.metrics.risk.avg_drawdown) }}</dd></div>
            <div><dt>水下时间占比</dt><dd>{{ formatPercent(detail.metrics.risk.underwater_ratio) }}</dd></div>
            <div><dt>单日最大涨幅</dt><dd>{{ formatSignedPercent(detail.metrics.risk.max_daily_gain) }}</dd></div>
            <div><dt>单日最大跌幅</dt><dd>{{ formatSignedPercent(detail.metrics.risk.max_daily_loss) }}</dd></div>
            <div><dt>日胜率</dt><dd>{{ formatPercent(detail.metrics.risk.daily_win_rate) }}</dd></div>
            <div><dt>日均收益</dt><dd>{{ formatSignedPercent(detail.metrics.risk.avg_daily_return) }}</dd></div>
            <div><dt>最大连涨</dt><dd>{{ detail.metrics.risk.max_up_streak }} 日</dd></div>
            <div><dt>最大连亏</dt><dd>{{ detail.metrics.risk.max_down_streak }} 日</dd></div>
            <div><dt>恢复因子</dt><dd>{{ formatNumber(detail.metrics.risk.recovery_factor) }}</dd></div>
            <div><dt>Ulcer 指数</dt><dd>{{ formatPercent(detail.metrics.risk.ulcer) }}</dd></div>
            <div><dt>日收益偏度</dt><dd>{{ formatNumber(detail.metrics.risk.skew) }}</dd></div>
            <div><dt>日收益峰度</dt><dd>{{ formatNumber(detail.metrics.risk.kurtosis) }}</dd></div>
          </dl>
          <h3>风险调整</h3>
          <dl class="simulation-metrics">
            <div><dt>年化波动</dt><dd>{{ formatPercent(detail.metrics.risk.volatility) }}</dd></div>
            <div><dt>下行波动</dt><dd>{{ formatPercent(detail.metrics.risk.downside_volatility) }}</dd></div>
            <div><dt>夏普</dt><dd>{{ formatNumber(detail.metrics.risk.sharpe) }}</dd></div>
            <div><dt>Sortino</dt><dd>{{ formatNumber(detail.metrics.risk.sortino) }}</dd></div>
            <div><dt>Calmar</dt><dd>{{ formatNumber(detail.metrics.risk.calmar) }}</dd></div>
            <div><dt>Omega</dt><dd>{{ formatNumber(detail.metrics.risk.omega) }}</dd></div>
            <div><dt>Gain-to-Pain</dt><dd>{{ formatNumber(detail.metrics.risk.gain_to_pain) }}</dd></div>
            <div><dt>Martin 比率</dt><dd>{{ formatNumber(detail.metrics.risk.martin) }}</dd></div>
            <div><dt>尾部比</dt><dd>{{ formatNumber(detail.metrics.risk.tail_ratio) }}</dd></div>
            <div><dt>VaR 95%（单日）</dt><dd>{{ formatSignedPercent(detail.metrics.risk.var95) }}</dd></div>
            <div><dt>CVaR 95%（单日）</dt><dd>{{ formatSignedPercent(detail.metrics.risk.cvar95) }}</dd></div>
          </dl>
          <h3>相对沪深300</h3>
          <dl class="simulation-metrics">
            <div><dt>对齐样本</dt><dd>{{ detail.metrics.relative.days }} 日</dd></div>
            <div><dt>Beta</dt><dd>{{ formatNumber(detail.metrics.relative.beta) }}</dd></div>
            <div><dt>相关系数</dt><dd>{{ formatNumber(detail.metrics.relative.correlation) }}</dd></div>
            <div><dt>跟踪误差</dt><dd>{{ formatPercent(detail.metrics.relative.tracking_error) }}</dd></div>
            <div><dt>信息比率</dt><dd>{{ formatNumber(detail.metrics.relative.information_ratio) }}</dd></div>
            <div><dt>Jensen Alpha</dt><dd>{{ formatPercent(detail.metrics.relative.alpha) }}</dd></div>
            <div><dt>Treynor</dt><dd>{{ formatNumber(detail.metrics.relative.treynor) }}</dd></div>
            <div><dt>上行捕获</dt><dd>{{ formatPercent(detail.metrics.relative.up_capture) }}</dd></div>
            <div><dt>下行捕获</dt><dd>{{ formatPercent(detail.metrics.relative.down_capture) }}</dd></div>
            <div><dt>相对回撤</dt><dd>{{ formatPercent(detail.metrics.relative.excess_max_drawdown) }}</dd></div>
          </dl>
          <h3>交易与集中度</h3>
          <dl class="simulation-metrics">
            <div><dt>已平仓</dt><dd>{{ detail.metrics.trading.closed }} 笔</dd></div>
            <div><dt>胜率</dt><dd>{{ formatPercent(detail.metrics.trading.win_rate) }}</dd></div>
            <div><dt>盈亏比</dt><dd>{{ formatNumber(detail.metrics.trading.payoff_ratio) }}</dd></div>
            <div><dt>盈亏因子</dt><dd>{{ formatNumber(detail.metrics.trading.profit_factor) }}</dd></div>
            <div><dt>平均持仓</dt><dd>{{ formatNumber(detail.metrics.trading.avg_holding_days, 1) }} 天</dd></div>
            <div><dt>平均每笔收益</dt><dd>{{ formatPercent(detail.metrics.trading.avg_trade_return) }}</dd></div>
            <div><dt>单笔最佳收益</dt><dd>{{ formatPercent(detail.metrics.trading.best_trade_return) }}</dd></div>
            <div><dt>单笔最差收益</dt><dd>{{ formatPercent(detail.metrics.trading.worst_trade_return) }}</dd></div>
            <div><dt>最大连盈</dt><dd>{{ detail.metrics.trading.max_win_streak }} 笔</dd></div>
            <div><dt>最大连亏</dt><dd>{{ detail.metrics.trading.max_loss_streak }} 笔</dd></div>
            <div><dt>平均盈利</dt><dd>{{ formatNumber(detail.metrics.trading.avg_win) }}</dd></div>
            <div><dt>平均亏损</dt><dd>{{ formatNumber(detail.metrics.trading.avg_loss) }}</dd></div>
            <div><dt>单笔最佳金额</dt><dd>{{ formatNumber(detail.metrics.trading.best_trade) }}</dd></div>
            <div><dt>单笔最差金额</dt><dd>{{ formatNumber(detail.metrics.trading.worst_trade) }}</dd></div>
            <div><dt>每笔期望</dt><dd>{{ formatNumber(detail.metrics.trading.expectancy) }}</dd></div>
            <div><dt>已实现盈亏</dt><dd>{{ formatNumber(detail.metrics.trading.realized_pnl) }}</dd></div>
            <div><dt>浮动盈亏</dt><dd>{{ formatNumber(detail.metrics.trading.unrealized_pnl) }}</dd></div>
            <div><dt>平均买入名次</dt><dd>{{ formatNumber(detail.metrics.trading.avg_rank, 1) }}</dd></div>
            <div><dt>换手倍数</dt><dd>{{ formatNumber(detail.turnover) }} 倍</dd></div>
            <div><dt>最大单只权重</dt><dd>{{ formatPercent(detail.metrics.concentration.max_weight) }}</dd></div>
            <div><dt>前三大权重</dt><dd>{{ formatPercent(detail.metrics.concentration.top3_weight) }}</dd></div>
            <div><dt>集中度 HHI</dt><dd>{{ formatNumber(detail.metrics.concentration.hhi, 3) }}</dd></div>
            <div><dt>现金占比</dt><dd>{{ formatPercent(detail.metrics.concentration.cash_ratio) }}</dd></div>
          </dl>
          <p class="simulation-copy">年化与比率类指标（年化收益、波动、夏普、Sortino、Calmar、Omega、Gain-to-Pain、Martin、偏度、峰度、尾部比、VaR/CVaR、Beta、跟踪误差、信息比率、Jensen Alpha、Treynor、捕获率）要求至少 20 个净值日样本；不足时显示 —。当前样本 {{ detail.metrics.risk.sample_days }} 个净值日、沪深300 对齐 {{ detail.metrics.relative.days }} 日。</p>
        </template>
        <h3>当前持仓</h3>
        <div class="table-wrap">
          <table>
            <thead><tr><th>标的</th><th>建仓日</th><th>股数</th><th>成本价</th><th>收盘价</th><th>持仓价格收益</th></tr></thead>
            <tbody><tr v-for="pos in detail.positions" :key="pos.market + pos.code">
              <td>{{ pos.market }}{{ pos.code }} {{ pos.name }}</td><td>{{ pos.entry_date }}</td><td>{{ formatNumber(pos.size) }}</td>
              <td>{{ formatNumber(pos.entry_price) }}</td><td>{{ formatNumber(pos.mark) }}</td><td>{{ formatSignedPercent(pos.entry_price > 0 ? pos.mark / pos.entry_price - 1 : null) }}</td>
            </tr></tbody>
          </table>
        </div>
        <h3 class="simulation-trades-title">成交记录</h3>
        <div class="table-wrap">
          <table>
            <thead><tr><th>成交日</th><th>信号日</th><th>标的</th><th>方向</th><th>价格</th><th>数量</th><th>盈亏</th><th>原因 / 买入榜名次</th></tr></thead>
            <tbody><tr v-for="trade in detail.trades" :key="trade.date + trade.symbol + trade.direction">
              <td>{{ trade.date }}</td><td>{{ trade.signal_date }}</td><td>{{ trade.symbol }}</td>
              <td>{{ trade.direction === 'BUY' ? '买入' : '卖出' }}</td>
              <td>{{ formatNumber(trade.price) }}</td><td>{{ formatNumber(trade.size) }}</td>
              <td>{{ formatNumber(trade.pnl) }}</td><td>{{ trade.reason }}{{ trade.rank ? ` · 第 ${trade.rank} 名` : '' }}</td>
            </tr></tbody>
          </table>
        </div>
        <nav class="simulation-legend" aria-label="成交翻页">
          <button class="button-secondary" type="button" :disabled="offset === 0" @click="select(detail.id, offset - pageSize)">上一页</button>
          <span>共 {{ detail.trades_total }} 笔</span>
          <button class="button-secondary" type="button" :disabled="offset + pageSize >= (detail.trades_total ?? 0)" @click="select(detail.id, offset + pageSize)">下一页</button>
        </nav>
      </section>
    </main>
  </div>
</template>

<style scoped>
.simulation-shell { display: grid; grid-template-columns: minmax(0, 1fr); align-content: start; gap: 18px; padding-top: 28px; }
.simulation-back { justify-self: start; color: var(--muted); font-size: 13px; text-decoration: none; }
.simulation-back:hover { color: var(--accent); }
.simulation-header, .simulation-section-heading { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; }
.simulation-header h1 { margin: 6px 0 8px; color: var(--text); font-size: clamp(26px, 3vw, 34px); letter-spacing: -.035em; }
.simulation-copy, .simulation-section-heading small { margin: 0; color: var(--muted); font-size: 13px; line-height: 1.6; }
.panel-card { min-width: 0; padding: clamp(14px, 2vw, 22px); border: 1px solid var(--border); border-radius: 10px; background: var(--surface); }
.simulation-section-heading { margin-bottom: 14px; }
.panel-card h2 { margin: 0; font-size: 17px; color: var(--text); }
.panel-card h3 { font-size: 13px; color: var(--muted); }
.simulation-form { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 180px), 1fr)); gap: 14px; }
.simulation-form label { display: grid; gap: 6px; min-width: 0; color: var(--muted); font-size: 12px; }
.simulation-form input, .simulation-form select { min-width: 0; }
.simulation-form-footer { grid-column: 1 / -1; display: flex; flex-wrap: wrap; align-items: center; justify-content: flex-end; gap: 12px; padding-top: 14px; border-top: 1px solid var(--border); }
.simulation-form-footer .simulation-copy { margin-right: auto; }
.simulation-empty { margin: 0; padding: 20px 14px; border: 1px dashed var(--border-strong); border-radius: 6px; color: var(--muted); font-size: 13px; }
.simulation-legend { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; margin: 10px 0; font-size: 12px; }
.simulation-chart { display: block; width: 100%; max-height: 420px; margin: 0 0 12px; color: var(--muted); }
.simulation-chart text { fill: currentColor; font-size: 11px; }
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 12px; }
th, td { padding: 8px 10px; border-bottom: 1px solid var(--border); white-space: nowrap; text-align: left; font-variant-numeric: tabular-nums; }
th { color: var(--muted); }
td small { display: block; margin-top: 5px; color: var(--warning); white-space: normal; }
.simulation-name { color: var(--accent); font-size: inherit; }
.simulation-name[aria-pressed="true"] { background: var(--accent-soft); }
.simulation-rename { margin-left: 4px; color: var(--muted); font-size: 11px; }
.simulation-excess { display: block; margin-top: 4px; color: var(--muted); font-size: 11px; white-space: normal; }
.simulation-delete { margin-left: 6px; border-color: var(--danger); background: transparent; color: var(--danger); }
.simulation-metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 125px), 1fr)); gap: 10px; margin: 14px 0 18px; }
.simulation-metrics > div { padding: 12px; border: 1px solid var(--border); border-radius: 6px; background: var(--surface-raised); }
.simulation-metrics dt { color: var(--muted); font-size: 12px; }
.simulation-metrics dd { margin: 6px 0 0; font-weight: 700; font-variant-numeric: tabular-nums; }
.simulation-curve-heading { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px; margin: 18px 0 10px; }
.simulation-curve-heading h3 { margin: 0; }
.simulation-trades-title { margin-top: 18px; }
.simulation-empty + .table-wrap { margin-top: 14px; }
@media (max-width: 640px) {
  .simulation-shell { width: calc(100% - 24px); padding-top: 18px; }
}
</style>
