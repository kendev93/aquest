import { beforeEach, describe, expect, it, vi } from 'vitest'
import { computed, ref } from 'vue'

import {
  createResearchPreset,
  deleteResearchPreset,
  fetchResearchPresets,
  getResearchPreset,
  updateResearchPreset,
} from '../api/researchPresets'
import { presetLoadMessage, stalePresetReferences } from '../utils/conditionResearch'
import { useResearchPresets } from './useResearchPresets'
import type { ResearchPresetDeps } from './useResearchPresets'
import type { ResearchPresetSummary } from '../types'

vi.mock('../api/researchPresets', () => ({
  createResearchPreset: vi.fn(),
  deleteResearchPreset: vi.fn(),
  fetchResearchPresets: vi.fn(),
  getResearchPreset: vi.fn(),
  updateResearchPreset: vi.fn(),
}))

vi.mock('../utils/conditionResearch', () => ({
  stalePresetReferences: vi.fn().mockReturnValue(0),
  presetLoadMessage: vi.fn((name: string, stale: number) => `已载入「${name}」，失效 ${stale} 项`),
}))

const preset: ResearchPresetSummary = {
  preset_id: 'preset-1',
  name: '超跌反弹',
  payload: { universe: 'all' },
  created_at: '2026-09-20 01:00:00',
  updated_at: '2026-09-20 01:00:00',
} as unknown as ResearchPresetSummary

function makeDeps(over: Partial<ResearchPresetDeps> = {}): ResearchPresetDeps {
  return {
    buildPayload: vi.fn().mockReturnValue({ universe: 'all' }),
    applyStoredRequest: vi.fn().mockResolvedValue(undefined),
    submit: vi.fn().mockResolvedValue(true),
    message: ref(''),
    customParseError: ref(''),
    presetAvailability: computed(() => ({ valueIds: [], signalIds: [] })),
    describeError: (error: unknown) => (error instanceof Error ? error.message : '方案操作失败'),
    ...over,
  }
}

