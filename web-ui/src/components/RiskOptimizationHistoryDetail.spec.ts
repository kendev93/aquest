import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import RiskOptimizationHistoryDetail from './RiskOptimizationHistoryDetail.vue'

import type { StrategyDiscoveryCandidate, StrategyDiscoveryResult } from '../types'

function makeCandidate(
  overrides: Partial<StrategyDiscoveryCandidate> = {},
): StrategyDiscoveryCandidate {
  return {
    candidate_hash: 'hash-sharpe',
    complexity: 3,
    human_readable_rules: 'C > 10',
    config: {},
    metrics: {
      train: {
        total_return: 0.12,
        annual_return: 0.25,
        sharpe: 1.2,
        sortino: 1.6,
        calmar: 0.9,
        volatility: 0.18,
        max_drawdown: 0.08,
        max_dd_duration: 12,
        win_rate: 0.6,
        total_trades: 40,
        win_trades: 24,
        lose_trades: 16,
        avg_win: 0.03,
        avg_loss: -0.015,
        payoff_ratio: 2,
        profit_factor: 999,
        max_win: 0.12,
        max_loss: -0.09,
        avg_holding_days: 8.5,
        win_holding_days: 9.5,
        loss_holding_days: 6.5,
        max_holding_days: 30,
        executed_buy_orders: 20,
        executed_sell_orders: 20,
        buy_turnover: 1.2,
        sell_turnover: 0.8,
        turnover: 2,
        buy_transaction_costs: 1234.5,
        sell_transaction_costs: 678.25,
        rejected_buy_orders: 3,
        rejected_sell_orders: 1,
      },
      // 训练段有交易、验证段零交易：payoff_ratio 在 total_trades === 0 时应显示 '—'，
      // 而不是拿缺失的 0 去当盈亏比。
      validation: {
        total_return: 0.05,
        sharpe: 0.8,
        total_trades: 0,
        win_trades: 0,
        lose_trades: 0,
      },
      // 留出段有交易但没亏损：盈亏比与盈利因子都应显示"无亏损"。
      holdout: {
        total_return: 0.02,
        sharpe: 0.4,
        total_trades: 10,
        win_trades: 10,
        lose_trades: 0,
        payoff_ratio: null,
        profit_factor: null,
        avg_loss: null,
        turnover: null,
        buy_turnover: null,
        sell_turnover: null,
      },
    },
    ...overrides,
  }
}

function makeResult(
  overrides: Partial<StrategyDiscoveryResult> = {},
): StrategyDiscoveryResult {
  return {
    request: {},
    results_by_sharpe: [makeCandidate()],
    results_by_return: [
      makeCandidate({ candidate_hash: 'hash-return', human_readable_rules: 'C < 5' }),
    ],
    search_metadata: {
      fidelity_evaluations: { coarse: 120, medium: 40, full: 10 },
      stage_throughput_per_second: { coarse: 12.5, medium: 8, full: 3 },
      evaluation_mode: 'process',
      evaluation_workers: 4,
      evaluation_batch_size: 32,
      proposal_attempts: 1000,
      duplicate_trials: 250,
      unique_proposal_rate: 0.75,
    },
    validation_metadata: {
      validated_candidate_count: 12,
      search_candidate_count: 40,
      holdout_candidate_count: 5,
    },
    algorithm_metadata: {
      snapshot_metrics: {
        snapshot_rows: 123456,
        snapshot_bytes: 5 * 1024 ** 3,
        snapshot_cache_hit: true,
        indicator_cache_hits: 900,
        indicator_cache_misses: 100,
        indicator_computed: 100,
      },
    },
    stage_durations_seconds: {
      snapshot: 95,
      indicators: 3661,
      coarse: 12.5,
      medium: 30,
      full: 120,
      validation: 61,
      holdout: 30,
      candidate_generation: 0,
    },
    duration_seconds: 3725,
    ...overrides,
  }
}

describe('RiskOptimizationHistoryDetail', () => {
  it('把各阶段耗时、吞吐与快照读数渲染成人话', () => {
    const wrapper = mount(RiskOptimizationHistoryDetail, {
      props: { result: makeResult() },
    })
    const text = wrapper.text()
    // 3725 秒 -> 1 小时 2 分钟；95 秒 -> 1 分 35 秒；120 秒 -> 2 分钟。
    expect(text).toContain('1 小时 2 分钟')
    expect(text).toContain('1 分 35 秒')
    expect(text).toContain('2 分钟')
    expect(text).toContain('12.5 秒')
    expect(text).toContain('12.50 组/秒')
    expect(text).toContain('75.00%')
    expect(text).toContain('123456 行')
    expect(text).toContain('5.00 GB')
    expect(text).toContain('快照命中')
    expect(text).toContain('4 workers')
  })

  it('快照缓存未命中与缺失字段都给出可读兜底', () => {
    const result = makeResult({
      algorithm_metadata: {
        snapshot_metrics: { snapshot_rows: 10, snapshot_bytes: 2048, snapshot_cache_hit: false },
      },
      stage_durations_seconds: {},
    })
    const wrapper = mount(RiskOptimizationHistoryDetail, { props: { result } })
    const text = wrapper.text()
    expect(text).toContain('快照未命中')
    // 2048 字节落在 KB 档；耗时字段整段缺失时是 '—' 而不是 NaN 或 0 秒。
    expect(text).toContain('2 KB')
    expect(text).toContain('—')
    expect(text).not.toContain('NaN')
  })

  it('候选指标按训练/验证/留出三段分别格式化', () => {
    const wrapper = mount(RiskOptimizationHistoryDetail, {
      props: { result: makeResult() },
    })
    const text = wrapper.text()
    expect(text).toContain('C > 10')
    expect(text).toContain('12.00%') // 训练段总收益
    expect(text).toContain('1.20') // 训练段 Sharpe
    expect(text).toContain('1.50%') // 平均亏损取绝对值
    expect(text).toContain('2.00×') // 总换手
    expect(text).toContain('¥1,234.50') // 买入成本
    // 盈利因子 999 是哨兵值，应显示"无亏损"而不是 999.00。
    expect(text).toContain('无亏损')
    expect(text).not.toContain('999.00')
    // 三段都要出现，缺失的指标用 '—' 兜底。
    expect(text).toContain('训练段')
    expect(text).toContain('验证段')
    expect(text).toContain('留出段')
  })

  it('在夏普榜与收益榜之间切换', async () => {
    const wrapper = mount(RiskOptimizationHistoryDetail, {
      props: { result: makeResult() },
    })
    expect(wrapper.text()).toContain('C > 10')
    await wrapper.get('[data-testid="portfolio-risk-history-detail-return"]').trigger('click')
    expect(wrapper.text()).toContain('C < 5')
    await wrapper.get('[data-testid="portfolio-risk-history-detail-sharpe"]').trigger('click')
    expect(wrapper.text()).toContain('C > 10')
  })

  it('空榜单给出提示而不是空白面板', () => {
    const wrapper = mount(RiskOptimizationHistoryDetail, {
      props: { result: makeResult({ results_by_sharpe: [], results_by_return: [] }) },
    })
    expect(wrapper.text()).toContain('此榜单没有候选。')
  })

  it('关闭按钮向上层抛 close', async () => {
    const wrapper = mount(RiskOptimizationHistoryDetail, {
      props: { result: makeResult() },
    })
    await wrapper.get('[data-testid="portfolio-risk-history-detail-close"]').trigger('click')
    expect(wrapper.emitted('close')).toHaveLength(1)
  })
})
