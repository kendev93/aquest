<script setup lang="ts">
import PageIntro from '../components/PageIntro.vue'
import { computed, onMounted, ref } from 'vue'

import TermHint from '../components/TermHint.vue'
import { fetchMetadata } from '../api/formulaScreen'
import { PollTimeoutError, pollUntil } from '../composables/usePolling'
import {
  fetchStrategyTracking,
  getStrategyTracking,
  getStrategyTrackingTrades,
  getTrackingJob,
  launchStrategyTracking,
  rerunStrategyTracking,
  stopStrategyTracking,
} from '../api/savedStrategies'
import type {
  SavedStrategyPerformance,
  StrategyTrackingPoint,
  StrategyTrackingSnapshot,
  TrackingConditionReading,
  TrackingCurrentState,
  TrackingTradeRow,
  TrackingTradePage,
  TrackingTodayCandidates,
  FormulaScreenMetadata,
} from '../types'
import { buildIndicatorNameMap, indicatorDisplayName } from '../utils/indicatorNames'
import AppTopbar from '../components/AppTopbar.vue'
import { formatNumber, formatPercent } from '../utils/format'

type PerformanceKey = keyof SavedStrategyPerformance

const snapshots = ref<StrategyTrackingSnapshot[]>([])
const metadata = ref<FormulaScreenMetadata | null>(null)
const indicatorNames = computed(() => buildIndicatorNameMap(metadata.value))
const loading = ref(false)
const message = ref('')
const messageIsError = ref(false)
const busyId = ref('')
const expandedId = ref('')
// 成交明细只在展开时单独取（列表接口不带它），取过就留在本地，切走回来不重取。
const details = ref<Record<string, StrategyTrackingSnapshot>>({})
const tradePages = ref<Record<string, TrackingTradePage>>({})
const tradeFilters = ref<Record<string, {
  phase: 'all' | 'pre' | 'post'
  dateFrom: string
  dateTo: string
  offset: number
}>>({})
const detailLoadingId = ref('')
const detailError = ref('')
const tradeLoadingId = ref('')
const tradeError = ref('')

const online = computed(() =>
  snapshots.value.filter((item) => item.tracking_status === 'active'),
)
const decayedCount = computed(
  () => online.value.filter((item) => (item.decay?.annual_return ?? 1) < 0.5).length,
)

/** 一年按 252 个交易日折算，和后端绩效口径保持一致。 */
const TRADING_DAYS_PER_YEAR = 252

/**
 * 最小跟踪记录长度（MinTRL）的提示文案。返回空串表示不用提示：
 * 要么算不出来（观察段太短、零波动、平均收益不为正），要么已经够了。
 *
 * 这不是错误提示而是进度：MinTRL 通常远大于观察段（年化夏普 0.6 要七八年），
 * 显示的是"还要等多少个交易日"，不是"你做错了什么"。
 */
function minTrlNote(item: StrategyTrackingSnapshot): string {
  const needed = item.min_track_record_length
  if (needed === null) return ''
  // 上线日记录空仓初始资金，是基点，不算收益观察日。
  const have = Math.max(0, item.observations.length - 1)
  const target = Math.ceil(needed)
  if (target <= have) return ''
  const years = target / TRADING_DAYS_PER_YEAR
  return (
    `按目前的表现，要在 95% 的把握下说它真的赚钱，需要 ${target} 个交易日` +
    `（约 ${years.toFixed(1)} 年）；已观察 ${have} 个，还差 ${target - have} 个。`
  )
}

function showMessage(text: string, isError = false): void {
  message.value = text
  messageIsError.value = isError
}

async function load(): Promise<void> {
  loading.value = true
  try {
    snapshots.value = await fetchStrategyTracking()
    // 重跑之后明细会变，缓存必须一起作废，否则展开看到的是上一次的结果。
    details.value = {}
    tradePages.value = {}
    // 作废之后要立刻把当前展开的那一行补回来：它本来就开着，空缓存会让它显示
    // 「0 笔」，看起来像是重跑把成交记录弄丢了，而实际上只是没人再去取。
    if (expandedId.value) {
      tradeFilterFor(expandedId.value).offset = 0
      await loadDetail(expandedId.value)
    }
  } catch (error) {
    showMessage(error instanceof Error ? error.message : '策略追踪加载失败。', true)
  } finally {
    loading.value = false
  }
}

async function loadDetail(strategyId: string): Promise<void> {
  detailLoadingId.value = strategyId
  detailError.value = ''
  try {
    details.value[strategyId] = await getStrategyTracking(strategyId)
    await loadTrades(strategyId)
  } catch (error) {
    detailError.value = error instanceof Error ? error.message : '交易明细读取失败。'
  } finally {
    detailLoadingId.value = ''
  }
}

