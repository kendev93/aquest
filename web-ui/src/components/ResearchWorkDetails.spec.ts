import { mount, flushPromises } from '@vue/test-utils'
import { expect, it, vi } from 'vitest'
import ResearchWorkDetails from './ResearchWorkDetails.vue'
vi.mock('../api/formulaScreen', () => ({ fetchMetadata: vi.fn(async () => ({ indicators: [{ values: [{ id: 'rsi', display_name: '相对强弱' }], signals: [] }] })) }))
it('shows actual work, localized rules and finite observed metrics', async () => {
  const page = mount(ResearchWorkDetails, { props: { details: {
    stage: '完整精度复核候选', current: 12, total: 40, elapsed_seconds: 63,
    buy_rule: { conditions: [{ left_value: 'rsi', operator: 'lt', right_constant: 30 }] },
    best_return: .15, best_sharpe: 1.2, best_orders: 24,
  } } })
  await flushPromises()
  expect(page.text()).toContain('12 / 40')
  expect(page.text()).toContain('相对强弱 < 30')
  expect(page.text()).toContain('15.00%')
  expect(page.text()).toContain('最多成交 24 笔')
  expect(page.text()).toContain('63 秒')
  await page.setProps({ details: {} })
  expect(page.find('[data-testid="research-work-details"]').exists()).toBe(false)
})

it('shows the executed order count so a flat zero is not read as a real result', async () => {
  // 零成交候选的年化与夏普都是 0.0：只有成交数能把「一次都没买进去」和「赚 0%」分开。
  const flat = mount(ResearchWorkDetails, { props: { details: {
    stage: '搜索止损、止盈和冷却参数', current: 800, total: 3000, elapsed_seconds: 166,
    best_return: 0, best_sharpe: 0, best_orders: 0,
  } } })
  await flushPromises()
  expect(flat.text()).toContain('最高年化 0.00%')
  expect(flat.text()).toContain('最多成交 0 笔')
  expect(flat.text()).toContain('不是有效结果')

  const unknown = mount(ResearchWorkDetails, { props: { details: { stage: '验证区间回测', current: 1, total: 2 } } })
  await flushPromises()
  // 后端没给成交数时显示占位符，不能编成 0 笔。
  expect(unknown.text()).toContain('最多成交 —')
})
