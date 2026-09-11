import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import type { BacktestHistorySummary } from '../types'
import BacktestHistoryPanel from './BacktestHistoryPanel.vue'

const item: BacktestHistorySummary = {
  history_id: 'history-1',
  backtest_type: 'single_stock',
  created_at: '2026-09-11 08:30:00',
  started_at: '2026-09-11 08:30:00',
  finished_at: '2026-09-11 08:30:02',
  duration_seconds: 2.5,
  summary: {
    market: 'SH',
    code: '600000',
    start_date: '2024-01-01',
    end_date: '2024-01-10',
  },
}

describe('BacktestHistoryPanel', () => {
  it('renders timestamps and emits load, remove, and refresh actions', async () => {
    const wrapper = mount(BacktestHistoryPanel, {
      props: {
        items: [item],
        testid: 'history-panel',
        title: '历史回测',
        emptyText: '暂无历史回测。',
      },
    })

    expect(wrapper.text()).toContain('SH 600000')
    expect(wrapper.text()).toContain('2026-09-11 08:30:02')
    expect(wrapper.text()).toContain('2.5 秒')
    await wrapper.get('[data-testid="history-panel-load-history-1"]').trigger('click')
    await wrapper.get('[data-testid="history-panel-remove-history-1"]').trigger('click')
    await wrapper.get('button').trigger('click')

    expect(wrapper.emitted('load')).toEqual([['history-1']])
    expect(wrapper.emitted('remove')).toEqual([['history-1']])
    expect(wrapper.emitted('refresh')).toEqual([[]])
  })
})
