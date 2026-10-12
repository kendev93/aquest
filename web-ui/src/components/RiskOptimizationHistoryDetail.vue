<script setup lang="ts">
import { computed, ref } from 'vue'

import type { StrategyDiscoveryCandidate, StrategyDiscoveryResult } from '../types'

const props = defineProps<{ result: StrategyDiscoveryResult }>()
const emit = defineEmits<{ close: [] }>()

const board = ref<'sharpe' | 'return'>('sharpe')
const candidates = computed(() => (
  board.value === 'sharpe' ? props.result.results_by_sharpe : props.result.results_by_return
))
const periods = [
  { key: 'train', label: '训练段' },
  { key: 'validation', label: '验证段' },
  { key: 'holdout', label: '留出段' },
] as const
type RiskMetricPeriod = (typeof periods)[number]['key']

function valueAt(...path: string[]): unknown {
  let value: unknown = props.result
  for (const key of path) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined
    value = (value as Record<string, unknown>)[key]
  }
  return value
}

function numberAt(...path: string[]): number | null {
  const value = valueAt(...path)
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function formatDuration(value: number | null): string {
  if (value === null || !Number.isFinite(value) || value < 0) return '—'
  if (value < 60) return `${value.toFixed(1)} 秒`
  const totalSeconds = Math.round(value)
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  if (minutes < 60) return seconds ? `${minutes} 分 ${seconds} 秒` : `${minutes} 分钟`
  const hours = Math.floor(minutes / 60)
  const remainingMinutes = minutes % 60
  return remainingMinutes ? `${hours} 小时 ${remainingMinutes} 分钟` : `${hours} 小时`
}

function formatNumber(value: number | null, digits = 2): string {
  return value === null ? '—' : value.toFixed(digits)
}

function formatRate(value: number | null): string {
  return value === null ? '—' : `${value.toFixed(2)} 组/秒`
}

function formatPercent(value: number | null): string {
  return value === null ? '—' : `${(value * 100).toFixed(2)}%`
}

function formatMoney(value: number | null): string {
  return value === null
    ? '—'
    : `¥${value.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

function formatBytes(value: number | null): string {
  if (value === null) return '—'
  if (value >= 1024 ** 3) return `${(value / 1024 ** 3).toFixed(2)} GB`
  if (value >= 1024 ** 2) return `${(value / 1024 ** 2).toFixed(1)} MB`
  return `${Math.round(value / 1024)} KB`
}

function snapshotCacheLabel(): string {
  const hit = valueAt('algorithm_metadata', 'snapshot_metrics', 'snapshot_cache_hit')
  return hit === true ? '命中' : hit === false ? '未命中' : '—'
}

function metric(
  candidate: StrategyDiscoveryCandidate,
  period: RiskMetricPeriod,
  key: string,
): number | null {
  const value = candidate.metrics[period]?.[key]
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function formatRatio(
  candidate: StrategyDiscoveryCandidate,
  period: RiskMetricPeriod,
  key: 'payoff_ratio' | 'profit_factor',
): string {
  const value = metric(candidate, period, key)
  if ((metric(candidate, period, 'total_trades') ?? 0) === 0) return '—'
  if (key === 'profit_factor' && value !== null && value >= 999) return '无亏损'
  if (key === 'payoff_ratio' && value === null) {
    const trades = metric(candidate, period, 'total_trades') ?? 0
    const losses = metric(candidate, period, 'lose_trades')
    return trades > 0 && losses === 0 ? '无亏损' : '—'
  }
  return formatNumber(value)
}

function formatAverageLoss(
  candidate: StrategyDiscoveryCandidate,
  period: RiskMetricPeriod,
): string {
  const value = metric(candidate, period, 'avg_loss')
  return value === null ? '—' : formatPercent(Math.abs(value))
}

function formatTurnover(
  candidate: StrategyDiscoveryCandidate,
  period: RiskMetricPeriod,
  key: 'buy_turnover' | 'sell_turnover' | 'turnover',
): string {
  const value = metric(candidate, period, key)
  return value === null ? '—' : `${value.toFixed(2)}×`
}
</script>

<template>
  <section class="risk-history-detail" data-testid="portfolio-risk-history-detail-panel">
    <div class="detail-heading">
      <div>
        <span class="section-kicker">RUN DETAIL</span>
        <h3>运行与候选详情</h3>
      </div>
      <button class="text-button" type="button" data-testid="portfolio-risk-history-detail-close" @click="emit('close')">关闭详情</button>
    </div>

    <div class="runtime-grid">
      <article>
        <span>总耗时</span>
        <strong>{{ formatDuration(props.result.duration_seconds ?? null) }}</strong>
      </article>
      <article>
        <span>准备数据（含指标计算）</span>
        <strong>{{ formatDuration(numberAt('stage_durations_seconds', 'snapshot')) }}</strong>
        <small>指标计算 {{ formatDuration(numberAt('stage_durations_seconds', 'indicators')) }}</small>
      </article>
      <article>
        <span>粗筛</span>
        <strong>{{ formatDuration(numberAt('stage_durations_seconds', 'coarse')) }}</strong>
        <small>{{ formatNumber(numberAt('search_metadata', 'fidelity_evaluations', 'coarse'), 0) }} 组 · {{ formatRate(numberAt('search_metadata', 'stage_throughput_per_second', 'coarse')) }}</small>
      </article>
      <article>
        <span>中筛</span>
        <strong>{{ formatDuration(numberAt('stage_durations_seconds', 'medium')) }}</strong>
        <small>{{ formatNumber(numberAt('search_metadata', 'fidelity_evaluations', 'medium'), 0) }} 组 · {{ formatRate(numberAt('search_metadata', 'stage_throughput_per_second', 'medium')) }}</small>
      </article>
      <article>
        <span>完整评估</span>
        <strong>{{ formatDuration(numberAt('stage_durations_seconds', 'full')) }}</strong>
        <small>{{ formatNumber(numberAt('search_metadata', 'fidelity_evaluations', 'full'), 0) }} 组 · {{ formatRate(numberAt('search_metadata', 'stage_throughput_per_second', 'full')) }}</small>
      </article>
      <article>
        <span>验证 / 留出</span>
        <strong>{{ formatDuration(numberAt('stage_durations_seconds', 'validation')) }} / {{ formatDuration(numberAt('stage_durations_seconds', 'holdout')) }}</strong>
        <small>候选 {{ formatNumber(numberAt('validation_metadata', 'validated_candidate_count'), 0) }} / {{ formatNumber(numberAt('validation_metadata', 'search_candidate_count'), 0) }} · 留出 {{ formatNumber(numberAt('validation_metadata', 'holdout_candidate_count'), 0) }}</small>
      </article>
      <article>
        <span>候选生成</span>
        <strong>{{ formatDuration(numberAt('stage_durations_seconds', 'candidate_generation')) }}</strong>
        <small>提案 {{ formatNumber(numberAt('search_metadata', 'proposal_attempts'), 0) }} · 重复 {{ formatNumber(numberAt('search_metadata', 'duplicate_trials'), 0) }} · 唯一率 {{ formatPercent(numberAt('search_metadata', 'unique_proposal_rate')) }}</small>
      </article>
      <article>
        <span>数据行数 / 快照大小</span>
        <strong>{{ formatNumber(numberAt('algorithm_metadata', 'snapshot_metrics', 'snapshot_rows'), 0) }} 行</strong>
        <small>{{ formatBytes(numberAt('algorithm_metadata', 'snapshot_metrics', 'snapshot_bytes')) }} · 快照{{ snapshotCacheLabel() }}</small>
      </article>
      <article>
        <span>指标缓存</span>
        <strong>{{ formatNumber(numberAt('algorithm_metadata', 'snapshot_metrics', 'indicator_cache_hits'), 0) }} 命中 / {{ formatNumber(numberAt('algorithm_metadata', 'snapshot_metrics', 'indicator_cache_misses'), 0) }} 未命中</strong>
        <small>计算 {{ formatNumber(numberAt('algorithm_metadata', 'snapshot_metrics', 'indicator_computed'), 0) }} 项</small>
      </article>
      <article>
        <span>并行评估</span>
        <strong>{{ valueAt('search_metadata', 'evaluation_mode') ?? '—' }} · {{ formatNumber(numberAt('search_metadata', 'evaluation_workers'), 0) }} workers</strong>
        <small>批大小 {{ formatNumber(numberAt('search_metadata', 'evaluation_batch_size'), 0) }}</small>
      </article>
    </div>
    <p class="detail-note">准备数据时间包含指标计算；粗筛是 search 阶段的一部分，不能与 search 耗时重复相加。盈亏比按卖出记录的平均盈利幅度 ÷ 平均亏损幅度计算；盈利因子按盈利总额 ÷ 亏损总额计算。买入是入场腿，已实现胜率与盈亏比按卖出记录统计，盈亏 ≤ 0 计为非盈利。买卖换手按各方向成交额 ÷ 初始资金计算，买卖成本包含佣金、印花税和滑点。更新前保存的历史候选没有盈亏比和买卖拆分字段时会显示“—”。</p>

    <div class="formula-mode-tabs" role="tablist" aria-label="历史寻优详情候选榜单">
      <button type="button" role="tab" :aria-selected="board === 'sharpe'" :class="{ active: board === 'sharpe' }" data-testid="portfolio-risk-history-detail-sharpe" @click="board = 'sharpe'">夏普榜</button>
      <button type="button" role="tab" :aria-selected="board === 'return'" :class="{ active: board === 'return' }" data-testid="portfolio-risk-history-detail-return" @click="board = 'return'">收益榜</button>
    </div>
    <div v-if="!candidates.length" class="inline-empty">此榜单没有候选。</div>
    <div v-else class="candidate-list">
      <details v-for="(candidate, index) in candidates" :key="candidate.candidate_hash" class="candidate-detail">
        <summary>
          <strong>#{{ index + 1 }} · {{ candidate.human_readable_rules }}</strong>
          <small>训练 Sharpe {{ formatNumber(metric(candidate, 'train', 'sharpe')) }} / 盈亏比 {{ formatRatio(candidate, 'train', 'payoff_ratio') }} · 验证 Sharpe {{ formatNumber(metric(candidate, 'validation', 'sharpe')) }} / 盈亏比 {{ formatRatio(candidate, 'validation', 'payoff_ratio') }} · 留出 Sharpe {{ formatNumber(metric(candidate, 'holdout', 'sharpe')) }} / 盈亏比 {{ formatRatio(candidate, 'holdout', 'payoff_ratio') }}</small>
        </summary>
        <div class="candidate-periods">
          <section v-for="period in periods" :key="period.key">
            <h4>{{ period.label }}</h4>
            <div class="candidate-metrics">
              <div><span>总收益</span><strong>{{ formatPercent(metric(candidate, period.key, 'total_return')) }}</strong></div>
              <div><span>年化收益</span><strong>{{ formatPercent(metric(candidate, period.key, 'annual_return')) }}</strong></div>
              <div><span>Sharpe</span><strong>{{ formatNumber(metric(candidate, period.key, 'sharpe')) }}</strong></div>
              <div><span>Sortino / Calmar</span><strong>{{ formatNumber(metric(candidate, period.key, 'sortino')) }} / {{ formatNumber(metric(candidate, period.key, 'calmar')) }}</strong></div>
              <div><span>年化波动率</span><strong>{{ formatPercent(metric(candidate, period.key, 'volatility')) }}</strong></div>
              <div><span>最大回撤</span><strong>{{ formatPercent(metric(candidate, period.key, 'max_drawdown')) }}</strong></div>
              <div><span>最大回撤持续</span><strong>{{ formatNumber(metric(candidate, period.key, 'max_dd_duration'), 0) }} 个交易日</strong></div>
              <div><span>胜率</span><strong>{{ formatPercent(metric(candidate, period.key, 'win_rate')) }}</strong></div>
              <div><span>盈亏比</span><strong>{{ formatRatio(candidate, period.key, 'payoff_ratio') }}</strong></div>
              <div><span>盈利因子</span><strong>{{ formatRatio(candidate, period.key, 'profit_factor') }}</strong></div>
              <div><span>卖出记录 / 盈利 / 非盈利</span><strong>{{ formatNumber(metric(candidate, period.key, 'total_trades'), 0) }} / {{ formatNumber(metric(candidate, period.key, 'win_trades'), 0) }} / {{ formatNumber(metric(candidate, period.key, 'lose_trades'), 0) }}</strong></div>
              <div><span>平均盈利 / 亏损</span><strong>{{ formatPercent(metric(candidate, period.key, 'avg_win')) }} / {{ formatAverageLoss(candidate, period.key) }}</strong></div>
              <div><span>最大单笔盈利 / 亏损</span><strong>{{ formatPercent(metric(candidate, period.key, 'max_win')) }} / {{ formatPercent(metric(candidate, period.key, 'max_loss') === null ? null : Math.abs(metric(candidate, period.key, 'max_loss') ?? 0)) }}</strong></div>
              <div><span>平均持仓天数</span><strong>{{ formatNumber(metric(candidate, period.key, 'avg_holding_days'), 1) }}</strong></div>
              <div><span>持仓天数 盈利 / 亏损</span><strong>{{ formatNumber(metric(candidate, period.key, 'win_holding_days'), 1) }} / {{ formatNumber(metric(candidate, period.key, 'loss_holding_days'), 1) }}</strong></div>
              <div><span>最长单笔持仓</span><strong>{{ formatNumber(metric(candidate, period.key, 'max_holding_days'), 1) }}</strong></div>
              <div><span>买入 / 卖出成交单</span><strong>{{ formatNumber(metric(candidate, period.key, 'executed_buy_orders'), 0) }} / {{ formatNumber(metric(candidate, period.key, 'executed_sell_orders'), 0) }}</strong></div>
              <div><span>买入 / 卖出换手</span><strong>{{ formatTurnover(candidate, period.key, 'buy_turnover') }} / {{ formatTurnover(candidate, period.key, 'sell_turnover') }}</strong></div>
              <div><span>总换手</span><strong>{{ formatTurnover(candidate, period.key, 'turnover') }}</strong></div>
              <div><span>买入 / 卖出成本</span><strong>{{ formatMoney(metric(candidate, period.key, 'buy_transaction_costs')) }} / {{ formatMoney(metric(candidate, period.key, 'sell_transaction_costs')) }}</strong></div>
              <div><span>拒绝买单 / 卖单</span><strong>{{ formatNumber(metric(candidate, period.key, 'rejected_buy_orders'), 0) }} / {{ formatNumber(metric(candidate, period.key, 'rejected_sell_orders'), 0) }}</strong></div>
            </div>
          </section>
        </div>
      </details>
    </div>
  </section>
</template>

<style scoped>
.risk-history-detail {
  margin-top: 16px;
  padding: 14px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: rgba(8, 15, 32, 0.5);
}

.detail-heading,
.candidate-detail summary {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.detail-heading h3 {
  margin: 4px 0 0;
  color: var(--text);
  font-size: 14px;
}

.runtime-grid,
.candidate-metrics {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(145px, 1fr));
  gap: 8px;
  margin-top: 12px;
}

.runtime-grid article,
.candidate-metrics > div {
  display: flex;
  min-height: 52px;
  flex-direction: column;
  justify-content: center;
  gap: 4px;
  padding: 8px;
  border: 1px solid var(--border);
  background: var(--surface);
}

.runtime-grid span,
.candidate-metrics span {
  color: var(--subtle);
  font-size: 9px;
}

.runtime-grid strong,
.candidate-metrics strong {
  color: var(--text);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 10px;
}

.runtime-grid small,
.candidate-detail summary small {
  color: var(--muted);
  font-size: 9px;
}

.detail-note {
  margin: 10px 0;
  color: var(--subtle);
  font-size: 10px;
  line-height: 1.5;
}

.candidate-list {
  display: grid;
  gap: 8px;
  margin-top: 12px;
}

.candidate-detail {
  padding: 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: rgba(8, 15, 32, 0.45);
}

.candidate-detail summary {
  cursor: pointer;
  flex-wrap: wrap;
}

.candidate-detail summary strong {
  color: var(--text);
  font-size: 10px;
}

.candidate-periods {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
  margin-top: 12px;
}

.candidate-periods section h4 {
  margin: 0;
  color: var(--muted);
  font-size: 10px;
}

@media (max-width: 860px) {
  .candidate-periods {
    grid-template-columns: 1fr;
  }
}

@media (min-width: 861px) and (max-width: 1200px) {
  .candidate-periods {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
