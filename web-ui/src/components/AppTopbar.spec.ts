import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import AppTopbar from './AppTopbar.vue'
import * as releaseApi from '../api/release'
import type { ReleaseStatus } from '../types'

vi.mock('../api/release', async () => {
  const actual = await vi.importActual<typeof import('../api/release')>('../api/release')
  return { ...actual, fetchReleaseStatus: vi.fn() }
})

// 顶栏在每个页面都挂一份，不自动卸载会让相邻用例共用 localStorage。
enableAutoUnmount(afterEach)

const DISMISS_KEY = 'aquest.release-dismissed-tag'

function releaseStatus(overrides: Partial<ReleaseStatus> = {}): ReleaseStatus {
  return {
    current: '0.1.7',
    repository: 'kendev93/aquest',
    enabled: true,
    update_available: true,
    latest: {
      tag: 'v0.1.8',
      version: 'v0.1.8',
      url: 'https://github.com/kendev93/aquest/releases/tag/v0.1.8',
      published_at: '2026-10-24T02:00:00Z',
    },
    checked_at: '2026-10-24T02:05:00+00:00',
    error: null,
    ...overrides,
  }
}

describe('AppTopbar 的版本升级提示', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.mocked(releaseApi.fetchReleaseStatus).mockReset()
  })

  it('names the newer tag and links to its release page', async () => {
    vi.mocked(releaseApi.fetchReleaseStatus).mockResolvedValue(releaseStatus())
    const wrapper = mount(AppTopbar)
    await flushPromises()

    const notice = wrapper.get('[data-testid="release-notice"]')
    expect(notice.text()).toBe('新版本 v0.1.8 可升级')
    expect(notice.attributes('href')).toBe('https://github.com/kendev93/aquest/releases/tag/v0.1.8')
    // 外链必须隔离开：不带 noopener 的话发布页能拿到 window.opener。
    expect(notice.attributes('target')).toBe('_blank')
    expect(notice.attributes('rel')).toBe('noopener')
    // 提示里要写清"为什么不直接帮我升级"和当前版本，否则用户会以为点一下就升好了。
    expect(notice.attributes('title')).toContain('本机在跑 0.1.7')
    expect(notice.attributes('title')).toContain('docker compose pull')
  })

  it('stays silent when there is nothing newer', async () => {
    vi.mocked(releaseApi.fetchReleaseStatus).mockResolvedValue(
      releaseStatus({ update_available: false }),
    )
    const wrapper = mount(AppTopbar)
    await flushPromises()

    expect(wrapper.find('[data-testid="release-notice"]').exists()).toBe(false)
    expect(wrapper.get('.topbar-status').text()).toContain('本地数据模式')
  })

  it('stays silent when the check is turned off or the read fails', async () => {
    // 离线部署（AQUEST_UPDATE_CHECK=off）与 GitHub 不可达都不该点亮任何东西。
    vi.mocked(releaseApi.fetchReleaseStatus).mockResolvedValue(
      releaseStatus({ enabled: false, update_available: false, latest: null }),
    )
    const disabled = mount(AppTopbar)
    await flushPromises()
    expect(disabled.find('[data-testid="release-notice"]').exists()).toBe(false)
    disabled.unmount()

    vi.mocked(releaseApi.fetchReleaseStatus).mockRejectedValue(new Error('读不到'))
    const failed = mount(AppTopbar)
    await flushPromises()
    expect(failed.find('[data-testid="release-notice"]').exists()).toBe(false)
  })

  it('keeps a dismissed version quiet until a newer one shows up', async () => {
    vi.mocked(releaseApi.fetchReleaseStatus).mockResolvedValue(releaseStatus())
    const wrapper = mount(AppTopbar)
    await flushPromises()

    await wrapper.get('[data-testid="release-dismiss"]').trigger('click')
    expect(wrapper.find('[data-testid="release-notice"]').exists()).toBe(false)
    expect(localStorage.getItem(DISMISS_KEY)).toBe('v0.1.8')

    // 重新挂载（换页面、刷新）仍然不再提醒同一个版本。
    const remounted = mount(AppTopbar)
    await flushPromises()
    expect(remounted.find('[data-testid="release-notice"]').exists()).toBe(false)
    remounted.unmount()

    // 上游又发了更新的版本，提示回来。
    vi.mocked(releaseApi.fetchReleaseStatus).mockResolvedValue(
      releaseStatus({
        latest: {
          tag: 'v0.1.9',
          version: 'v0.1.9',
          url: 'https://github.com/kendev93/aquest/releases/tag/v0.1.9',
          published_at: '2026-11-01T02:00:00Z',
        },
      }),
    )
    const newer = mount(AppTopbar)
    await flushPromises()
    expect(newer.get('[data-testid="release-notice"]').text()).toBe('新版本 v0.1.9 可升级')
  })
})
