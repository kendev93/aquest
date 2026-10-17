<script setup lang="ts">
import { computed, ref } from 'vue'

import type { StrategyPoolEntry, StrategyPoolObjective } from '../types'
import { formatPercent, formatSignedPercent } from '../utils/format'
import { objectiveLabel, type StrategyPoolSortDirection } from '../utils/strategyPoolStatus'

interface Props {
  entries: StrategyPoolEntry[]
  objective: StrategyPoolObjective
  loading?: boolean
  listContext?: string
}

const props = withDefaults(defineProps<Props>(), {
  loading: false,
  listContext: '',
})

/** 榜只画头部这么多行：排行榜的意义是比谁在前面，剩下的用「共 N 只可排」交代。 */
const BOARD_ROWS = 20

/**
 * 上线天数门槛由用户挑，不替他设默认阈值。
 *
 * 刚上线 1 天的 +3% 和跑了 200 天的 +3% 不是同一件事，全放进榜里前排一定是运气；
 * 「满 60 个交易日」这一档和后端「上线后衰减」判定用的窗口是同一个口径。
 */
const DAY_THRESHOLDS = [
  { days: 0, label: '全部（含刚上线）' },
  { days: 5, label: '上线满 5 个交易日' },
  { days: 20, label: '上线满 20 个交易日' },
  { days: 60, label: '上线满 60 个交易日' },
]

interface BoardRow {
  key: string
  market: 'SH' | 'SZ'
  code: string
  name: string
  postLaunchReturn: number
  benchmark: number | null
  excess: number | null
  createdReturn: number | null
  days: number
  asOfDate: string
  source: string
  failed: boolean
}

