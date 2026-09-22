<script setup lang="ts">
import PageGuide from '../components/PageGuide.vue'
import { computed, onMounted, onUnmounted, ref } from 'vue'

import TermHint from '../components/TermHint.vue'
import {
  fetchStrategyTracking,
  getTrackingJob,
  launchStrategyTracking,
  rerunStrategyTracking,
  stopStrategyTracking,
} from '../api/savedStrategies'
import type {
  SavedStrategyPerformance,
  StrategyTrackingPoint,
  StrategyTrackingSnapshot,
} from '../types'

type PerformanceKey = keyof SavedStrategyPerformance

const snapshots = ref<StrategyTrackingSnapshot[]>([])
const loading = ref(false)
const message = ref('')
const messageIsError = ref(false)
const busyId = ref('')
const expandedId = ref('')
let pollTimer: ReturnType<typeof setTimeout> | null = null

const online = computed(() =>
  snapshots.value.filter((item) => item.tracking_status === 'active'),
)
const tracked = computed(() => snapshots.value.filter((item) => item.launched_at !== null))
const decayedCount = computed(
  () => online.value.filter((item) => (item.decay?.annual_return ?? 1) < 0.5).length,
)

function showMessage(text: string, isError = false): void {
  message.value = text
  messageIsError.value = isError
}

async function load(): Promise<void> {
  loading.value = true
  try {
    snapshots.value = await fetchStrategyTracking()
  } catch (error) {
    showMessage(error instanceof Error ? error.message : '策略追踪加载失败。', true)
  } finally {
    loading.value = false
  }
}

/** 重跑要重算全历史，是分钟级任务，所以提交完只能轮询。 */
async function waitForJob(jobId: string, strategyId: string): Promise<void> {
  busyId.value = strategyId
  const deadline = Date.now() + 30 * 60 * 1000
  try {
    while (Date.now() < deadline) {
      const state = await getTrackingJob(jobId)
      if (state.status === 'completed') {
        await load()
        showMessage('追踪已更新。')
        return
      }
      if (state.status === 'failed') {
        showMessage(state.error || '追踪任务失败，已保留上一次成功的结果。', true)
        await load()
        return
      }
      await new Promise((resolve) => setTimeout(resolve, 2000))
    }
    showMessage('追踪任务超时，请稍后手动重算。', true)
  } catch (error) {
    showMessage(error instanceof Error ? error.message : '追踪任务状态读取失败。', true)
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

function toggle(strategyId: string): void {
  expandedId.value = expandedId.value === strategyId ? '' : strategyId
}

onMounted(load)
onUnmounted(() => {
  if (pollTimer !== null) clearTimeout(pollTimer)
})

// ---------------------------------------------------------------------------
// 展示
// ---------------------------------------------------------------------------

function formatDate(value: number | null): string {
  if (value === null) return '—'
  const text = String(value)
  return `${text.slice(0, 4)}-${text.slice(4, 6)}-${text.slice(6, 8)}`
}

function formatPercent(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  return `${(value * 100).toFixed(digits)}%`
}

function formatNumber(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  return value.toFixed(digits)
}

function formatMoney(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  return value.toLocaleString('zh-CN', { maximumFractionDigits: 0 })
}

/** 观察段天数。上线日那一行是接力点，不算观察日。 */
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
        <a class="nav-link" href="/swing-research">波段研究</a>
        <a class="nav-link active" href="/strategy-tracking" aria-current="page">策略追踪</a>
        <a class="nav-link" href="/factor-test">因子检验</a>
        <a class="nav-link" href="/history-compare">历史对比</a>
        <a class="nav-link" href="/instrument-lab">标的检验台</a>
        <a class="nav-link" href="/candidate-library">候选库</a>
        <a class="nav-link" href="/factor-attribution">因子归因</a>
      </nav>
      <div class="topbar-status"><span class="status-dot" aria-hidden="true"></span> 本地数据模式</div>
    </header>

    <main class="content-shell">
      <section class="page-intro">
        <div>
          <p class="eyebrow">OUT OF SAMPLE / DAILY</p>
          <h1>策略追踪</h1>
          <p class="intro-copy">把保存下来的策略沿着交易日历往前推，回答别的功能回答不了的那个问题：<strong>从今天起，它还灵不灵。</strong></p>
        </div>
        <div class="intro-note"><span class="note-label">研究方法</span><strong>规则上线即冻结</strong><small>要改规则请新开一个追踪，改了就不是样本外了</small></div>
      </section>

      <PageGuide page-key="strategy-tracking" />

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
          <span class="muted-code" :data-testid="`tracking-updated-${item.strategy_id}`">算到 {{ formatDate(item.last_tracked_date) }}</span>
        </div>

        <p v-if="item.last_error" class="notice error" :data-testid="`tracking-error-detail-${item.strategy_id}`">{{ item.last_error }}</p>

        <p v-if="item.observation_is_short" class="sample-warning" data-testid="tracking-short-warning">
          观察段还不到 20 个交易日，此时的年化和衰减比基本是噪声，先当参考，别当结论。
        </p>

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
          <p class="chart-summary">两条线都从上线日的资产出发，基准是同一批标的等权买入不动。</p>
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
      </section>
    </main>
  </div>
</template>
