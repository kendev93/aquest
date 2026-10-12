import { mount, flushPromises } from '@vue/test-utils'
import { expect, it, vi } from 'vitest'
import ResearchWorkDetails from './ResearchWorkDetails.vue'
vi.mock('../api/formulaScreen', () => ({ fetchMetadata: vi.fn(async () => ({ indicators: [{ values: [{ id: 'rsi', display_name: '相对强弱' }], signals: [] }] })) }))
it('shows actual work, localized rules and finite observed metrics', async () => {
  const page = mount(ResearchWorkDetails, { props: { details: {
    stage: '完整精度复核候选', current: 12, total: 40, elapsed_seconds: 63,
    buy_rule: { conditions: [{ left_value: 'rsi', operator: 'lt', right_constant: 30 }] },
    best_return: .15, best_sharpe: 1.2,
  } } })
  await flushPromises()
  expect(page.text()).toContain('12 / 40')
  expect(page.text()).toContain('相对强弱 < 30')
  expect(page.text()).toContain('15.00%')
  expect(page.text()).toContain('63 秒')
  await page.setProps({ details: {} })
  expect(page.find('[data-testid="research-work-details"]').exists()).toBe(false)
})