function finite(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

const direction = ref<StrategyPoolSortDirection>('desc')
const minDays = ref(0)

/**
 * 能进榜的只有「当前生效版本（手工覆盖优先）+ 创建之后确实有新行情」的标的。
 *
 * 没有新行情时后端给出的上线后收益是 0.0，那是「还没跑」而不是「平盘」，
 * 混进榜里会把一堆没数据的东西排成一条平线。
 */
const eligible = computed<BoardRow[]>(() => {
  const rows: BoardRow[] = []
  for (const entry of props.entries) {
    const value = finite(entry.post_launch_return)
    if (value === null || entry.post_launch_has_data === false) continue
    const benchmark = finite(entry.post_launch_benchmark_return)
    rows.push({
      key: `${entry.market}${entry.code}`,
      market: entry.market,
      code: entry.code,
      name: entry.name,
      postLaunchReturn: value,
      benchmark,
      excess: benchmark === null ? null : value - benchmark,
      createdReturn: finite(entry.created_return),
      days: finite(entry.post_launch_days) ?? 0,
      asOfDate: entry.tracking_as_of_date ?? '',
      source: entry.active_source === 'manual' ? '手工覆盖' : '自动最优',
      failed: entry.tracking_status === 'failed',
    })
  }
  return rows
})

const thresholdRows = computed(() => eligible.value.filter((row) => row.days >= minDays.value))

/** 并列先比上线天数（历史更长的更可信），再比代码，保证刷新前后是同一个顺序。 */
const sorted = computed<BoardRow[]>(() => [...thresholdRows.value].sort((left, right) => {
  const byReturn = direction.value === 'desc'
    ? right.postLaunchReturn - left.postLaunchReturn
    : left.postLaunchReturn - right.postLaunchReturn
  if (byReturn !== 0) return byReturn
  if (right.days !== left.days) return right.days - left.days
  return left.key.localeCompare(right.key)
}))

const rows = computed(() => sorted.value.slice(0, BOARD_ROWS))
const rankable = computed(() => thresholdRows.value.length)
const noVersion = computed(() => props.entries.filter((entry) => !entry.tracking).length)
const noWindow = computed(() => props.entries.filter(
  (entry) => entry.tracking && (entry.post_launch_has_data === false || finite(entry.post_launch_return) === null),
).length)
const hiddenByThreshold = computed(() => eligible.value.length - thresholdRows.value.length)

/** 行情截至日不一致时窗口长短就不同，必须点名，否则榜单在比不一样的东西。 */
const behindCount = computed(() => {
  const latest = rows.value.reduce((max, row) => (row.asOfDate > max ? row.asOfDate : max), '')
  return rows.value.filter((row) => row.asOfDate && row.asOfDate !== latest).length
})

function detailHref(row: BoardRow): string {
  return `/strategy-pool/${row.market}/${row.code}?objective=${props.objective}`
    + `&list=${encodeURIComponent(props.listContext)}`
}

function excessClass(value: number | null): string {
  if (value === null || value === 0) return ''
  return value > 0 ? 'positive' : 'negative'
}
</script>

<template>
  <section class="pool-board-section panel-card" data-testid="pool-post-launch-board">
    <div class="section-heading">
      <div><p class="eyebrow">POST-LAUNCH BOARD</p><h2>上线后收益榜 <span>{{ rankable.toLocaleString() }}</span></h2></div>
      <small>{{ objectiveLabel(objective) }}目标 · 各标的从自己这一版策略的上线日算起</small>
    </div>
    <div class="pool-board-controls">
      <div class="pool-board-tabs" role="group" aria-label="榜单排序方向">
        <button type="button" data-testid="pool-board-desc" :class="{ active: direction === 'desc' }" :aria-pressed="direction === 'desc'" @click="direction = 'desc'">收益最高在前</button>
        <button type="button" data-testid="pool-board-asc" :class="{ active: direction === 'asc' }" :aria-pressed="direction === 'asc'" @click="direction = 'asc'">收益最低在前</button>
      </div>
      <label class="pool-board-days">上线天数门槛
        <select v-model.number="minDays" data-testid="pool-board-min-days">
          <option v-for="threshold in DAY_THRESHOLDS" :key="threshold.days" :value="threshold.days">{{ threshold.label }}</option>
        </select>
      </label>
    </div>
    <p class="pool-scale-note" data-testid="pool-board-caliber">
      上线后收益从这一版策略的上线日算到它的行情截至日，和「创建至今收益」（从建仓基准日算，可能早于创建日）不是同一个窗口。
      同期买入持有是同一区间个股复权价的涨跌，超额 = 上线后收益 − 它。
      不进榜：还没有生效版本 {{ noVersion.toLocaleString() }} 只、创建后还没有新行情 {{ noWindow.toLocaleString() }} 只。
      <template v-if="hiddenByThreshold">另有 {{ hiddenByThreshold.toLocaleString() }} 只未满 {{ minDays }} 个交易日，被门槛挡住。</template>
      <template v-if="behindCount">榜内 {{ behindCount }} 只的行情截至日落后于其余标的（停牌或行情没更新），窗口更短。</template>
    </p>
    <p v-if="loading" class="empty-copy">正在读取策略池…</p>
    <p v-else-if="!rows.length" class="empty-copy" data-testid="pool-board-empty">
      还没有可排的标的。上线后收益要等模拟跟踪任务把各标的创建日之后的新行情跑出来才有；刚建好池子时这里为空是正常的。
    </p>
    <div v-else class="table-wrap">
      <table class="pool-board-table">
        <caption class="sr-only">全市场策略池上线后收益排行榜</caption>
        <thead>
          <tr>
            <th>#</th>
            <th>标的</th>
            <th>上线后收益</th>
            <th>同期买入持有</th>
            <th>超额</th>
            <th>创建至今收益</th>
            <th>上线交易日</th>
            <th>行情截至</th>
            <th>生效版本</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, index) in rows" :key="row.key" :data-testid="`pool-board-row-${row.key}`">
            <td>{{ index + 1 }}</td>
            <td><a class="table-link" :href="detailHref(row)">{{ row.market }} {{ row.code }} {{ row.name }}</a><small v-if="row.failed">跟踪更新失败，这里是最后一次成功结果</small></td>
            <td :class="excessClass(row.postLaunchReturn)" data-testid="pool-board-return-cell"><b>{{ formatPercent(row.postLaunchReturn) }}</b></td>
            <td>{{ formatPercent(row.benchmark) }}</td>
            <td :class="excessClass(row.excess)">{{ formatSignedPercent(row.excess) }}</td>
            <td>{{ formatPercent(row.createdReturn) }}</td>
            <td>{{ row.days }}</td>
            <td>{{ row.asOfDate || '—' }}</td>
            <td>{{ row.source }}</td>
          </tr>
        </tbody>
      </table>
      <p v-if="rankable > rows.length" class="pool-scale-note">榜长 {{ BOARD_ROWS }} 行，共 {{ rankable.toLocaleString() }} 只可排；用上方搜索框或列头排序在「全市场标的状态」里看完整名单。</p>
    </div>
  </section>
</template>

<style scoped>
.pool-board-controls { display: flex; flex-wrap: wrap; align-items: end; justify-content: space-between; gap: 12px; margin: 14px 0 10px; }
.pool-board-tabs { display: inline-flex; gap: 6px; }
.pool-board-tabs button { padding: 6px 12px; border: 1px solid #2f4258; border-radius: 6px; background: rgba(9, 17, 34, 0.6); color: #a9b8cd; font-size: 12px; cursor: pointer; }
.pool-board-tabs button.active { border-color: var(--accent); color: #eff4fc; background: var(--accent-soft); }
.pool-board-days { color: #8798b1; font-size: 12px; }
.pool-board-days select { margin-left: 6px; padding: 5px 8px; border: 1px solid #2f4258; border-radius: 6px; background: #0b1428; color: #eff4fc; font-size: 12px; }
.pool-board-table { width: 100%; min-width: 900px; border-collapse: collapse; text-align: left; }
.pool-board-table th, .pool-board-table td { padding: 10px; border-bottom: 1px solid #25364f; vertical-align: top; white-space: nowrap; }
.pool-board-table th { color: #8798b1; font-size: 12px; }
.pool-board-table td small { display: block; margin-top: 4px; color: #899ab3; white-space: normal; }
.table-link { color: var(--accent); text-decoration: none; font-size: 13px; }
.table-link:hover { text-decoration: underline; }
</style>
