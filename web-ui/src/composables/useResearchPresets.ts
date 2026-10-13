/**
 * 研究预设（保存 / 加载 / 覆盖 / 重命名 / 删除）的状态与动作。
 *
 * 从 ConditionResearchPage 拆出来：这块自带 API 往返和 busy 状态，但读写表单
 * 都要经过页面的 buildPayload / applyStoredRequest，所以依赖以回调 + ref 注入，
 * 本模块不持有表单。模板里的 data-testid（research-preset-*）仍在页面，
 * 约定不变。
 */

import { ref } from 'vue'
import type { ComputedRef, Ref } from 'vue'

import {
  createResearchPreset,
  deleteResearchPreset,
  fetchResearchPresets,
  getResearchPreset,
  updateResearchPreset,
} from '../api/researchPresets'
import { presetLoadMessage, stalePresetReferences } from '../utils/conditionResearch'
import type { ResearchPresetAvailability } from '../utils/conditionResearch'
import type { ConditionResearchPayload, ResearchPresetSummary } from '../types'

export interface PresetLoadOutcome {
  name: string
  stale: number
}

export interface ResearchPresetDeps {
  /** 当前表单的完整 payload（保存新方案 / 覆盖旧方案时上传的就是它）。 */
  buildPayload: () => ConditionResearchPayload
  /** 把一份存储的 payload 还原进表单（含自定义公式的重新解析）。 */
  applyStoredRequest: (raw: unknown) => Promise<void>
  /** 按当前表单跑一轮研究；返回是否真的启动了。 */
  submit: () => Promise<boolean>
  /** 页面顶部的消息条：预设操作的成败都写在这里。 */
  message: Ref<string>
  /** 自定义公式解析失败的原因；载入方案后据此调整措辞。 */
  customParseError: Ref<string>
  /** 当前实际可用的指标 / 信号，用来数方案里失效的引用。 */
  presetAvailability: ComputedRef<ResearchPresetAvailability>
  /** 后端错误 → 用户可读文案（fallback 文案是页面自己的）。 */
  describeError: (error: unknown) => string
}

export function useResearchPresets(deps: ResearchPresetDeps) {
  const researchPresets = ref<ResearchPresetSummary[]>([])
  const selectedPresetId = ref('')
  const presetName = ref('')
  const presetBusy = ref(false)

  async function refreshResearchPresets(): Promise<void> {
    try {
      researchPresets.value = await fetchResearchPresets()
    } catch {
      // 列表拉不到不影响研究，只在方案区留空即可。
    }
  }

  async function loadSelectedPreset(): Promise<PresetLoadOutcome | null> {
    const presetId = selectedPresetId.value
    if (!presetId) return null
    presetBusy.value = true
    try {
      const preset = await getResearchPreset(presetId)
      await deps.applyStoredRequest(preset.payload)
      presetName.value = preset.name
      // 数的是 payload 这份原始记录，不是表单：还原时 signals 会被丢掉（研究页
      // 的条件不用信号），数表单一项都数不出来。
      //
      // 放在还原之后而不是之前：自定义公式要解析完才知道它到底产出哪些输出/信
      // 号，在还原前数的话，一个自定义方案的每一项都会被误判成失效——那时手上
      // 的依据还是上一份（预置）元数据。
      const stale = stalePresetReferences(preset.payload, deps.presetAvailability.value)
      if (deps.customParseError.value) {
        // 公式没解析出来时上面的失效计数是空的（没有可对照的清单），不能据此
        // 报「已载入」。
        deps.message.value = `已载入研究方案「${preset.name}」，但自定义公式没能解析：${deps.customParseError.value}`
        return { name: preset.name, stale: 0 }
      }
      deps.message.value = presetLoadMessage(preset.name, stale)
      return { name: preset.name, stale }
    } catch (error) {
      deps.message.value = deps.describeError(error)
      return null
    } finally {
      presetBusy.value = false
    }
  }

  /** 一键研究：载入方案后立刻跑，省掉「加载 → 滚到底部点开始」两步。 */
  async function quickResearch(): Promise<void> {
    const loaded = await loadSelectedPreset()
    if (!loaded) return
    const ran = await deps.submit()
    if (loaded.stale > 0) {
      // 失效引用比「研究完成」重要：条件已经被悄悄换过，不能只报喜。
      deps.message.value = `已按方案「${loaded.name}」运行，但有 ${loaded.stale} 项引用了当前不存在的指标或信号、已被替换，请先核对条件。`
    } else if (!ran) {
      // submit 失败时自己会写 message（后端拒绝的原因就在里面）。只有它什么都没
      // 说——也就是本地校验没过——才补这句，否则会把真正的原因盖掉。
      if (!deps.message.value) {
        deps.message.value = `已载入方案「${loaded.name}」，但当前配置没能通过校验，请修正后再运行。`
      }
    }
  }

  async function savePresetAsNew(): Promise<void> {
    const name = presetName.value.trim()
    if (!name) {
      deps.message.value = '请先填写研究方案名称。'
      return
    }
    presetBusy.value = true
    try {
      const created = await createResearchPreset({ name, payload: deps.buildPayload() })
      await refreshResearchPresets()
      selectedPresetId.value = created.preset_id
      presetName.value = created.name
      deps.message.value = `已保存研究方案「${created.name}」。`
    } catch (error) {
      deps.message.value = deps.describeError(error)
    } finally {
      presetBusy.value = false
    }
  }

  async function overwritePreset(): Promise<void> {
    const presetId = selectedPresetId.value
    if (!presetId) return
    presetBusy.value = true
    try {
      const updated = await updateResearchPreset(presetId, { payload: deps.buildPayload() })
      await refreshResearchPresets()
      deps.message.value = `已用当前设置覆盖研究方案「${updated.name}」。`
    } catch (error) {
      deps.message.value = deps.describeError(error)
    } finally {
      presetBusy.value = false
    }
  }

  async function renamePreset(): Promise<void> {
    const presetId = selectedPresetId.value
    const name = presetName.value.trim()
    if (!presetId) return
    if (!name) {
      deps.message.value = '请先填写新的研究方案名称。'
      return
    }
    presetBusy.value = true
    try {
      const updated = await updateResearchPreset(presetId, { name })
      await refreshResearchPresets()
      presetName.value = updated.name
      deps.message.value = `研究方案已重命名为「${updated.name}」。`
    } catch (error) {
      deps.message.value = deps.describeError(error)
    } finally {
      presetBusy.value = false
    }
  }

  async function deleteSelectedPreset(): Promise<void> {
    const presetId = selectedPresetId.value
    if (!presetId) return
    presetBusy.value = true
    try {
      await deleteResearchPreset(presetId)
      selectedPresetId.value = ''
      presetName.value = ''
      await refreshResearchPresets()
      deps.message.value = '已删除研究方案。'
    } catch (error) {
      deps.message.value = deps.describeError(error)
    } finally {
      presetBusy.value = false
    }
  }

  return {
    researchPresets,
    selectedPresetId,
    presetName,
    presetBusy,
    refreshResearchPresets,
    loadSelectedPreset,
    quickResearch,
    savePresetAsNew,
    overwritePreset,
    renamePreset,
    deleteSelectedPreset,
  }
}