function toggle(strategyId: string): void {
  const next = expandedId.value === strategyId ? '' : strategyId
  expandedId.value = next
  if (next && !details.value[next]) void loadDetail(next)
}

function tradesOf(strategyId: string) {
  return tradePages.value[strategyId] ?? null
}

function currentStateFor(item: StrategyTrackingSnapshot): TrackingCurrentState | null {
  return details.value[item.strategy_id]?.current_state ?? item.current_state ?? null
}

function candidatesFor(item: StrategyTrackingSnapshot): TrackingTodayCandidates | null {
  return details.value[item.strategy_id]?.today_candidates ?? item.today_candidates ?? null
}

function tradeFilterFor(strategyId: string) {
  if (!tradeFilters.value[strategyId]) {
    tradeFilters.value[strategyId] = { phase: 'all', dateFrom: '', dateTo: '', offset: 0 }
  }
  return tradeFilters.value[strategyId]
}

async function loadTrades(strategyId: string, resetOffset = false): Promise<void> {
  const filters = tradeFilterFor(strategyId)
  if (resetOffset) filters.offset = 0
  tradeLoadingId.value = strategyId
  tradeError.value = ''
  try {
    tradePages.value[strategyId] = await getStrategyTrackingTrades(strategyId, {
      phase: filters.phase === 'all' ? '' : filters.phase,
      date_from: filters.dateFrom,
      date_to: filters.dateTo,
      limit: 50,
      offset: filters.offset,
    })
  } catch (error) {
    tradeError.value = error instanceof Error ? error.message : '成交明细读取失败。'
  } finally {
    tradeLoadingId.value = ''
  }
}

function applyTradeFilters(strategyId: string): void {
  void loadTrades(strategyId, true)
}

function changeTradePage(strategyId: string, direction: -1 | 1): void {
  const page = tradesOf(strategyId)
  if (!page) return
  const nextOffset = page.offset + direction * page.limit
  if (nextOffset < 0 || nextOffset >= page.total) return
  tradeFilterFor(strategyId).offset = nextOffset
  void loadTrades(strategyId)
}

function tradeRange(page: TrackingTradePage | null): string {
  if (!page || page.total === 0) return '0 笔'
  return `${page.offset + 1}–${Math.min(page.offset + page.rows.length, page.total)} / ${page.total} 笔`
}

function recordedCost(value: number | null): string {
  return value === null ? '未记录' : formatPreciseMoney(value)
}

function operatorLabel(operator: string): string {
  return ({
    gt: '>', gte: '≥', lt: '<', lte: '≤', eq: '=', neq: '≠',
    cross_up: '上穿', cross_down: '下穿',
  } as Record<string, string>)[operator] ?? operator
}

function conditionLabel(condition: TrackingConditionReading): string {
  const left = indicatorDisplayName(indicatorNames.value, condition.left_value)
  const right = condition.right_value
    ? indicatorDisplayName(indicatorNames.value, condition.right_value)
    : formatNumber(condition.threshold)
  return `${left} ${operatorLabel(condition.operator)} ${right}`
}

function conditionValues(condition: TrackingConditionReading): string {
  const current = `${formatNumber(condition.current_left_value)}${condition.right_value ? ` / ${formatNumber(condition.current_right_value)}` : ''}`
  if (condition.operator !== 'cross_up' && condition.operator !== 'cross_down') return current
  const previous = `${formatNumber(condition.previous_left_value)}${condition.right_value ? ` / ${formatNumber(condition.previous_right_value)}` : ''}`
  return `${previous} → ${current}`
}

function signalSummary(signals: Array<{ id: string; matched: boolean }>): string {
  return signals.length
    ? signals.map((signal) => `${indicatorDisplayName(indicatorNames.value, signal.id)}：${signal.matched ? '满足' : '未满足'}`).join('；')
    : '未配置独立信号'
}

/** 重跑要重算全历史，是分钟级任务，所以提交完只能轮询。30 分钟内没跑完就放弃。 */
async function waitForJob(jobId: string, strategyId: string): Promise<void> {
  busyId.value = strategyId
  try {
    const state = await pollUntil(
      () => getTrackingJob(jobId),
      {
        intervalMs: 2000,
        timeoutMs: 30 * 60 * 1000,
        until: (current) => {
          if (current.status === 'failed') {
            showMessage(current.error || '追踪任务失败，已保留上一次成功的结果。', true)
            return true
          }
          return current.status === 'completed'
        },
      },
    )
    if (state.status === 'completed') {
      await load()
      showMessage('追踪已更新。')
    } else {
      // until 里已把失败消息提示过了，这里补一次列表刷新。
      await load()
    }
  } catch (error) {
    if (error instanceof PollTimeoutError) {
      showMessage('追踪任务超时，请稍后手动重算。', true)
    } else {
      showMessage(error instanceof Error ? error.message : '追踪任务状态读取失败。', true)
    }
  } finally {
    busyId.value = ''
  }
}

