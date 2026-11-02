import { computed, reactive, ref, type Ref } from 'vue'

import {
  createStockGroup,
  deleteStockGroup,
  fetchStockGroups,
  fetchStockGroupsSummary,
  fetchStockGroupPerformance,
  getStockGroup,
  removeStockGroupMembers,
  renameStockGroup,
} from '../api/stockGroups'
import type {
  BasketCurve,
  CustomFormulaMetadata,
  FormulaScreenMetadata,
  ResultsMeta,
  ScreenFormState,
  ScreenResult,
  StockGroup,
  StockGroupMember,
  StockGroupPerformance,
  StockGroupsSummary,
  StockGroupSummary,
  ValueDefinition,
} from '../types'
import { buildChartGeometry } from '../utils/chart'
import { buildScanPayload, describeScreenForm } from '../utils/formulaScreen'
import { savePortfolioBacktestHandoff } from '../utils/portfolioBacktestHandoff'
import { formatChinaTimestamp } from '../utils/time'

interface FormulaScreenStockGroupOptions {
  results: Ref<ScreenResult[]>
  resultKey: (result: ScreenResult) => string
  form: ScreenFormState
  availableValues: Ref<ValueDefinition[]>
  metadata: Ref<FormulaScreenMetadata | null>
  customMetadata: Ref<CustomFormulaMetadata | null>
  resultMeta: Ref<ResultsMeta | null>
  parsedFormulaText: Ref<string>
  displayedRankingOrder: () => 'asc' | 'desc'
  apiMessage: (error: unknown, context: string) => string
  showMessage: (text: string, isError?: boolean) => void
}

/** 组数不限，但列表默认只铺最近 20 条，剩下的要显式点开——否则面板会被顶下去。 */
const VISIBLE_GROUP_LIMIT = 20

interface FoldedStat {
  label: string
  value: number | null
  excess: number | null
  trading_days: number
  complete: boolean
}

