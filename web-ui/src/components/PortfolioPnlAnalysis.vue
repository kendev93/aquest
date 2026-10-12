<script setup lang="ts">
/**
 * 组合回测结果页的「盈亏分析」面板。
 *
 * 数据在前端现算：结果里的成交明细本来就在内存里，用户每次改参数重跑后
 * 打开看到的就是这次的口径，不必再等一次请求。计算逻辑集中在
 * `utils/portfolioPnlAnalysis.ts`，与 `scripts/analyze_portfolio_losses.py` 同口径。
 */
import { computed } from 'vue'

import type { PortfolioBacktestResult } from '../types'
import { analyzePortfolioPnl } from '../utils/portfolioPnlAnalysis'


const props = withDefaults(
  defineProps<{
    result: PortfolioBacktestResult | null
    testid?: string
  }>(),
  { testid: 'portfolio-pnl-analysis' },
)

const analysis = computed(() => analyzePortfolioPnl(props.result))

/** 长套（持有 60 日以上）贡献的亏损占比——这次回测最该看的一行数。 */
const longHold = computed(() => {
  const rows = analysis.value.byHolding.filter(
    (row) => row.label === '61–120 日' || row.label === '>120 日',
  )
  const loss = rows.reduce((sum, row) => sum + row.loss, 0)
  const count = rows.reduce((sum, row) => sum + row.count, 0)
  const total = analysis.value.overview.grossLoss
  return { loss, count, share: total > 0 ? loss / total : 0 }
})

const costShare = computed(() => {
  const total = analysis.value.overview.grossLoss
  return total > 0 ? analysis.value.overview.costs / total : 0
})

