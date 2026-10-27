<script setup lang="ts">
import { computed } from 'vue'

import StrategyPoolFollowButton from './StrategyPoolFollowButton.vue'
import type {
  StrategyPoolEntry,
  StrategyPoolFollowTarget,
  StrategyPoolObjective,
  StrategyPoolSignal,
} from '../types'
import { formatNumber, formatPercent, formatSignedPercent } from '../utils/format'
import {
  benchmarkReturn,
  fullPeriodAsOf,
  entryHoldingRisk,
  entryIsHeld,
  entryLatestClose,
  entryStatus,
  formatHoldingDays,
  holdingRiskSummary,
  metricValue,
  objectiveLabel,
  rerunEntryDisabled,
  rerunEntryTitle,
  statusLabel,
  symbol,
  tradeCount,
  type StrategyPoolMetricColumn,
  type StrategyPoolSortColumn,
  type StrategyPoolSortDirection,
} from '../utils/strategyPoolStatus'

interface Props {
  entries: StrategyPoolEntry[]
  objective: StrategyPoolObjective | null
  states: StrategyPoolSignal[]
  heldKeys: Set<string>
  followKeys: Set<string>
  testid: string
  emptyText?: string
  sortable?: boolean
  sortColumn?: StrategyPoolSortColumn
  sortDirection?: StrategyPoolSortDirection
  queuedSymbol?: string
  busy?: boolean
  busyIntent?: string
  listContext?: string
  followBusy?: boolean
}

const props = withDefaults(defineProps<Props>(), {
  sortable: true,
  sortColumn: 'status',
  sortDirection: 'desc',
  queuedSymbol: '',
  busy: false,
  busyIntent: '',
  listContext: '',
  emptyText: '',
  followBusy: false,
})

const emit = defineEmits<{
  sort: [column: StrategyPoolMetricColumn]
  rerun: [entry: StrategyPoolEntry]
  toggleFollow: [target: StrategyPoolFollowTarget]
}>()

// 表格指标列：列头和单元格口径一致，'status' 档不进这里（没有可点的排序按钮）。
const METRIC_COLUMNS: { key: StrategyPoolMetricColumn; label: string }[] = [
  { key: 'benchmark_return', label: '基准涨跌' },
  { key: 'created_return', label: '创建至今收益' },
  { key: 'strategy_return', label: '策略收益' },
  { key: 'annual_return', label: '年化' },
  { key: 'sharpe', label: '夏普' },
  { key: 'win_rate', label: '胜率' },
  { key: 'payoff_ratio', label: '盈亏比' },
  { key: 'max_drawdown', label: '最大回撤' },
  { key: 'avg_holding_days', label: '平均持仓' },
]

const jobContext = computed(() => ({
  queuedSymbol: props.queuedSymbol,
  busy: props.busy,
  busyIntent: props.busyIntent,
}))

// 列头一次点击只上报「点了这一列」，高→低 → 低→高 → 取消排序的循环由父组件持有，
// 这里只按它给的方向画箭头，并在提示里说清下一次点击会排什么。
function sortGlyph(column: StrategyPoolMetricColumn): string {
  if (props.sortColumn !== column) return ''
  return props.sortDirection === 'asc' ? ' ↑' : ' ↓'
}

function sortHint(column: StrategyPoolMetricColumn, label: string): string {
  if (props.sortColumn !== column) {
    return `点击按${label}从高到低排序；再点改为低→高，第三次取消排序`
  }
  return props.sortDirection === 'desc'
    ? `当前按${label}从高到低排序；点击改为低→高，再点取消排序回到按状态分组`
    : `当前按${label}从低到高排序；点击取消排序，回到按状态分组`
}

function detailHref(entry: StrategyPoolEntry): string {
  return `/strategy-pool/${entry.market}/${entry.code}?objective=${props.objective ?? 'annual_return'}`
    + `&list=${encodeURIComponent(props.listContext)}`
}

// 买卖同时触发时拆成两个徽章：持仓行再加「持仓中」，三个状态就能并列看到。
function entryStatusChips(entry: StrategyPoolEntry): string[] {
  const status = entryStatus(entry, props.states)
  return status === 'buy_sell' ? ['buy', 'sell'] : [status]
}