async function launch(strategyId: string): Promise<void> {
  showMessage('')
  try {
    const accepted = await launchStrategyTracking(strategyId)
    await waitForJob(accepted.job_id, strategyId)
  } catch (error) {
    showMessage(error instanceof Error ? error.message : '上线失败。', true)
    busyId.value = ''
  }
}

async function rerun(strategyId: string): Promise<void> {
  showMessage('')
  try {
    const accepted = await rerunStrategyTracking(strategyId)
    await waitForJob(accepted.job_id, strategyId)
  } catch (error) {
    showMessage(error instanceof Error ? error.message : '重算失败。', true)
    busyId.value = ''
  }
}

async function stop(strategyId: string): Promise<void> {
  showMessage('')
  busyId.value = strategyId
  try {
    await stopStrategyTracking(strategyId)
    await load()
    showMessage('已停止追踪，已观察到的记录保留。')
  } catch (error) {
    showMessage(error instanceof Error ? error.message : '停止追踪失败。', true)
  } finally {
    busyId.value = ''
  }
}

/** 列表行上的一句话：明天有没有要动的手。 */
function planBadge(item: StrategyTrackingSnapshot): string {
  const plan = item.next_plan
  if (!plan) return ''
  const sells = plan.sells.length
  const buys = plan.buys.length
  if (sells === 0 && buys === 0) return '明日无需操作'
  return `明日：卖 ${sells} / 买 ${buys}`
}

function tradeRows(strategyId: string): TrackingTradeRow[] {
  return tradesOf(strategyId)?.rows ?? []
}

function tradeTotal(strategyId: string): number {
  return tradesOf(strategyId)?.total ?? 0
}

onMounted(() => {
  void load()
  void fetchMetadata()
    .then((value) => { metadata.value = value })
    .catch(() => undefined)
})

// ---------------------------------------------------------------------------
// 展示
// ---------------------------------------------------------------------------

function formatDate(value: number | null): string {
  if (value === null) return '—'
  const text = String(value)
  return `${text.slice(0, 4)}-${text.slice(4, 6)}-${text.slice(6, 8)}`
}

function formatMoney(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  return value.toLocaleString('zh-CN', { maximumFractionDigits: 0 })
}

