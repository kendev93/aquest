<script setup lang="ts">
import { computed, onActivated, onDeactivated, ref, watch } from 'vue'
import { createRequestClient, ApiError } from '../api/client'
import { formatNumber, formatPercent, formatSignedPercent } from '../utils/format'

const endpoint = '/api/v1/strategy-pool/simulations'
const request = createRequestClient({ errorClass: ApiError, messages: { fallback: '模拟盘请求失败' } })
type Objective = 'annual_return' | 'sharpe'
type Trade = { date: string; signal_date: string; symbol: string; direction: string; price: number; size: number; fee: number; reason: string; rank?: number; pnl?: number }
type Position = { market: string; code: string; name: string | null; size: number; entry_price: number; mark: number; entry_date: string }
type Simulation = {
  id: string; name: string; objective: Objective; ranking: string; min_days: number; direction: string
  start_date: string; initial_cash: number; paused: boolean; warning?: string
  cash: number; value: number; total_return: number; max_drawdown: number
  total_fees: number; turnover: number; holding_count: number
  equity: { date: string; value: number }[]
  positions?: Position[]; trades?: Trade[]; trades_total?: number
}

const rankLabels = {
  postLaunchReturn: '上线后收益', benchmark: '同期买入持有', excess: '超额',
  holdingReturn: '当前持仓收益', createdReturn: '创建至今收益',
} as const
type Rank = keyof typeof rankLabels
const labels = { annual_return: '年化收益', sharpe: '夏普' }
const query = new URLSearchParams(window.location.search)
const objective = ref<Objective>(query.get('objective') === 'sharpe' ? 'sharpe' : 'annual_return')
const initialRank = query.get('ranking')
const ranking = ref<Rank>(initialRank && Object.hasOwn(rankLabels, initialRank) ? initialRank as Rank : 'postLaunchReturn')
const direction = ref(query.get('direction') === 'asc' ? 'asc' : 'desc')
const initialDays = Number(query.get('min_days'))
const minDays = ref([0, 5, 20, 60].includes(initialDays) ? initialDays : 0)
const name = ref('')
const cash = ref(1_000_000)
const days = ref<string[]>([])
const startDate = ref('')
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
  try {
    const response = await request<{ data: string[] }>(`${endpoint}/signal-days?objective=${objective.value}`)
    if (token !== daysRequest) return
    days.value = response.data
    if (!days.value.includes(startDate.value)) startDate.value = days.value[0] ?? ''
  } catch (exc) { if (token === daysRequest) showError(exc) }
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
        name: name.value, objective: objective.value, ranking: ranking.value, direction: direction.value,
        min_days: minDays.value, start_date: startDate.value, initial_cash: cash.value,
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