/** 持仓中标的风控条；没持仓或还没有跟踪快照时为空，不显示占位符。 */
function holdingRiskText(entry: StrategyPoolEntry): string {
  const risk = entryHoldingRisk(entry)
  return risk ? holdingRiskSummary(risk) : ''
}

/** 最新收盘价：直接从行情取，每一行都有，与有没有持仓无关。 */
function latestCloseText(entry: StrategyPoolEntry): string {
  const close = entryLatestClose(entry)
  return close ? formatNumber(close.price, 2) : '—'
}

/**
 * 只有当价和风控线的判定日期不是同一天时才补一句日期。
 *
 * 一致时全表已经有「截至 X」那行，重复写两遍同样的日期只是噪声；不一致说明跟踪落后于
 * 行情（停牌或落库失败），这时必须让人看见价是哪一天的，不然会拿今天的价去比昨天的线。
 */
function latestCloseAsOf(entry: StrategyPoolEntry): string {
  const close = entryLatestClose(entry)
  if (!close?.asOf || close.asOf === entry.tracking_as_of_date) return ''
  return close.asOf
}
</script>

<template>
  <div class="table-scroll">
    <table class="pool-table">
      <thead><tr>
        <th>持仓 / 今日信号</th><th>证券</th><th title="最后一根已完成日线的收盘价（不复权），每行都有；日期与风控线判定日期不一致时会在价下面标出来">最新收盘</th>
        <th v-for="column in METRIC_COLUMNS" :key="column.key">
          <button v-if="sortable" :data-testid="`${testid}-sort-${column.key}`" :title="sortHint(column.key, column.label)" @click="emit('sort', column.key)">{{ column.label }}{{ sortGlyph(column.key) }}</button>
          <template v-else>{{ column.label }}</template>
        </th>
        <th>策略 / 风险标记</th><th>维护</th>
      </tr></thead>
      <tbody>
        <tr v-for="entry in entries" :key="symbol(entry)" :data-testid="`${testid}-row-${entry.code}`">
          <td><div class="status-stack"><span v-if="entryIsHeld(entry, heldKeys)" class="status-chip status-held">持仓中</span><span v-for="chip in entryStatusChips(entry)" :key="chip" class="status-chip" :class="`status-${chip}`">{{ statusLabel(chip) }}</span></div><small v-if="holdingRiskText(entry)" :data-testid="`${testid}-holding-risk-${entry.code}`">{{ holdingRiskText(entry) }}</small></td>
          <td><strong>{{ entry.market }} {{ entry.code }}</strong><small>{{ entry.name }}</small></td>
          <td :data-testid="`${testid}-latest-close-${entry.code}`">{{ latestCloseText(entry) }}<small v-if="latestCloseAsOf(entry)">价截至 {{ latestCloseAsOf(entry) }}</small></td>
          <td :data-testid="`${testid}-entry-benchmark-${entry.code}`">
            <div v-if="entry.benchmark" class="benchmark-summary">
              <div>全段基准 <b>{{ formatSignedPercent(benchmarkReturn(entry)) }}</b></div>
              <small v-if="fullPeriodAsOf(entry)">截至 {{ fullPeriodAsOf(entry) }}</small>
            </div>
            <span v-else class="empty-copy">下次全市场重选时补算</span>
          </td>
          <td :data-testid="`${testid}-created-return-${entry.code}`">{{ typeof entry.created_return === 'number' ? formatPercent(entry.created_return) : entry.tracking_status === 'pending' ? '待初始化' : entry.tracking_status === 'failed' ? '更新失败' : '—' }}<small v-if="entry.tracking_status === 'failed'">同步失败，保留上次结果</small><small v-if="entry.tracking_as_of_date">截至 {{ entry.tracking_as_of_date }}</small></td>
          <td :data-testid="`${testid}-strategy-return-${entry.code}`">{{ formatPercent(metricValue(entry, 'strategy_return')) }}<small v-if="fullPeriodAsOf(entry)">截至 {{ fullPeriodAsOf(entry) }}</small></td>
          <td>{{ formatPercent(metricValue(entry, 'annual_return')) }}</td>
          <td>{{ formatNumber(metricValue(entry, 'sharpe')) }}</td>
          <td>{{ formatPercent(metricValue(entry, 'win_rate')) }}</td>
          <td>{{ formatNumber(metricValue(entry, 'payoff_ratio')) }}</td>
          <td>{{ formatPercent(metricValue(entry, 'max_drawdown')) }}</td>
          <td :data-testid="`${testid}-holding-days-${entry.code}`">{{ formatHoldingDays(metricValue(entry, 'avg_holding_days')) }}</td>
          <td><span v-if="entry.active_candidate?.quality?.badges?.length" class="badge-row"><i v-for="badge in entry.active_candidate.quality.badges" :key="badge">{{ badge }}</i></span><span v-else-if="entry.status === 'no_strategy'">无可计算的策略绩效</span><span v-else-if="entry.last_error" class="danger-text" :data-testid="`${testid}-entry-error-${entry.code}`" :title="entry.last_error">策略搜索失败 · 悬停查看原因</span><span v-else>—</span><small v-if="entry.active_candidate?.metrics">交易数：验证 {{ formatNumber(tradeCount(entry.active_candidate, 'validation'), 0) }} · 留出 {{ formatNumber(tradeCount(entry.active_candidate, 'holdout'), 0) }} 笔</small><small v-if="entry.active_source === 'manual'">手工覆盖 · {{ objectiveLabel(objective) }}</small></td>
          <td><a class="text-button" :href="detailHref(entry)" :data-testid="`${testid}-detail-${entry.code}`">详情</a><button :data-testid="`${testid}-rerun-entry-${entry.code}`" class="text-button" :disabled="rerunEntryDisabled(jobContext)" :title="rerunEntryTitle(entry, jobContext)" @click="emit('rerun', entry)">{{ queuedSymbol === symbol(entry) ? '排队中' : '重跑' }}</button><StrategyPoolFollowButton :target="entry" :follow-keys="followKeys" :busy="followBusy" :test-id="`${testid}-follow-entry-${entry.code}`" @toggle="emit('toggleFollow', $event)" /></td>
        </tr>
        <tr v-if="!entries.length"><td colspan="13" class="empty-copy" :data-testid="`${testid}-entry-empty`">{{ emptyText }}</td></tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.table-scroll { overflow: auto; }