export function useFormulaScreenStockGroups(options: FormulaScreenStockGroupOptions) {
  const stockGroups = ref<StockGroupSummary[]>([])
  const selectedGroupId = ref('')
  const groupName = ref('')
  const groupBusy = ref(false)
  const groupError = ref('')
  // 展开态的「入组至今」按组缓存：算一次要扫一遍全市场，切走再点回来不该重算。
  const groupDetails = ref<Record<string, StockGroupPerformance>>({})
  const groupMembers = ref<Record<string, StockGroup>>({})
  const handoffGroupId = ref('')
  const expandedGroupIds = reactive(new Set<string>())
  const loadingGroupId = ref('')
  // 折叠态的「近 N 个交易日」：所有组共用同一窗口，一次请求全部拿回。
  const groupSummary = ref<StockGroupsSummary | null>(null)
  const showAllGroups = ref(false)
  // 结果行的勾选。用 Set 而不是数组：一次扫描可能选出几百只，includes 是线性的。
  const selectedResultKeys = reactive(new Set<string>())

  const visibleGroups = computed(() => (
    showAllGroups.value ? stockGroups.value : stockGroups.value.slice(0, VISIBLE_GROUP_LIMIT)
  ))
  const selectedResultCount = computed(() => (
    options.results.value.filter((item) => selectedResultKeys.has(options.resultKey(item))).length
  ))

  /** 折叠态那一行的「近 1 月 / 近 3 月」。窗口标签由后端下发，两边不各写一份。 */
  function foldedStats(group: StockGroupSummary): FoldedStat[] {
    const entry = groupSummary.value?.groups.find((item) => item.group_id === group.group_id)
    if (!entry) return []
    return entry.windows.map((item) => ({
      label: groupSummary.value?.labels[String(item.window)] ?? `近 ${item.window} 个交易日`,
      value: item.group_return,
      excess: item.excess_return,
      trading_days: item.trading_days,
      complete: item.complete,
    }))
  }

  function detailOf(group: StockGroupSummary): StockGroupPerformance | null {
    return groupDetails.value[group.group_id] ?? null
  }

  /** 详情可能是 null，模板里没法用 v-if 顺便拿到非空引用——用单元素数组起个名字。 */
  function detailList(group: StockGroupSummary): StockGroupPerformance[] {
    const detail = detailOf(group)
    return detail ? [detail] : []
  }

  function memberList(group: StockGroupSummary): StockGroup[] {
    const members = groupMembers.value[group.group_id]
    return members ? [members] : []
  }

  /** 两条曲线共用同一纵轴：各画各的会让「跑赢了多少」看起来比实际多。 */
  function chartOf(group: StockGroupSummary) {
    const payload = detailOf(group)
    if (!payload?.group_curve || !payload.market_curve) return null
    const toPoints = (curve: BasketCurve) => curve.totals.map((total, index) => ({
      x: index,
      y: (total - 1) * 100,
    }))
    return buildChartGeometry(
      [toPoints(payload.group_curve), toPoints(payload.market_curve)],
      { includeZero: true, height: 200 },
    )
  }

  function chartList(group: StockGroupSummary) {
    const chart = chartOf(group)
    return chart ? [chart] : []
  }

  /** 入组日期是这组唯一的锚点，按东八区显示——后端存的是 UTC。 */
  function groupDay(createdAt: string): string {
    return formatChinaTimestamp(createdAt).slice(0, 10)
  }

  function toggleResultPick(result: ScreenResult, checked: boolean): void {
    const key = options.resultKey(result)
    if (checked) selectedResultKeys.add(key)
    else selectedResultKeys.delete(key)
  }

  async function refreshStockGroups(): Promise<void> {
    try {
      stockGroups.value = await fetchStockGroups()
    } catch {
      // 列表拉不到不影响扫描，只在面板里留空。
    }
  }

  async function refreshGroupSummaries(): Promise<void> {
    try {
      groupSummary.value = await fetchStockGroupsSummary()
    } catch {
      // 折叠态的阶段收益读不到就空着，组名/只数这些静态信息照样能看。
      groupSummary.value = null
    }
  }

  async function saveResultsAsGroup(useAll: boolean): Promise<void> {
    const picked = useAll
      ? options.results.value
      : options.results.value.filter((item) => selectedResultKeys.has(options.resultKey(item)))
    if (picked.length === 0) {
      groupError.value = useAll ? '还没有扫描结果可以保存。' : '请先勾选要保存的标的。'
      return
    }
    groupBusy.value = true
    groupError.value = ''
    try {
      const created = await createStockGroup({
        name: groupName.value.trim() || defaultGroupName(),
        items: picked.map((item) => ({
          market: item.market,
          code: item.code,
          name: item.name ?? null,
        })),
        source: 'screen',
        // 存快照而不是记 history_id：回测/扫描历史每种类型只留最近 20 条，等组还在、
        // 历史已被淘汰时，只有组里这句人话还能回答「这批票是按什么选出来的」。
        criteria: {
          summary: describeScreenForm(
            options.form,
            options.availableValues.value,
            options.metadata.value,
            options.customMetadata.value,
          ),
          payload: buildScanPayload(options.form) as unknown as Record<string, unknown>,
        },
      })
      groupName.value = ''
      selectedResultKeys.clear()
      await Promise.all([refreshStockGroups(), refreshGroupSummaries()])
      selectedGroupId.value = created.group_id
      expandedGroupIds.add(created.group_id)
      await loadGroupPerformance(created.group_id)
    } catch (error) {
      groupError.value = options.apiMessage(error, '保存失败')
    } finally {
      groupBusy.value = false
    }
  }

  function validFormulaParameters(value: unknown): Record<string, number> {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return {}
    return Object.fromEntries(
      Object.entries(value).filter((entry): entry is [string, number] => (
        typeof entry[1] === 'number' && Number.isFinite(entry[1])
      )),
    )
  }

  function handoffStockPool(
    codes: string[],
    sourceLabel: string,
    input: Record<string, unknown> = {},
    signalDate: number | null = null,
  ): void {
    const normalizedCodes = [...new Set(codes.map((code) => code.trim()).filter(Boolean))]
    if (!normalizedCodes.length) {
      options.showMessage('这组没有可回测的标的。', true)
      return
    }

    const formulaText = typeof input.formula_text === 'string'
      ? input.formula_text.trim()
      : ''
    const rankingValue = typeof input.ranking_value === 'string' && input.ranking_value.trim()
      ? input.ranking_value.trim()
      : null
    const handoff = savePortfolioBacktestHandoff({
      kind: 'stock-pool',
      source: 'formula-screen',
      source_label: sourceLabel,
      codes: normalizedCodes,
      ranking_value: rankingValue,
      rank_order: input.rank_order === 'asc' ? 'asc' : 'desc',
      mode: formulaText ? 'custom' : 'preset',
      formula_text: formulaText || null,
      formula_parameters: validFormulaParameters(input.formula_parameters),
      signal_date: signalDate,
    })
    if (!handoff) {
      options.showMessage('浏览器暂时无法保存这次跨页配置，请检查隐私模式或存储权限。', true)
      return
    }
    if (import.meta.env.MODE !== 'test') window.location.assign('/portfolio-backtest')
  }

  function handoffScreenResults(useSelected: boolean): void {
    const picked = useSelected
      ? options.results.value.filter((item) => selectedResultKeys.has(options.resultKey(item)))
      : options.results.value
    const label = useSelected
      ? `公式选股勾选结果（${picked.length} 只）`
      : `公式选股整批结果（${picked.length} 只）`
    const scanConfig: Record<string, unknown> = {
      ranking_value: options.resultMeta.value?.ranking_value ?? null,
      rank_order: options.resultMeta.value?.rank_order ?? options.displayedRankingOrder(),
    }
    if (
      options.form.mode === 'custom'
      && options.form.formulaText.trim() === options.parsedFormulaText.value
    ) {
      scanConfig.formula_text = options.form.formulaText.trim()
      scanConfig.formula_parameters = { ...options.form.formulaParameters }
    }
    const signalDates = [...new Set(picked.map((item) => item.signal_date))]
    handoffStockPool(
      picked.map((item) => `${item.market.toUpperCase()}:${item.code}`),
      label,
      scanConfig,
      signalDates.length === 1 ? signalDates[0] : null,
    )
  }

  async function handoffStockGroup(group: StockGroupSummary): Promise<void> {
    if (handoffGroupId.value) return
    handoffGroupId.value = group.group_id
    groupError.value = ''
    try {
      const detail = groupMembers.value[group.group_id] ?? await getStockGroup(group.group_id)
      groupMembers.value = { ...groupMembers.value, [group.group_id]: detail }
      const criteria = group.criteria?.payload ?? {}
      handoffStockPool(
        detail.items.map((item) => `${item.market.toUpperCase()}:${item.code}`),
        `股票组「${group.name}」`,
        criteria,
      )
    } catch (error) {
      groupError.value = options.apiMessage(error, '读取股票组失败')
    } finally {
      handoffGroupId.value = ''
    }
  }

  function defaultGroupName(): string {
    const stamp = new Date().toISOString().slice(0, 10)
    return `${stamp} 选股结果`
  }

  /**
   * 展开才去算「入组至今」。
   *
   * 这一份每组都要单独扫一遍全市场（几秒），而折叠态要的那两个数已经在
   * ``groupSummary`` 里了——先铺开信息、按需再算重活，是这页唯一能扛住「存很多组」
   * 的做法。
   */
  async function toggleGroup(group: StockGroupSummary): Promise<void> {
    const groupId = group.group_id
    if (expandedGroupIds.has(groupId)) {
      expandedGroupIds.delete(groupId)
      return
    }
    expandedGroupIds.add(groupId)
    selectedGroupId.value = groupId
    if (groupDetails.value[groupId]) return
    await loadGroupPerformance(groupId)
  }

  async function loadGroupPerformance(groupId: string): Promise<void> {
    selectedGroupId.value = groupId
    groupError.value = ''
    if (!groupId) return
    loadingGroupId.value = groupId
    try {
      // 表现和成员分两次取：成员随时可编辑，而表现要扫全市场（几秒），不该被一次
      // 编辑来回拖着重算。
      const [performance, detail] = await Promise.all([
        fetchStockGroupPerformance(groupId),
        getStockGroup(groupId),
      ])
      groupDetails.value = { ...groupDetails.value, [groupId]: performance }
      groupMembers.value = { ...groupMembers.value, [groupId]: detail }
    } catch (error) {
      groupError.value = options.apiMessage(error, '表现读取失败')
    } finally {
      loadingGroupId.value = ''
    }
  }

  async function renameGroup(group: StockGroupSummary): Promise<void> {
    const next = window.prompt('新的分组名称', group.name)?.trim()
    if (!next || next === group.name) return
    groupBusy.value = true
    try {
      await renameStockGroup(group.group_id, next)
      await refreshStockGroups()
      const cached = groupMembers.value[group.group_id]
      if (cached) groupMembers.value[group.group_id] = { ...cached, name: next }
    } catch (error) {
      groupError.value = options.apiMessage(error, '改名失败')
    } finally {
      groupBusy.value = false
    }
  }

  async function removeGroupMember(
    group: StockGroupSummary,
    member: StockGroupMember,
  ): Promise<void> {
    const groupId = group.group_id
    groupBusy.value = true
    try {
      await removeStockGroupMembers(groupId, [{ market: member.market, code: member.code }])
      // 折叠态那两个窗口收益来自 groupSummary，不是 stockGroups——只刷列表的话，
      // 移出之后这一行还是按「包括被移走那只」算出来的老数字。
      await Promise.all([refreshStockGroups(), refreshGroupSummaries()])
      await loadGroupPerformance(groupId)
    } catch (error) {
      groupError.value = options.apiMessage(error, '移除失败')
    } finally {
      groupBusy.value = false
    }
  }

  async function deleteGroup(group: StockGroupSummary): Promise<void> {
    if (
      !window.confirm(
        `删除分组「${group.name}」？它包含的 ${group.item_count} 只标的会一起丢掉。`,
      )
    ) {
      return
    }
    groupBusy.value = true
    try {
      await deleteStockGroup(group.group_id)
      expandedGroupIds.delete(group.group_id)
      if (selectedGroupId.value === group.group_id) selectedGroupId.value = ''
      const details = { ...groupDetails.value }
      delete details[group.group_id]
      groupDetails.value = details
      await refreshStockGroups()
    } catch (error) {
      groupError.value = options.apiMessage(error, '删除失败')
    } finally {
      groupBusy.value = false
    }
  }

  /** 涨红跌绿（A 股习惯）。零附近不着色——0.00% 标成红色会让人以为赚了。 */
  function toneClass(value: number | null | undefined): string {
    if (value === null || value === undefined || !Number.isFinite(value) || value === 0) return ''
    return value > 0 ? 'tone-up' : 'tone-down'
  }

  function formatPoints(value: number | null | undefined): string {
    if (value === null || value === undefined || !Number.isFinite(value)) return '—'
    return `${value >= 0 ? '+' : ''}${(value * 100).toFixed(2)} 个百分点`
  }

  return {
    stockGroups,
    groupName,
    groupBusy,
    groupError,
    handoffGroupId,
    expandedGroupIds,
    loadingGroupId,
    showAllGroups,
    selectedResultKeys,
    visibleGroups,
    selectedResultCount,
    VISIBLE_GROUP_LIMIT,
    foldedStats,
    detailList,
    memberList,
    chartList,
    groupDay,
    toggleResultPick,
    refreshStockGroups,
    refreshGroupSummaries,
    saveResultsAsGroup,
    handoffScreenResults,
    handoffStockGroup,
    toggleGroup,
    renameGroup,
    removeGroupMember,
    deleteGroup,
    toneClass,
    formatPoints,
  }
}