function formatPreciseMoney(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  return value.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

/** 观察段天数。上线日的空仓初始资金行是基点，不算收益观察日。 */
function observationDays(item: StrategyTrackingSnapshot): number {
  return Math.max(0, item.observations.length - 1)
}

function metric(
  segment: StrategyTrackingSnapshot['research'],
  key: PerformanceKey,
): number | null | undefined {
  return segment?.performance?.[key]
}

/** 纵轴必须同时装下策略线和基准线，否则基准跑出画框，看着像策略完胜。 */
function chartGeometry(item: StrategyTrackingSnapshot) {
  const rows = item.observations
  if (rows.length < 2) return null
  const values: number[] = []
  for (const row of rows) {
    values.push(row.equity)
    if (row.benchmark_value !== null) values.push(row.benchmark_value)
  }
  const minimum = Math.min(...values)
  const maximum = Math.max(...values)
  const span = maximum - minimum || 1
  const stepX = 780 / (rows.length - 1)
  const toY = (value: number) => 190 - ((value - minimum) / span) * 170
  const build = (pick: (row: StrategyTrackingPoint) => number | null) =>
    rows
      .map((row, index) => {
        const value = pick(row)
        return value === null ? null : `${(10 + index * stepX).toFixed(1)},${toY(value).toFixed(1)}`
      })
      .filter((point): point is string => point !== null)
      .join(' ')
  return {
    strategy: build((row) => row.equity),
    benchmark: build((row) => row.benchmark_value),
    hasBenchmark: rows.some((row) => row.benchmark_value !== null),
  }
}

function chartFor(item: StrategyTrackingSnapshot) {
  return chartGeometry(item)
}
</script>

<template>
  <div class="screen-page tracking-page" data-testid="strategy-tracking-page">
    <AppTopbar />

    <main class="content-shell">
      <PageIntro
        eyebrow="OUT OF SAMPLE / DAILY"
        title="策略追踪"
        note-label="研究方法"
        guide-key="strategy-tracking"
      >
        <template #copy>
          把保存下来的策略沿着交易日历往前推，回答别的功能回答不了的那个问题：<strong>从今天起，它还灵不灵。</strong>
        </template>
        <strong>规则上线即冻结</strong>
        <small>要改规则请新开一个追踪，改了就不是样本外了</small>
      </PageIntro>

      <p v-if="message" class="notice" :class="{ error: messageIsError }" role="alert" data-testid="tracking-message">{{ message }}</p>

      <section class="panel" data-testid="tracking-overview" aria-labelledby="tracking-overview-title">
        <div class="panel-heading">
          <div><span class="section-kicker">OVERVIEW</span><h2 id="tracking-overview-title">追踪总览</h2></div>
          <span class="library-count" data-testid="tracking-count">{{ online.length }} / {{ snapshots.length }} 个已上线</span>
        </div>
        <p class="library-hint">「研究段」是上线前那段回测，「观察段」是上线之后真实走出来的部分。衰减比 = 观察段 ÷ 研究段，它只是呈现，不替你下结论。</p>
        <p v-if="online.length" class="library-hint" data-testid="tracking-decay-summary">当前 {{ online.length }} 个在线策略里，有 <strong>{{ decayedCount }}</strong> 个的年化衰减到研究段的一半以下。</p>

        <p v-if="loading && !snapshots.length" class="library-empty">正在加载追踪…</p>
        <p v-else-if="!snapshots.length" class="library-empty" data-testid="tracking-empty">还没有保存的策略。先在「组合回测」里跑一次并保存为策略，再回到这里开始追踪。</p>

        <div v-else class="table-wrap">
          <table data-testid="tracking-table">
            <caption class="sr-only">策略追踪总览</caption>
            <thead>
              <tr>
                <th>策略</th>
                <th>状态</th>
                <th>上线日<TermHint term="launchDay" /></th>
                <th>观察天数</th>
                <th>研究段年化</th>
                <th>观察段年化</th>
                <th>衰减比<TermHint term="decay" /></th>
                <th>相对基准<TermHint term="excessReturn" /></th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in snapshots" :key="item.strategy_id" :data-testid="`tracking-row-${item.strategy_id}`">
                <td>
                  <button class="text-button" type="button" :data-testid="`tracking-toggle-${item.strategy_id}`" @click="toggle(item.strategy_id)">
                    {{ item.name || item.strategy_id }}
                  </button>
                  <span v-if="item.last_error" class="error-pill" :data-testid="`tracking-error-${item.strategy_id}`">上次失败</span>
                  <span v-if="planBadge(item)" class="plan-pill" :data-testid="`tracking-plan-badge-${item.strategy_id}`">{{ planBadge(item) }}</span>
                </td>
                <td>
                  <span class="status-pill" :class="item.tracking_status === 'active' ? 'live' : 'idle'" :data-testid="`tracking-status-${item.strategy_id}`">
                    {{ item.tracking_status === 'active' ? '已上线' : '未上线' }}
                  </span>
                </td>
                <td class="muted-code">{{ formatDate(item.launched_at) }}</td>
                <td>{{ observationDays(item) }}</td>
                <td>{{ formatPercent(metric(item.research, 'annual_return')) }}</td>
                <td>{{ formatPercent(metric(item.observation, 'annual_return')) }}</td>
                <td>
                  <span
                    v-if="item.decay?.annual_return === null || item.decay?.annual_return === undefined"
                    class="muted"
                    title="研究段该指标为负或缺失，衰减比不适用（相除会让人读出相反结论），请直接对比两段的数值"
                  >—</span>
                  <strong v-else :class="{ negative: item.decay.annual_return < 0.5 }" :data-testid="`tracking-decay-${item.strategy_id}`">{{ formatNumber(item.decay.annual_return) }}×</strong>
                </td>
                <td :class="{ positive: (item.excess_annual_return ?? 0) > 0, negative: (item.excess_annual_return ?? 0) < 0 }">
                  {{ formatPercent(item.excess_annual_return) }}
                </td>
                <td class="tracking-actions">
                  <button
                    v-if="item.tracking_status !== 'active'"
                    class="secondary-button"
                    type="button"
                    :data-testid="`tracking-launch-${item.strategy_id}`"
                    :disabled="busyId === item.strategy_id"
                    @click="launch(item.strategy_id)"
                  >开始追踪</button>
                  <template v-else>
                    <button class="secondary-button" type="button" :data-testid="`tracking-rerun-${item.strategy_id}`" :disabled="busyId === item.strategy_id" @click="rerun(item.strategy_id)">立即重算</button>
                    <button class="text-button" type="button" :data-testid="`tracking-stop-${item.strategy_id}`" :disabled="busyId === item.strategy_id" @click="stop(item.strategy_id)">停止</button>
                  </template>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <section
        v-for="item in snapshots.filter((entry) => expandedId === entry.strategy_id)"
        :key="`detail-${item.strategy_id}`"
        class="panel tracking-detail"
        :data-testid="`tracking-detail-${item.strategy_id}`"
        :aria-label="`${item.name || item.strategy_id} 追踪详情`"
      >
        <div class="panel-heading">
          <div><span class="section-kicker">DETAIL</span><h3>{{ item.name || item.strategy_id }}</h3></div>
          <span class="muted-code" :data-testid="`tracking-updated-${item.strategy_id}`">
            <template v-if="detailLoadingId === item.strategy_id">详情读取中 · </template>
            算到 {{ formatDate(item.last_tracked_date) }}
          </span>
        </div>

        <p v-if="item.last_error" class="notice error" :data-testid="`tracking-error-detail-${item.strategy_id}`">{{ item.last_error }}</p>
        <p v-if="detailError" class="field-error" data-testid="tracking-detail-error">{{ detailError }}</p>

        <p v-if="item.observation_is_short" class="sample-warning" data-testid="tracking-short-warning">
          观察段还不到 20 个交易日，此时的年化和衰减比基本是噪声，先当参考，别当结论。
        </p>
        <p v-else-if="minTrlNote(item)" class="sample-warning" data-testid="tracking-mintrl-note">
          {{ minTrlNote(item) }}<TermHint term="minTrackRecordLength" />
        </p>

        <section class="data-section tracking-state" :data-testid="`tracking-current-state-${item.strategy_id}`">
          <div class="section-heading">
            <div><span class="section-kicker">CURRENT STATE</span><h4>当前账户与持仓</h4></div>
            <span v-if="currentStateFor(item)?.as_of" class="muted-code">截至 {{ currentStateFor(item)?.as_of }}</span>
          </div>
          <template v-if="currentStateFor(item)">
            <div class="tracking-account-strip">
              <div><span>总资产</span><strong>{{ formatMoney(currentStateFor(item)?.total_asset) }}</strong></div>
              <div><span>现金</span><strong>{{ formatMoney(currentStateFor(item)?.cash) }}</strong></div>
              <div><span>持仓市值</span><strong>{{ formatMoney(currentStateFor(item)?.position_value) }}</strong></div>
              <div><span>持仓数</span><strong>{{ currentStateFor(item)?.positions_count }}</strong></div>
            </div>
            <p v-if="!currentStateFor(item)?.holdings.length" class="plan-empty">截至该日为空仓。</p>
            <div v-else class="table-wrap">
              <table :data-testid="`tracking-holdings-${item.strategy_id}`">
                <thead><tr><th>标的</th><th>数量</th><th>建仓日期</th><th>成本价</th><th>最新价</th><th>市值</th><th>浮动盈亏</th><th>仓位占比</th><th>卖出指标</th></tr></thead>
                <tbody>
                  <tr v-for="holding in currentStateFor(item)?.holdings ?? []" :key="`${holding.market}${holding.code}`">
                    <td><strong class="code">{{ holding.code }}</strong><small class="holding-name">{{ holding.name || '—' }}</small></td>
                    <td>{{ formatMoney(holding.size) }}</td>
                    <td class="muted-code">{{ holding.entry_date }}</td>
                    <td>{{ formatPreciseMoney(holding.entry_price) }}</td>
                    <td>{{ formatPreciseMoney(holding.latest_price) }}</td>
                    <td>{{ formatMoney(holding.market_value) }}</td>
                    <td :class="{ positive: holding.unrealized_pnl > 0, negative: holding.unrealized_pnl < 0 }">
                      {{ formatPreciseMoney(holding.unrealized_pnl) }} <small>({{ formatPercent(holding.unrealized_pnl_pct) }})</small>
                    </td>
                    <td>{{ formatPercent(holding.position_weight) }}</td>
                    <td>
                      <span :class="holding.sell_signal.matched ? 'negative' : 'muted'">{{ holding.sell_signal.matched ? '满足' : '未满足' }}</span>
                      <small v-if="holding.indicator_as_of" class="holding-name">指标截至 {{ holding.indicator_as_of }}</small>
                      <details v-if="holding.sell_signal.signals.length || holding.sell_signal.conditions.length" class="indicator-detail">
                        <summary>查看指标值</summary>
                        <p v-if="holding.sell_signal.signals.length" class="signal-readout">{{ signalSummary(holding.sell_signal.signals) }}</p>
                        <table v-if="holding.sell_signal.conditions.length" class="compact-table">
                          <thead><tr><th>条件</th><th>前值 → 当前值</th><th>阈值 / 右值</th><th>结果</th></tr></thead>
                          <tbody><tr v-for="(condition, index) in holding.sell_signal.conditions" :key="index">
                            <td>{{ conditionLabel(condition) }}</td><td>{{ conditionValues(condition) }}</td>
                            <td>{{ condition.right_value ? formatNumber(condition.current_right_value) : formatNumber(condition.threshold) }}</td>
                            <td>{{ condition.matched ? '满足' : '未满足' }}</td>
                          </tr></tbody>
                        </table>
                      </details>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
            <p class="plan-note">浮动盈亏按最近收盘价估算，不含未来卖出费用。</p>
          </template>
          <p v-else-if="item.launched_at !== null" class="plan-empty" data-testid="tracking-state-not-saved">历史版本未保存持仓快照。</p>
          <p v-else class="plan-empty">策略上线后会在首次成功计算时保存账户快照。</p>
        </section>

        <section class="data-section candidate-section" :data-testid="`tracking-candidates-${item.strategy_id}`">
          <div class="section-heading">
            <div><span class="section-kicker">TODAY CANDIDATES</span><h4>当日候选</h4></div>
            <span v-if="candidatesFor(item)?.as_of" class="muted-code">{{ candidatesFor(item)?.as_of }} · 命中 {{ candidatesFor(item)?.total }} 只</span>
          </div>
          <p class="plan-note">候选扫描每天按策略排名展示买入条件命中的标的，只用于观察指标当前值；实际待执行订单以「下一交易日计划」为准。</p>
          <p v-if="!candidatesFor(item) && item.launched_at !== null" class="plan-empty">历史版本未保存当日候选快照。</p>
          <p v-else-if="candidatesFor(item)?.total === 0" class="plan-empty">该日没有满足买入条件的候选。</p>
          <div v-else-if="candidatesFor(item)?.rows.length" class="table-wrap">
            <table>
              <thead><tr><th>排名</th><th>标的</th><th>排名指标值</th><th>排序分</th><th>买入信号</th><th>买入条件当前值</th></tr></thead>
              <tbody>
                <tr v-for="candidate in candidatesFor(item)?.rows" :key="`${candidate.market}${candidate.code}`">
                  <td>{{ candidate.rank }}</td>
                  <td><strong class="code">{{ candidate.code }}</strong><small class="holding-name">{{ candidate.name || '—' }}<template v-if="candidate.already_held"> · 已持有</template></small></td>
                  <td>{{ formatNumber(candidate.score) }}</td>
                  <td>{{ formatNumber(candidate.rank_score) }}</td>
                  <td><span :class="candidate.buy_signal.matched ? 'positive' : 'muted'">{{ candidate.buy_signal.matched ? '匹配' : '不匹配' }}</span><small class="signal-readout">{{ signalSummary(candidate.buy_signal.signals) }}</small></td>
                  <td>
                    <span v-if="!candidate.buy_conditions.length" class="muted">无数值条件</span>
                    <details v-else class="indicator-detail">
                      <summary>{{ candidate.buy_conditions.filter((condition) => condition.matched).length }} / {{ candidate.buy_conditions.length }} 条满足</summary>
                      <table class="compact-table">
                        <thead><tr><th>条件</th><th>前值 → 当前值</th><th>阈值 / 右值</th><th>结果</th></tr></thead>
                        <tbody><tr v-for="(condition, index) in candidate.buy_conditions" :key="index">
                          <td>{{ conditionLabel(condition) }}</td><td>{{ conditionValues(condition) }}</td>
                          <td>{{ condition.right_value ? formatNumber(condition.current_right_value) : formatNumber(condition.threshold) }}</td>
                          <td>{{ condition.matched ? '满足' : '未满足' }}</td>
                        </tr></tbody>
                      </table>
                    </details>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>

        <section v-if="item.next_plan" class="plan-card" :data-testid="`tracking-plan-${item.strategy_id}`">
          <div class="section-heading">
            <div><span class="section-kicker">NEXT SESSION</span><h4>下一交易日计划</h4></div>
            <span class="muted-code">基于 {{ item.next_plan.as_of }} 收盘</span>
          </div>

          <p class="plan-note">这是下一交易日待执行计划，和上方当日候选扫描、当前持仓快照分开。</p>

          <div class="plan-grid">
            <div class="plan-column">
              <h5>卖出 {{ item.next_plan.sells.length }} 只</h5>
              <p v-if="!item.next_plan.sells.length" class="plan-empty">明日没有要卖的。</p>
              <div v-else class="table-wrap">
                <table :data-testid="`tracking-plan-sells-${item.strategy_id}`">
                  <thead><tr><th>代码</th><th>名称</th><th>股数</th><th>参考价</th><th>原因</th></tr></thead>
                  <tbody>
                    <tr v-for="order in item.next_plan.sells" :key="order.code">
                      <td><strong class="code">{{ order.code }}</strong></td>
                      <td>{{ order.name || '—' }}</td>
                      <td>{{ formatMoney(order.size) }}</td>
                      <td>{{ formatPreciseMoney(order.reference_price) }}</td>
                      <td>{{ order.reason || '—' }}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>
            <div class="plan-column">
              <h5>买入 {{ item.next_plan.buys.length }} 只</h5>
              <p v-if="!item.next_plan.buys.length" class="plan-empty">
                {{ item.next_plan.rebalance_due ? '明日没有要买的。' : '不是调仓日，不产生买入计划。' }}
              </p>
              <div v-else class="table-wrap">
                <table :data-testid="`tracking-plan-buys-${item.strategy_id}`">
                  <thead><tr><th>代码</th><th>名称</th><th>估算股数</th><th>参考价</th><th>估算金额</th></tr></thead>
                  <tbody>
                    <tr v-for="order in item.next_plan.buys" :key="order.code">
                      <td><strong class="code">{{ order.code }}</strong></td>
                      <td>{{ order.name || '—' }}</td>
                      <td>{{ formatMoney(order.estimated_size) }}</td>
                      <td>{{ formatPreciseMoney(order.reference_price) }}</td>
                      <td>{{ formatPreciseMoney(order.estimated_cost) }}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>
          </div>

          <p v-for="note in item.next_plan.notes" :key="note" class="plan-note">{{ note }}</p>
        </section>

        <div class="segment-grid">
          <div class="segment-card">
            <span class="section-kicker">RESEARCH</span>
            <h4>研究段<TermHint term="researchSegment" /></h4>
            <p class="muted-code">{{ item.research?.start_date ?? '—' }} → {{ item.research?.end_date ?? '—' }}</p>
            <div class="metrics-strip">
              <div><span>总收益</span><strong>{{ formatPercent(metric(item.research, 'total_return')) }}</strong></div>
              <div><span>年化</span><strong>{{ formatPercent(metric(item.research, 'annual_return')) }}</strong></div>
              <div><span>夏普</span><strong>{{ formatNumber(metric(item.research, 'sharpe')) }}</strong></div>
              <div><span>最大回撤</span><strong class="negative">{{ formatPercent(metric(item.research, 'max_drawdown')) }}</strong></div>
            </div>
          </div>
          <div class="segment-card">
            <span class="section-kicker">OBSERVATION</span>
            <h4>观察段<TermHint term="observationSegment" /></h4>
            <p class="muted-code">{{ item.observation?.start_date ?? '—' }} → {{ item.observation?.end_date ?? '—' }}</p>
            <div class="metrics-strip">
              <div><span>总收益</span><strong>{{ formatPercent(metric(item.observation, 'total_return')) }}</strong></div>
              <div><span>年化</span><strong>{{ formatPercent(metric(item.observation, 'annual_return')) }}</strong></div>
              <div><span>夏普</span><strong>{{ formatNumber(metric(item.observation, 'sharpe')) }}</strong></div>
              <div><span>最大回撤</span><strong class="negative">{{ formatPercent(metric(item.observation, 'max_drawdown')) }}</strong></div>
            </div>
            <div v-if="item.benchmark_performance" class="benchmark-inline">
              同期基准年化 {{ formatPercent(item.benchmark_performance.annual_return) }} ·
              超额 <strong :class="{ positive: (item.excess_annual_return ?? 0) > 0, negative: (item.excess_annual_return ?? 0) < 0 }">{{ formatPercent(item.excess_annual_return) }}</strong>
            </div>
          </div>
        </div>

        <section v-if="chartFor(item)" class="chart-card">
          <div class="chart-heading">
            <div><span class="section-kicker">OBSERVATION CURVE</span><h4>观察段净值</h4></div>
            <span class="chart-legend">
              <i aria-hidden="true"></i>策略
              <template v-if="chartFor(item)?.hasBenchmark"><i class="legend-benchmark" aria-hidden="true"></i>基准</template>
            </span>
          </div>
          <svg class="equity-chart" viewBox="0 0 800 200" role="img" :aria-label="`观察段净值曲线，共 ${observationDays(item)} 个交易日`" :data-testid="`tracking-chart-${item.strategy_id}`">
            <polyline v-if="chartFor(item)?.benchmark" :points="chartFor(item)?.benchmark" fill="none" stroke="var(--muted)" stroke-width="2" stroke-dasharray="6 4" stroke-linecap="round" stroke-linejoin="round" />
            <polyline :points="chartFor(item)?.strategy" fill="none" stroke="var(--accent)" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
          <p class="chart-summary">策略以初始资金、空仓起步；基准按上线日归一化为同等资产，之后按同一批标的等权买入不动。</p>
        </section>
        <p v-else class="library-empty">观察段还没有足够的数据可以画曲线。</p>

        <section class="data-section">
          <div class="section-heading">
            <div><span class="section-kicker">OBSERVATIONS</span><h4>每日观察</h4></div>
            <span>{{ item.observations.length }} 行</span>
          </div>
          <div class="table-wrap">
            <table :data-testid="`tracking-observations-${item.strategy_id}`">
              <caption class="sr-only">每日观察记录</caption>
              <thead>
                <tr><th>日期</th><th>总资产</th><th>现金</th><th>持仓数</th><th>基准</th><th>回撤</th></tr>
              </thead>
              <tbody>
                <tr v-for="row in item.observations" :key="row.trade_date">
                  <td class="muted-code">{{ formatDate(row.trade_date) }}</td>
                  <td><strong>{{ formatMoney(row.equity) }}</strong></td>
                  <td>{{ formatMoney(row.cash) }}</td>
                  <td>{{ row.positions_count }}</td>
                  <td>{{ formatMoney(row.benchmark_value) }}</td>
                  <td class="negative">{{ formatPercent(row.drawdown) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>

        <section class="data-section" :data-testid="`tracking-trades-${item.strategy_id}`">
          <div class="section-heading">
            <div><span class="section-kicker">TRADES</span><h4>成交明细</h4></div>
            <span v-if="tradeLoadingId === item.strategy_id" data-testid="trades-loading">读取中…</span>
          </div>
          <p v-if="tradeError" class="field-error" data-testid="trades-error">{{ tradeError }}</p>
          <div class="trade-filters">
            <label>阶段
              <select v-model="tradeFilterFor(item.strategy_id).phase" @change="applyTradeFilters(item.strategy_id)">
                <option value="all">全部</option><option value="post">上线后</option><option value="pre">上线前</option>
              </select>
            </label>
            <label>成交日起
              <input v-model="tradeFilterFor(item.strategy_id).dateFrom" type="date" @change="applyTradeFilters(item.strategy_id)">
            </label>
            <label>成交日止
              <input v-model="tradeFilterFor(item.strategy_id).dateTo" type="date" @change="applyTradeFilters(item.strategy_id)">
            </label>
            <span class="muted-code">{{ tradeRange(tradesOf(item.strategy_id)) }}</span>
          </div>
          <p v-if="!tradeRows(item.strategy_id).length && !tradeLoadingId" class="plan-empty">
            {{ tradeTotal(item.strategy_id) === 0 ? '筛选范围内没有实际成交。' : '正在读取成交记录。' }}
          </p>
          <div v-else-if="tradeRows(item.strategy_id).length" class="table-wrap">
            <table>
              <thead><tr><th>成交日</th><th>信号日</th><th>方向</th><th>标的</th><th>数量</th><th>成交价</th><th>手续费</th><th>印花税</th><th>滑点</th><th>成本价</th><th>已实现盈亏</th><th>原因</th></tr></thead>
              <tbody>
                <tr v-for="(row, index) in tradeRows(item.strategy_id)" :key="`${row.trade_date}-${row.code}-${index}`">
                  <td class="muted-code">{{ formatDate(row.trade_date) }}</td>
                  <td class="muted-code">{{ row.signal_date === null ? '未记录' : formatDate(row.signal_date) }}</td>
                  <td :class="row.direction === 'SELL' ? 'negative' : 'positive'">{{ row.direction === 'SELL' ? '卖出' : '买入' }}</td>
                  <td><strong class="code">{{ row.code }}</strong><small class="holding-name">{{ row.name || '—' }}</small></td>
                  <td>{{ formatMoney(row.size) }}</td>
                  <td>{{ formatPreciseMoney(row.price) }}</td>
                  <td>{{ recordedCost(row.commission) }}</td>
                  <td>{{ recordedCost(row.stamp_tax) }}</td>
                  <td>{{ recordedCost(row.slippage) }}</td>
                  <td>{{ row.direction === 'SELL' ? recordedCost(row.cost_basis) : '—' }}</td>
                  <td :class="{ positive: row.direction === 'SELL' && row.pnl > 0, negative: row.direction === 'SELL' && row.pnl < 0 }">{{ row.direction === 'SELL' ? formatPreciseMoney(row.pnl) : '—' }}</td>
                  <td>{{ row.reason || '—' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <div v-if="tradesOf(item.strategy_id)?.total" class="trade-pagination">
            <button class="secondary-button" type="button" :disabled="tradeLoadingId === item.strategy_id || (tradesOf(item.strategy_id)?.offset ?? 0) === 0" @click="changeTradePage(item.strategy_id, -1)">上一页</button>
            <span>{{ tradeRange(tradesOf(item.strategy_id)) }} · 每页 {{ tradesOf(item.strategy_id)?.limit }} 笔</span>
            <button class="secondary-button" type="button" :disabled="tradeLoadingId === item.strategy_id || (tradesOf(item.strategy_id)?.offset ?? 0) + (tradesOf(item.strategy_id)?.rows.length ?? 0) >= tradeTotal(item.strategy_id)" @click="changeTradePage(item.strategy_id, 1)">下一页</button>
          </div>
        </section>
      </section>
    </main>
  </div>
</template>