describe('useResearchPresets', () => {
  let deps: ResearchPresetDeps

  beforeEach(() => {
    vi.clearAllMocks()
    deps = makeDeps()
    vi.mocked(fetchResearchPresets).mockResolvedValue([preset])
    vi.mocked(getResearchPreset).mockResolvedValue(preset as never)
    vi.mocked(stalePresetReferences).mockReturnValue(0)
    vi.mocked(presetLoadMessage).mockImplementation(
      (name: string, stale: number) => `已载入「${name}」，失效 ${stale} 项`,
    )
  })

  it('tolerates a preset list it cannot fetch', async () => {
    vi.mocked(fetchResearchPresets).mockRejectedValueOnce(new Error('服务未就绪'))
    const presets = useResearchPresets(deps)
    await presets.refreshResearchPresets()
    expect(presets.researchPresets.value).toEqual([])
    expect(deps.message.value).toBe('')
  })

  it('counts stale references only after the payload has been restored', async () => {
    // 还原自定义公式之前手上没有可对照的清单：这时数出来的失效项全是误判。
    let parsed = false
    deps = makeDeps({
      applyStoredRequest: vi.fn(async () => {
        parsed = true
      }),
    })
    vi.mocked(stalePresetReferences).mockImplementation(() => (parsed ? 2 : 0))
    const presets = useResearchPresets(deps)
    presets.selectedPresetId.value = 'preset-1'

    const outcome = await presets.loadSelectedPreset()
    expect(outcome).toEqual({ name: '超跌反弹', stale: 2 })
    expect(presets.presetName.value).toBe('超跌反弹')
    expect(deps.message.value).toBe('已载入「超跌反弹」，失效 2 项')
    expect(presets.presetBusy.value).toBe(false)
  })

  it('does not report success when the custom formula failed to parse', async () => {
    deps = makeDeps({ customParseError: ref('函数 MA99 不受支持') })
    const presets = useResearchPresets(deps)
    presets.selectedPresetId.value = 'preset-1'

    await expect(presets.loadSelectedPreset()).resolves.toEqual({ name: '超跌反弹', stale: 0 })
    expect(deps.message.value).toContain('自定义公式没能解析')
    expect(presetLoadMessage).not.toHaveBeenCalled()
  })

  it('returns null without a selection and reports backend failures', async () => {
    const presets = useResearchPresets(deps)
    await expect(presets.loadSelectedPreset()).resolves.toBeNull()
    expect(getResearchPreset).not.toHaveBeenCalled()

    presets.selectedPresetId.value = 'preset-1'
    vi.mocked(getResearchPreset).mockRejectedValueOnce(new Error('方案已被删除'))
    await expect(presets.loadSelectedPreset()).resolves.toBeNull()
    expect(deps.message.value).toBe('方案已被删除')
    expect(presets.presetBusy.value).toBe(false)
  })

  it('warns about stale references ahead of the completion message', async () => {
    deps = makeDeps()
    vi.mocked(stalePresetReferences).mockReturnValue(3)
    const presets = useResearchPresets(deps)
    presets.selectedPresetId.value = 'preset-1'
    await presets.quickResearch()

    expect(deps.submit).toHaveBeenCalledTimes(1)
    expect(deps.message.value).toBe(
      '已按方案「超跌反弹」运行，但有 3 项引用了当前不存在的指标或信号、已被替换，请先核对条件。',
    )
  })

  it('only adds its own validation notice when submit stayed silent', async () => {
    // 真实 submit 一进来就清空消息条：校验没过时它什么都不写，只剩下载入提示被抹掉后的空白。
    deps = makeDeps({ submit: vi.fn(async () => {
      deps.message.value = ''
      return false
    }) })
    const presets = useResearchPresets(deps)
    presets.selectedPresetId.value = 'preset-1'
    await presets.quickResearch()
    expect(deps.message.value).toBe('已载入方案「超跌反弹」，但当前配置没能通过校验，请修正后再运行。')

    deps = makeDeps({ submit: vi.fn(async () => {
      deps.message.value = '标的代码不能为空'
      return false
    }) })
    const second = useResearchPresets(deps)
    second.selectedPresetId.value = 'preset-1'
    await second.quickResearch()
    // 后端拒绝的原因不能被这句补话盖掉。
    expect(deps.message.value).toBe('标的代码不能为空')
  })

  it('saves a new preset and selects what the backend stored', async () => {
    vi.mocked(createResearchPreset).mockResolvedValue({ ...preset, preset_id: 'preset-2', name: '超跌反弹 v2' } as never)
    const presets = useResearchPresets(deps)
    presets.presetName.value = '  超跌反弹 v2  '
    await presets.savePresetAsNew()

    expect(createResearchPreset).toHaveBeenCalledWith({ name: '超跌反弹 v2', payload: { universe: 'all' } })
    expect(presets.selectedPresetId.value).toBe('preset-2')
    expect(presets.presetName.value).toBe('超跌反弹 v2')
    expect(deps.message.value).toBe('已保存研究方案「超跌反弹 v2」。')

    presets.presetName.value = '   '
    await presets.savePresetAsNew()
    expect(deps.message.value).toBe('请先填写研究方案名称。')
  })

  it('gates overwrite, rename and delete on having a selected preset', async () => {
    vi.mocked(updateResearchPreset).mockResolvedValue(preset as never)
    vi.mocked(deleteResearchPreset).mockResolvedValue(undefined as never)
    const presets = useResearchPresets(deps)

    await presets.overwritePreset()
    await presets.renamePreset()
    await presets.deleteSelectedPreset()
    expect(updateResearchPreset).not.toHaveBeenCalled()
    expect(deleteResearchPreset).not.toHaveBeenCalled()

    presets.selectedPresetId.value = 'preset-1'
    await presets.overwritePreset()
    expect(updateResearchPreset).toHaveBeenCalledWith('preset-1', { payload: { universe: 'all' } })
    expect(deps.message.value).toBe('已用当前设置覆盖研究方案「超跌反弹」。')

    presets.presetName.value = ''
    await presets.renamePreset()
    expect(deps.message.value).toBe('请先填写新的研究方案名称。')

    presets.presetName.value = ' 缩量整理 '
    vi.mocked(updateResearchPreset).mockResolvedValue({ ...preset, name: '缩量整理' } as never)
    await presets.renamePreset()
    expect(updateResearchPreset).toHaveBeenLastCalledWith('preset-1', { name: '缩量整理' })
    expect(presets.presetName.value).toBe('缩量整理')
    expect(deps.message.value).toBe('研究方案已重命名为「缩量整理」。')

    await presets.deleteSelectedPreset()
    expect(deleteResearchPreset).toHaveBeenCalledWith('preset-1')
    expect(presets.selectedPresetId.value).toBe('')
    expect(presets.presetName.value).toBe('')
    expect(deps.message.value).toBe('已删除研究方案。')
  })
})
