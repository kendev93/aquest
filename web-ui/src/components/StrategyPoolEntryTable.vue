<script setup lang="ts">
import { computed } from 'vue'

import type { StrategyPoolEntry, StrategyPoolObjective, StrategyPoolSignal } from '../types'
import { formatNumber, formatPercent, formatSignedPercent } from '../utils/format'
import {
  benchmarkReturn,
  entryIsHeld,
  entryStatus,
  formatHoldingDays,
  metricValue,
  objectiveLabel,
  rerunEntryDisabled,
  rerunEntryTitle,
  statusLabel,
  symbol,
  tradeCount,
  type StrategyPoolMetricColumn,
  type StrategyPoolSortColumn,
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
  queuedSymbol?: string
  busy?: boolean
  busyIntent?: string
  listContext?: string
  followBusy?: boolean
}

const props = withDefaults(defineProps<Props>(), {
  sortable: true,
  sortColumn: 'status',
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
  toggleFollow: [entry: StrategyPoolEntry]
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

function detailHref(entry: StrategyPoolEntry): string {
  return `/strategy-pool/${entry.market}/${entry.code}?objective=${props.objective ?? 'annual_return'}`
    + `&list=${encodeURIComponent(props.listContext)}`
}

function followLabel(entry: StrategyPoolEntry): string {
  return props.followKeys.has(symbol(entry)) ? '已关注' : '关注'
}

function followTitle(entry: StrategyPoolEntry): string {
  return props.followKeys.has(symbol(entry))
    ? `取消关注 ${entry.market} ${entry.code}`
    : `关注 ${entry.market} ${entry.code}，加入特别关注面板`
}
</script>

<template>
  <div class="table-scroll">
    <table class="pool-table">
      <thead><tr>
        <th>持仓 / 今日信号</th><th>证券</th>
        <th v-for="column in METRIC_COLUMNS" :key="column.key">
          <button v-if="sortable" @click="emit('sort', column.key)">{{ column.label }}{{ sortColumn === column.key ? ' ↓' : '' }}</button>
          <template v-else>{{ column.label }}</template>
        </th>
        <th>策略 / 风险标记</th><th>维护</th>
      </tr></thead>
      <tbody>
        <tr v-for="entry in entries" :key="symbol(entry)" :data-testid="`${testid}-row-${entry.code}`">
          <td><div class="status-stack"><span v-if="entryIsHeld(entry, heldKeys)" class="status-chip status-held">持仓中</span><span class="status-chip" :class="`status-${entryStatus(entry, states)}`">{{ statusLabel(entryStatus(entry, states)) }}</span></div></td>
          <td><strong>{{ entry.market }} {{ entry.code }}</strong><small>{{ entry.name }}</small></td>
          <td :data-testid="`${testid}-entry-benchmark-${entry.code}`">
            <div v-if="entry.benchmark" class="benchmark-summary">
              <div>全段基准 <b>{{ formatSignedPercent(benchmarkReturn(entry)) }}</b></div>
            </div>
            <span v-else class="empty-copy">下次全市场重选时补算</span>
          </td>
          <td :data-testid="`${testid}-created-return-${entry.code}`">{{ typeof entry.created_return === 'number' ? formatPercent(entry.created_return) : entry.tracking_status === 'pending' ? '待初始化' : entry.tracking_status === 'failed' ? '更新失败' : '—' }}<small v-if="entry.tracking_status === 'failed'">同步失败，保留上次结果</small><small v-if="entry.tracking_as_of_date">截至 {{ entry.tracking_as_of_date }}</small></td>
          <td>{{ formatPercent(metricValue(entry, 'strategy_return', objective)) }}</td>
          <td>{{ formatPercent(metricValue(entry, 'annual_return', objective)) }}</td>
          <td>{{ formatNumber(metricValue(entry, 'sharpe', objective)) }}</td>
          <td>{{ formatPercent(metricValue(entry, 'win_rate', objective)) }}</td>
          <td>{{ formatNumber(metricValue(entry, 'payoff_ratio', objective)) }}</td>
          <td>{{ formatPercent(metricValue(entry, 'max_drawdown', objective)) }}</td>
          <td :data-testid="`${testid}-holding-days-${entry.code}`">{{ formatHoldingDays(metricValue(entry, 'avg_holding_days', objective)) }}</td>
          <td><span v-if="entry.active_candidate?.quality?.badges?.length" class="badge-row"><i v-for="badge in entry.active_candidate.quality.badges" :key="badge">{{ badge }}</i></span><span v-else-if="entry.status === 'no_strategy'">无可计算的策略绩效</span><span v-else-if="entry.last_error" class="danger-text">{{ entry.last_error }}</span><span v-else>—</span><small v-if="entry.active_candidate?.metrics">交易数：验证 {{ formatNumber(tradeCount(entry.active_candidate, 'validation'), 0) }} · 留出 {{ formatNumber(tradeCount(entry.active_candidate, 'holdout'), 0) }} 笔</small><small v-if="entry.active_source === 'manual'">手工覆盖 · {{ objectiveLabel(objective) }}</small></td>
          <td><a class="text-button" :href="detailHref(entry)" :data-testid="`${testid}-detail-${entry.code}`">详情</a><button :data-testid="`${testid}-rerun-entry-${entry.code}`" class="text-button" :disabled="rerunEntryDisabled(jobContext)" :title="rerunEntryTitle(entry, jobContext)" @click="emit('rerun', entry)">{{ queuedSymbol === symbol(entry) ? '排队中' : '重跑' }}</button><button :data-testid="`${testid}-follow-entry-${entry.code}`" class="text-button" :disabled="followBusy" :title="followTitle(entry)" @click="emit('toggleFollow', entry)">{{ followLabel(entry) }}</button></td>
        </tr>
        <tr v-if="!entries.length"><td colspan="13" class="empty-copy" :data-testid="`${testid}-entry-empty`">{{ emptyText }}</td></tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.table-scroll { overflow: auto; }
.pool-table { width: 100%; min-width: 980px; border-collapse: collapse; text-align: left; }
.pool-table th, .pool-table td { border-bottom: 1px solid #25364f; padding: 12px 10px; vertical-align: top; }
.pool-table th { color: #8798b1; font-size: .88rem; white-space: nowrap; }
.pool-table th button { border: 0; padding: 0; background: none; color: inherit; font: inherit; cursor: pointer; }
.pool-table td small { display: block; margin-top: 5px; color: #899ab3; }
.empty-copy { color: #8798b1; font-size: .9rem; line-height: 1.6; }
.benchmark-summary { display: grid; min-width: 250px; gap: 4px; color: #a8b8cc; font-size: .78rem; line-height: 1.45; white-space: nowrap; }
.benchmark-summary b { color: #eff4fc; }
.status-chip { display: inline-block; padding: 4px 8px; border: 1px solid #42516a; border-radius: 999px; color: #a9b5c7; font-size: .78rem; white-space: nowrap; }
.status-stack { display: flex; flex-wrap: wrap; gap: 5px; }
.status-buy, .status-buy_sell { border-color: #128f4c; color: #25d475; }
.status-sell, .status-error { border-color: #974d50; color: #ff7775; }
.status-held { border-color: #8b7b3c; color: #e0c55b; }
.badge-row { display: flex; flex-wrap: wrap; gap: 5px; }
.badge-row i { padding: 3px 6px; border-radius: 4px; background: #263348; color: #b9c7d9; font-size: .75rem; font-style: normal; }
.text-button { border: 0; padding: 4px 7px; background: none; color: #94a9c4; font: inherit; cursor: pointer; }
.danger-text { color: #ff7775; }
</style>