function money(value: number | null | undefined): string {
  return value === null || value === undefined
    ? '—'
    : `¥${value.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

function percent(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : `${(value * 100).toFixed(1)}%`
}

function integer(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : value.toLocaleString('zh-CN')
}

function ratio(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : value.toFixed(2)
}
</script>

<template>
  <section class="data-section pnl-analysis" :data-testid="testid" aria-labelledby="pnl-analysis-title">
    <div class="section-heading">
      <div><span class="section-kicker">PNL BREAKDOWN</span><h3 id="pnl-analysis-title">盈亏分析</h3></div>
      <span>按平仓配对统计，已扣费税</span>
    </div>

    <p v-if="!analysis.overview.trips" class="inline-empty">这次回测没有形成可配对的平仓。</p>

    <template v-else>
      <div class="metrics-strip pnl-metrics">
        <div><span>平仓段数</span><strong :data-testid="`${testid}-trips`">{{ integer(analysis.overview.trips) }}</strong></div>
        <div><span>盈利 / 亏损</span><strong>{{ integer(analysis.overview.winners) }} / {{ integer(analysis.overview.losers) }}</strong></div>
        <div><span>盈利总额</span><strong class="positive">{{ money(analysis.overview.grossWin) }}</strong></div>
        <div><span>亏损总额</span><strong class="negative" :data-testid="`${testid}-gross-loss`">{{ money(-analysis.overview.grossLoss) }}</strong></div>
        <div><span>盈亏比</span><strong>{{ ratio(analysis.overview.profitFactor) }}</strong></div>
        <div><span>平均盈利 / 平均亏损</span><strong>{{ money(analysis.overview.avgWin) }} / {{ money(analysis.overview.avgLoss) }}</strong></div>
        <div><span>最深单笔</span><strong class="negative">{{ money(analysis.overview.worstPnl) }}</strong></div>
        <div><span>交易成本占亏损</span><strong>{{ percent(costShare) }}</strong></div>
      </div>

      <p v-if="longHold.count" class="pnl-note" :data-testid="`${testid}-long-hold`">
        持有超过 60 日的 {{ longHold.count }} 段平仓贡献了 {{ percent(longHold.share) }} 的亏损额（{{ money(-longHold.loss) }}）。
      </p>

      <div class="pnl-grid">
        <div class="pnl-block">
          <h4>亏损发生在哪一年</h4>
          <div class="table-wrap">
            <table :data-testid="`${testid}-by-year`">
              <caption class="sr-only">按平仓年份统计盈亏</caption>
              <thead><tr><th>年份</th><th>平仓</th><th>亏损</th><th>净盈亏</th><th>亏损额</th></tr></thead>
              <tbody>
                <tr v-for="row in analysis.byYear" :key="row.label">
                  <td class="muted-code">{{ row.label }}</td>
                  <td>{{ integer(row.count) }}</td>
                  <td>{{ integer(row.lossCount) }}</td>
                  <td :class="{ positive: row.net >= 0, negative: row.net < 0 }">{{ money(row.net) }}</td>
                  <td class="negative">{{ row.loss ? money(-row.loss) : '—' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <div class="pnl-block">
          <h4>亏损与持有天数</h4>
          <div class="table-wrap">
            <table :data-testid="`${testid}-by-holding`">
              <caption class="sr-only">按持有天数统计盈亏</caption>
              <thead><tr><th>持有天数</th><th>平仓</th><th>亏损</th><th>净盈亏</th><th>亏损额</th></tr></thead>
              <tbody>
                <tr v-for="row in analysis.byHolding" :key="row.label">
                  <td>{{ row.label }}</td>
                  <td>{{ integer(row.count) }}</td>
                  <td>{{ integer(row.lossCount) }}</td>
                  <td :class="{ positive: row.net >= 0, negative: row.net < 0 }">{{ money(row.net) }}</td>
                  <td class="negative">{{ row.loss ? money(-row.loss) : '—' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <div class="pnl-block">
          <h4>亏损幅度分布</h4>
          <div class="table-wrap">
            <table :data-testid="`${testid}-by-return`">
              <caption class="sr-only">按收益率区间统计盈亏</caption>
              <thead><tr><th>收益率区间</th><th>平仓</th><th>亏损</th><th>净盈亏</th><th>亏损额</th></tr></thead>
              <tbody>
                <tr v-for="row in analysis.byReturn" :key="row.label">
                  <td>{{ row.label }}</td>
                  <td>{{ integer(row.count) }}</td>
                  <td>{{ integer(row.lossCount) }}</td>
                  <td :class="{ positive: row.net >= 0, negative: row.net < 0 }">{{ money(row.net) }}</td>
                  <td class="negative">{{ row.loss ? money(-row.loss) : '—' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <div class="pnl-block">
          <h4>平仓原因</h4>
          <div class="table-wrap">
            <table :data-testid="`${testid}-by-reason`">
              <caption class="sr-only">按平仓原因统计盈亏</caption>
              <thead><tr><th>原因</th><th>平仓</th><th>亏损</th><th>净盈亏</th><th>亏损额</th></tr></thead>
              <tbody>
                <tr v-for="row in analysis.byReason" :key="row.label">
                  <td>{{ row.label }}</td>
                  <td>{{ integer(row.count) }}</td>
                  <td>{{ integer(row.lossCount) }}</td>
                  <td :class="{ positive: row.net >= 0, negative: row.net < 0 }">{{ money(row.net) }}</td>
                  <td class="negative">{{ row.loss ? money(-row.loss) : '—' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <h4>亏损最重的 10 段平仓</h4>
      <div class="table-wrap">
        <table :data-testid="`${testid}-worst`">
          <caption class="sr-only">亏损最重的平仓</caption>
          <thead><tr><th>标的</th><th>持有区间</th><th>天数</th><th>收益率</th><th>盈亏</th><th>原因</th></tr></thead>
          <tbody>
            <tr v-for="(trip, index) in analysis.worstTrips" :key="`${trip.code}-${trip.exitDate}-${index}`">
              <td>{{ trip.label }}</td>
              <td class="muted-code">{{ trip.entryDate || '—' }} → {{ trip.exitDate }}</td>
              <td>{{ trip.entryDate ? `${trip.holdingDays} 日` : '—' }}</td>
              <td class="negative">{{ percent(trip.returnPct) }}</td>
              <td class="negative">{{ money(trip.pnl) }}</td>
              <td>{{ trip.reason }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="pnl-grid">
        <div class="pnl-block">
          <h4>连续亏损与回撤</h4>
          <div class="table-wrap">
            <table :data-testid="`${testid}-streak`">
              <caption class="sr-only">连续亏损与净值回撤</caption>
              <thead><tr><th>指标</th><th>数值</th></tr></thead>
              <tbody>
                <tr><td>最长连亏</td><td>{{ analysis.streak.longest }} 段</td></tr>
                <tr><td>连亏期间最大累计亏损</td><td class="negative">{{ money(-analysis.streak.worstAmount) }}</td></tr>
                <tr><td>期末是否仍在连亏</td><td>{{ analysis.streak.trailing ? `是（${analysis.streak.trailing} 段）` : '否' }}</td></tr>
                <tr v-for="(episode, index) in analysis.drawdowns" :key="`${episode.start}-${index}`">
                  <td>回撤 {{ episode.start }} → {{ episode.end }}（{{ episode.bars }} 日）</td>
                  <td class="negative">
                    {{ percent(episode.depth) }}
                    <small v-if="episode.benchmarkChange !== null" class="muted-code"> · 同期基准 {{ percent(episode.benchmarkChange) }}</small>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <div class="pnl-block">
          <h4>期末未实现浮亏</h4>
          <div class="table-wrap">
            <table :data-testid="`${testid}-unrealized`">
              <caption class="sr-only">期末未实现浮亏</caption>
              <thead><tr><th>标的</th><th>成本 → 现价</th><th>浮动</th><th>浮盈亏</th></tr></thead>
              <tbody>
                <tr v-if="!analysis.unrealized.rows.length"><td colspan="4" class="muted-code">回测结束时没有持仓。</td></tr>
                <tr v-for="row in analysis.unrealized.rows" :key="row.label">
                  <td>{{ row.label }}</td>
                  <td class="muted-code">{{ money(row.entry) }} → {{ money(row.close) }}</td>
                  <td :class="{ positive: (row.pct ?? 0) >= 0, negative: (row.pct ?? 0) < 0 }">{{ percent(row.pct) }}</td>
                  <td :class="{ positive: row.pnl >= 0, negative: row.pnl < 0 }">{{ money(row.pnl) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <p v-if="analysis.unrealized.rows.length" class="pnl-note">
            合计浮盈亏 {{ money(analysis.unrealized.total) }}（未平仓，不计入上面的已实现盈亏）。
          </p>
        </div>

        <div class="pnl-block">
          <h4>本次风控设置</h4>
          <div class="table-wrap">
            <table :data-testid="`${testid}-risk`">
              <caption class="sr-only">本次回测的止盈止损设置</caption>
              <thead><tr><th>项目</th><th>设置</th></tr></thead>
              <tbody>
                <tr v-for="item in analysis.riskControls" :key="item.label">
                  <td>{{ item.label }}</td>
                  <td :class="{ negative: !item.enabled }">{{ item.setting }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <p v-if="analysis.noStopLoss" class="pnl-note" :data-testid="`${testid}-no-stop-loss`">
            没有启用止损/移动止盈：亏损段只能等卖出条件触发，深度由行情决定。
          </p>
        </div>
      </div>

      <p class="pnl-footnote">
        口径：按标的 FIFO 把买卖配成平仓段；收益率 = 盈亏 ÷ 含买入成本的本金；标的名称取自当前行情库快照，不做历史归因。
      </p>
    </template>
  </section>
</template>