watch(objective, () => { void loadDays() })
onActivated(() => {
  const search = window.location.search
  if (search !== previousSearch) {
    previousSearch = search
    const params = new URLSearchParams(search)
    if (params.get('create') === '1') {
      creating.value = true
      objective.value = params.get('objective') === 'sharpe' ? 'sharpe' : 'annual_return'
      const rank = params.get('ranking')
      ranking.value = rank && Object.hasOwn(rankLabels, rank) ? rank as Rank : 'postLaunchReturn'
      direction.value = params.get('direction') === 'asc' ? 'asc' : 'desc'
      const threshold = Number(params.get('min_days'))
      minDays.value = [0, 5, 20, 60].includes(threshold) ? threshold : 0
      startDate.value = ''
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
  return {
    low, high, first: new Date(start).toISOString().slice(0, 10), last: new Date(end).toISOString().slice(0, 10),
    lines: items.value.map((item, index) => ({
      id: item.id, name: item.name, color: colors[index % colors.length],
      points: item.equity.map(p => {
        const x = 62 + (Date.parse(p.date) - start) / Math.max(1, end - start) * 712
        const y = 198 - (p.value / item.initial_cash - 1 - low) / span * 176
        return `${x},${y}`
      }).join(' '),
    })).filter(line => line.points),
  }
})
</script>

<template>
  <div class="screen-page simulation-page">
    <a class="table-link" href="/strategy-pool">← 返回全市场策略池</a>
    <section class="panel-card">
      <div class="section-heading">
        <div><p class="eyebrow">SIMULATIONS</p><h1>榜单模拟盘</h1></div>
        <button class="button-secondary" type="button" @click="creating = !creating">{{ creating ? '收起' : '创建模拟盘' }}</button>
      </div>
      <p>最多持有五只。成立和卖出补仓时，按收盘买入信号榜选股，下一交易日开盘先卖后买。</p>
      <p v-if="error" class="field-error" role="alert">{{ error }}</p>
      <form v-if="creating" class="simulation-form" @submit.prevent="create">
        <label>名称<input v-model.trim="name" required maxlength="80" /></label>
        <label>策略目标<select v-model="objective"><option value="annual_return">年化收益</option><option value="sharpe">夏普</option></select></label>
        <label>榜单<select v-model="ranking"><option v-for="(label, key) in rankLabels" :key="key" :value="key">{{ label }}</option></select></label>
        <label>排序<select v-model="direction"><option value="desc">从高到低</option><option value="asc">从低到高</option></select></label>
        <label>上线交易日门槛<select v-model.number="minDays"><option v-for="day in [0, 5, 20, 60]" :key="day" :value="day">{{ day }}</option></select></label>
        <label>首批信号日<select v-model="startDate" required><option v-for="day in days" :key="day" :value="day">{{ day }}</option></select></label>
        <label>初始资金<input v-model.number="cash" type="number" min="1000" max="100000000" required /></label>
        <button class="button-primary" type="submit" :disabled="busy || !startDate">创建并回放</button>
        <p v-if="!days.length">暂无可回放的完整信号。请先完成一轮新版全市场收盘信号扫描。</p>
      </form>
    </section>

    <section class="panel-card">
      <h2>净值对比</h2>
      <template v-if="chart">
        <div class="simulation-legend"><span v-for="line in chart.lines" :key="line.id" :style="{ color: line.color }">{{ line.name }}</span></div>
        <svg class="simulation-chart" viewBox="0 0 800 225" role="img" aria-label="模拟盘累计收益对比">
          <path d="M62 22V198H774" fill="none" stroke="currentColor" />
          <text x="54" y="24" text-anchor="end">{{ formatPercent(chart.high) }}</text>
          <text x="54" y="199" text-anchor="end">{{ formatPercent(chart.low) }}</text>
          <text x="62" y="218">{{ chart.first }}</text><text x="774" y="218" text-anchor="end">{{ chart.last }}</text>
          <polyline v-for="line in chart.lines" :key="line.id" :points="line.points" :stroke="line.color" fill="none" stroke-width="2.5" />
        </svg>
      </template>
      <p v-else>尚无净值。如果已建盘，系统会在下一交易日行情和信号齐备后推进。</p>
      <div v-if="items.length" class="table-wrap">
        <table>
          <thead><tr><th>模拟盘</th><th>榜单</th><th>持仓</th><th>累计收益</th><th>最大回撤</th><th>换手倍数</th><th>交易成本</th><th>状态</th></tr></thead>
          <tbody>
            <tr v-for="item in items" :key="item.id">
              <td><button class="button-secondary" type="button" @click="select(item.id)">{{ item.name }}</button></td>
              <td>{{ labels[item.objective] }} · {{ rankLabels[item.ranking as Rank] }}</td>
              <td>{{ item.holding_count }}/5</td><td>{{ formatSignedPercent(item.total_return) }}</td>
              <td>{{ formatPercent(item.max_drawdown) }}</td><td>{{ formatNumber(item.turnover) }} 倍</td><td>{{ formatNumber(item.total_fees) }}</td>
              <td><button class="button-secondary" type="button" :disabled="busy" @click="toggle(item)">{{ item.paused ? '恢复' : '暂停' }}</button><small v-if="item.warning">{{ item.warning }}</small></td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <section v-if="detail" class="panel-card">
      <h2>{{ detail.name }} · 持仓与成交</h2>
      <p>资产 {{ formatNumber(detail.value) }} · 现金 {{ formatNumber(detail.cash) }} · 持仓 {{ detail.holding_count }} 只</p>
      <div class="table-wrap">
        <table>
          <thead><tr><th>标的</th><th>建仓日</th><th>股数</th><th>成本价</th><th>收盘价</th><th>持仓价格收益</th></tr></thead>
          <tbody><tr v-for="pos in detail.positions" :key="pos.market + pos.code">
            <td>{{ pos.market }}{{ pos.code }} {{ pos.name }}</td><td>{{ pos.entry_date }}</td><td>{{ formatNumber(pos.size) }}</td>
            <td>{{ formatNumber(pos.entry_price) }}</td><td>{{ formatNumber(pos.mark) }}</td><td>{{ formatSignedPercent(pos.entry_price > 0 ? pos.mark / pos.entry_price - 1 : null) }}</td>
          </tr></tbody>
        </table>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>成交日</th><th>信号日</th><th>标的</th><th>方向</th><th>价格</th><th>数量</th><th>成本</th><th>盈亏</th><th>原因 / 买入榜名次</th></tr></thead>
          <tbody><tr v-for="trade in detail.trades" :key="trade.date + trade.symbol + trade.direction">
            <td>{{ trade.date }}</td><td>{{ trade.signal_date }}</td><td>{{ trade.symbol }}</td>
            <td>{{ trade.direction === 'BUY' ? '买入' : '卖出' }}</td>
            <td>{{ formatNumber(trade.price) }}</td><td>{{ formatNumber(trade.size) }}</td><td>{{ formatNumber(trade.fee) }}</td>
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
  </div>
</template>

<style scoped>
.simulation-page { display: grid; gap: 16px; }
.simulation-form { display: flex; flex-wrap: wrap; align-items: end; gap: 12px; }
.simulation-form label { display: grid; gap: 5px; }
.simulation-legend { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; }
.simulation-chart { width: 100%; max-height: 420px; color: var(--muted); }
.simulation-chart text { fill: currentColor; font-size: 11px; }
td small { display: block; }
</style>
