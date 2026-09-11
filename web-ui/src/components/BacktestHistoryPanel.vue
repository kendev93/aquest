<script setup lang="ts">
import type { BacktestHistorySummary } from '../types'

interface Props {
  items: BacktestHistorySummary[]
  loading?: boolean
  testid: string
  title: string
  emptyText: string
}

const props = withDefaults(defineProps<Props>(), {
  loading: false,
})

const emit = defineEmits<{
  refresh: []
  load: [historyId: string]
  remove: [historyId: string]
}>()

function formatTimestamp(value: string): string {
  return value.replace('T', ' ').replace(/\.\d+/, '').replace('Z', '')
}

function formatDuration(value: number): string {
  if (!Number.isFinite(value) || value < 0) return '—'
  if (value < 60) return `${value.toFixed(1)} 秒`
  const totalSeconds = Math.round(value)
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  if (minutes < 60) return seconds ? `${minutes} 分 ${seconds} 秒` : `${minutes} 分钟`
  const hours = Math.floor(minutes / 60)
  const remainingMinutes = minutes % 60
  return remainingMinutes ? `${hours} 小时 ${remainingMinutes} 分钟` : `${hours} 小时`
}

function summaryLabel(item: BacktestHistorySummary): string {
  const summary = item.summary
  if (typeof summary.market === 'string' && typeof summary.code === 'string') {
    return typeof summary.name === 'string' && summary.name
      ? `${summary.name}（${summary.market} ${summary.code}）`
      : `${summary.market} ${summary.code}`
  }
  if (Array.isArray(summary.instrument_names)) {
    const names = summary.instrument_names
      .map((item) => {
        if (!item || typeof item !== 'object') return null
        const value = item as Record<string, unknown>
        if (typeof value.name !== 'string' || !value.name) return null
        if (typeof value.market === 'string' && typeof value.code === 'string') {
          return `${value.name}（${value.market} ${value.code}）`
        }
        return value.name
      })
      .filter((name): name is string => name !== null)
    if (names.length) {
      const prefix = typeof summary.universe === 'string' ? summary.universe.toUpperCase() : item.backtest_type
      return `${prefix} · ${names.slice(0, 3).join('、')}${names.length > 3 ? ' 等' : ''}`
    }
  }
  if (typeof summary.universe === 'string') return summary.universe.toUpperCase()
  return item.backtest_type
}

function dateRange(item: BacktestHistorySummary): string {
  const start = item.summary.start_date
  const end = item.summary.end_date
  if (typeof start === 'string' && typeof end === 'string') return `${start} → ${end}`
  return '区间未记录'
}
</script>

<template>
  <section class="panel backtest-history-panel" :data-testid="props.testid">
    <div class="panel-heading">
      <div><span class="section-kicker">HISTORY</span><h2>{{ props.title }}</h2></div>
      <button class="secondary-button" type="button" :disabled="props.loading" @click="emit('refresh')">{{ props.loading ? '读取中…' : '刷新' }}</button>
    </div>
    <div v-if="props.loading && !props.items.length" class="inline-empty">正在读取历史记录…</div>
    <div v-else-if="!props.items.length" class="inline-empty">{{ props.emptyText }}</div>
    <div v-else class="table-wrap">
      <table>
        <caption class="sr-only">{{ props.title }}</caption>
        <thead><tr><th>实验/回测</th><th>数据区间</th><th>开始时间</th><th>结束时间</th><th>耗时</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="item in props.items" :key="item.history_id">
            <td><strong class="code">{{ summaryLabel(item) }}</strong><small class="history-id">{{ item.history_id }}</small></td>
            <td class="muted-code">{{ dateRange(item) }}</td>
            <td class="muted-code">{{ formatTimestamp(item.started_at) }}</td>
            <td class="muted-code">{{ formatTimestamp(item.finished_at) }}</td>
            <td>{{ formatDuration(item.duration_seconds) }}</td>
            <td class="history-actions"><button class="table-link" type="button" :data-testid="`${props.testid}-load-${item.history_id}`" @click="emit('load', item.history_id)">加载</button><button class="table-link danger-link" type="button" :data-testid="`${props.testid}-remove-${item.history_id}`" @click="emit('remove', item.history_id)">删除</button></td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>