.pool-table { width: 100%; min-width: 1060px; border-collapse: collapse; text-align: left; }
.pool-table th, .pool-table td { border-bottom: 1px solid #25364f; padding: 12px 10px; vertical-align: top; }
.pool-table th { color: #8798b1; font-size: .88rem; white-space: nowrap; }
.pool-table th button { border: 0; padding: 0; background: none; color: inherit; font: inherit; cursor: pointer; }
.pool-table td small { display: block; margin-top: 5px; color: #899ab3; }
.empty-copy { color: #8798b1; font-size: .9rem; line-height: 1.6; }
.benchmark-summary { display: grid; min-width: 250px; gap: 4px; color: #a8b8cc; font-size: .78rem; line-height: 1.45; white-space: nowrap; }
.benchmark-summary b { color: #eff4fc; }
.status-chip { display: inline-block; padding: 4px 8px; border: 1px solid #42516a; border-radius: 999px; color: #a9b5c7; font-size: .78rem; white-space: nowrap; }
.status-stack { display: flex; flex-wrap: wrap; gap: 5px; }
.status-buy { border-color: #128f4c; color: #25d475; }
.status-sell, .status-error { border-color: #974d50; color: #ff7775; }
.status-held { border-color: #8b7b3c; color: #e0c55b; }
.badge-row { display: flex; flex-wrap: wrap; gap: 5px; }
.badge-row i { padding: 3px 6px; border-radius: 4px; background: #263348; color: #b9c7d9; font-size: .75rem; font-style: normal; }
.text-button { border: 0; padding: 4px 7px; background: none; color: #94a9c4; font: inherit; cursor: pointer; }
.danger-text { color: #ff7775; }
</style>
