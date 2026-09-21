<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  deleteCandidate,
  fetchCandidates,
  fetchPublishPreview,
  publishCandidate,
  updateCandidateStatus,
} from '../api/candidateLibrary'
import { fetchMetadata, FormulaScreenApiError } from '../api/formulaScreen'
import TermHint from '../components/TermHint.vue'
import type {
  CandidateLibraryItem,
  CandidatePublishPreview,
  CandidateStatus,
  FormulaScreenMetadata,
} from '../types'
import {
  availableActions,
  evidenceRows,
  previewNeedsExit,
  previewNeedsRanking,
  publishBlockedReason,
  publishWarningText,
  sideLabel,
  significanceTone,
  significanceText,
  statusLabel,
  statusTone,
  studyRangeText,
  studyScaleText,
} from '../utils/candidateLibrary'

const FILTERS: { key: '' | CandidateStatus; label: string }[] = [
  { key: '', label: '全部' },
  { key: 'pending', label: '待定' },
  { key: 'adopted', label: '已采纳' },
  { key: 'rejected', label: '已否决' },
  { key: 'published', label: '已发布' },
]

const items = ref<CandidateLibraryItem[]>([])
const filter = ref<'' | CandidateStatus>('')
const loading = ref(false)
const message = ref('')
const messageIsError = ref(false)
const metadata = ref<FormulaScreenMetadata | null>(null)

const expandedId = ref('')
const preview = ref<CandidatePublishPreview | null>(null)
const previewLoading = ref(false)
const publishName = ref('')
const rankingValue = ref('')
const stopLossPct = ref<number | null>(null)
const publishing = ref(false)
const publishResult = ref('')

const rankableValues = computed(() => (
  metadata.value?.indicators.flatMap((indicator) => indicator.values ?? [])
    .filter((value) => value.rankable === true) ?? []
))
const needsRanking = computed(() => previewNeedsRanking(preview.value))
const needsExit = computed(() => previewNeedsExit(preview.value))

function showMessage(text: string, isError = false): void {
  message.value = text
  messageIsError.value = isError
}

function apiMessage(error: unknown): string {
  return error instanceof FormulaScreenApiError ? error.message : '候选库请求失败，请稍后重试。'
}

async function load(): Promise<void> {
  loading.value = true
  try {
    items.value = await fetchCandidates(filter.value || undefined)
  } catch (error) {
    showMessage(apiMessage(error), true)
  } finally {
    loading.value = false
  }
}

async function applyFilter(key: '' | CandidateStatus): Promise<void> {
  filter.value = key
  expandedId.value = ''
  preview.value = null
  await load()
}

async function changeStatus(item: CandidateLibraryItem, status: CandidateStatus): Promise<void> {
  try {
    await updateCandidateStatus(item.candidate_id, { status })
    const withdrawn = item.status === 'published' && status === 'adopted'
    showMessage(
      withdrawn
        ? '已撤回发布：由它生成的策略一并删除，候选回到「已采纳」。'
        : `已更新为「${statusLabel(status)}」。`,
    )
    expandedId.value = ''
    preview.value = null
    await load()
  } catch (error) {
    showMessage(apiMessage(error), true)
  }
}

async function remove(item: CandidateLibraryItem): Promise<void> {
  try {
    await deleteCandidate(item.candidate_id)
    showMessage('已删除这条候选。')
    await load()
  } catch (error) {
    showMessage(apiMessage(error), true)
  }
}

/** 展开发布区时先把"将要写什么"取回来——确认是看见了才确认，不是点了就算。 */
async function openPublish(item: CandidateLibraryItem): Promise<void> {
  if (expandedId.value === item.candidate_id) {
    expandedId.value = ''
    preview.value = null
    return
  }
  expandedId.value = item.candidate_id
  publishResult.value = ''
  publishName.value = `${item.label}｜策略`
  rankingValue.value = ''
  stopLossPct.value = null
  preview.value = null
  previewLoading.value = true
  try {
    preview.value = await fetchPublishPreview(item.candidate_id)
    // 缺离场规则时给个常见的起点，但必须让人看见并可以改——不是替他决定。
    if (previewNeedsExit(preview.value)) stopLossPct.value = 10
  } catch (error) {
    showMessage(apiMessage(error), true)
  } finally {
    previewLoading.value = false
  }
}

function blockedReason(item: CandidateLibraryItem): string {
  const reason = publishBlockedReason(
    item,
    expandedId.value === item.candidate_id ? preview.value : null,
  )
  if (!reason) return ''
  // 缺的东西能由本次输入补齐的，就不算拦着：按钮只在"现在点也发不出去"时变灰。
  if (needsRanking.value && rankingValue.value) return ''
  if (needsExit.value && stopLossPct.value !== null && stopLossPct.value > 0) return ''
  return reason
}

async function confirmPublish(item: CandidateLibraryItem): Promise<void> {
  publishing.value = true
  try {
    const outcome = await publishCandidate(item.candidate_id, {
      name: publishName.value.trim(),
      ranking_value: rankingValue.value || null,
      stop_loss_pct: stopLossPct.value === null ? null : stopLossPct.value / 100,
    })
    publishResult.value = outcome.message
    showMessage(outcome.message)
    expandedId.value = ''
    preview.value = null
    await load()
  } catch (error) {
    showMessage(apiMessage(error), true)
  } finally {
    publishing.value = false
  }
}

onMounted(async () => {
  await load()
  // 排序下拉只在"这次研究没指定排序规则"时才用得上，但仍然一并取回来：等用户
  // 打开发布区再去取，按钮就会先灰一段时间，看起来像坏了。
  try {
    metadata.value = await fetchMetadata()
  } catch {
    metadata.value = null
  }
})
</script>

<template>
  <div class="screen-page candidate-library-page" data-testid="candidate-library-page">
    <header class="topbar">
      <a class="brand" href="/indicator-lab" aria-label="指标实验室首页">
        <img class="brand-mark" src="/indicator-lab-mark.png" alt="" aria-hidden="true" width="34" height="34">
        <span><strong>Indicator Lab</strong><small>指标实验室</small></span>
      </a>
      <nav aria-label="主导航">
        <a class="nav-link" href="/indicator-lab">公式选股</a>
        <a class="nav-link" href="/portfolio-backtest">组合回测</a>
        <a class="nav-link" href="/strategy-lab">策略实验室</a>
        <a class="nav-link" href="/condition-research">买卖条件研究</a>
        <a class="nav-link" href="/strategy-fitness">策略适配性</a>
        <a class="nav-link" href="/market-data">本地行情</a>
        <a class="nav-link" href="/swing-research">波段研究</a>
        <a class="nav-link" href="/strategy-tracking">策略追踪</a>
        <a class="nav-link" href="/factor-test">因子检验</a>
        <a class="nav-link" href="/history-compare">历史对比</a>
        <a class="nav-link" href="/instrument-lab">标的检验台</a>
        <a class="nav-link active" href="/candidate-library" aria-current="page">候选库</a>
      </nav>
      <div class="topbar-status"><span class="status-dot" aria-hidden="true"></span> 本地数据模式</div>
    </header>

    <main class="content-shell">
      <section class="page-intro">
        <div>
          <p class="eyebrow">CANDIDATE LIBRARY</p>
          <h1>候选库<TermHint term="candidateLibrary" /></h1>
          <p class="intro-copy">
            研究挖出来的候选条件存在这里，带它当时的证据快照和你的表态。
            <strong>发布必须由你确认，且发布后永远是离线的。</strong>
            <TermHint term="adoptVsPublish" />
          </p>
        </div>
        <div class="intro-note">
          <span class="note-label">为什么单独存一份</span>
          <p>
            研究历史的候选编号只在一次报告里有效，而历史只留最近 20 条。
            所以入库时把证据整份复制进来——它不回指历史记录，也就不会跟着消失。
          </p>
        </div>
      </section>

      <section class="panel">
        <div class="panel-head">
          <h2>候选（{{ items.length }}）</h2>
          <div class="preset-toolbar" data-testid="candidate-filters">
            <button
              v-for="option in FILTERS"
              :key="option.key"
              class="text-button"
              type="button"
              :class="{ 'filter-active': filter === option.key }"
              :data-testid="'candidate-filter-' + (option.key || 'all')"
              @click="applyFilter(option.key)"
            >{{ option.label }}</button>
          </div>
        </div>

        <p v-if="message" :class="messageIsError ? 'field-error' : 'scope-helper'" data-testid="candidate-message">
          {{ message }}
        </p>
        <p v-if="loading" class="helper">载入中…</p>
        <div v-else-if="!items.length" class="inline-empty" data-testid="candidate-empty">
          还没有候选。到<a href="/condition-research">买卖条件研究</a>跑一次，勾选候选后点「加入候选库」。
        </div>

        <div v-else class="candidate-list">
          <article
            v-for="item in items"
            :key="item.candidate_id"
            class="candidate-card"
            :data-testid="'candidate-card-' + item.candidate_id"
          >
            <header>
              <div class="candidate-title">
                <span class="candidate-side" :data-testid="'candidate-side-' + item.candidate_id">
                  {{ sideLabel(item.side) }}
                </span>
                <strong>{{ item.label }}</strong>
                <span class="candidate-status" :class="'tone-' + statusTone(item.status)" :data-testid="'candidate-status-' + item.candidate_id">
                  {{ statusLabel(item.status) }}
                </span>
              </div>
              <span class="candidate-source">
                {{ studyRangeText(item.evidence) }}
                <template v-if="studyScaleText(item.evidence)"> · {{ studyScaleText(item.evidence) }}</template>
                · 研究 {{ item.source_history_id.slice(0, 8) }}
              </span>
            </header>

            <p class="candidate-significance" :class="'tone-' + significanceTone(item.evidence)" data-testid="'candidate-significance-' + item.candidate_id">
              {{ significanceText(item.evidence) }}
              <template v-if="item.evidence.permutation_pvalue !== null">
                （p = {{ item.evidence.permutation_pvalue.toFixed(3) }}）
              </template>
              <TermHint term="blockPermutation" />
            </p>

            <dl class="candidate-evidence">
              <div v-for="row in evidenceRows(item)" :key="row.label">
                <dt>{{ row.label }}</dt>
                <dd>{{ row.value }}</dd>
              </div>
            </dl>

            <p v-if="item.note" class="candidate-note">备注：{{ item.note }}</p>
            <p v-if="item.published_strategy_id" class="candidate-note">
              已生成策略 {{ item.published_strategy_id.slice(0, 8) }}
            </p>

            <footer class="candidate-actions">
              <button
                v-for="action in availableActions(item.status)"
                :key="action"
                class="text-button"
                type="button"
                :data-testid="'candidate-action-' + action + '-' + item.candidate_id"
                @click="changeStatus(item, action)"
              >{{ action === 'adopted' ? (item.status === 'published' ? '撤回发布' : '采纳') : action === 'rejected' ? '否决' : '恢复为待定' }}</button>
              <button
                class="secondary-button"
                type="button"
                :data-testid="'candidate-publish-open-' + item.candidate_id"
                @click="openPublish(item)"
              >发布…</button>
              <button
                class="text-button"
                type="button"
                :data-testid="'candidate-delete-' + item.candidate_id"
                @click="remove(item)"
              >删除</button>
            </footer>

            <section v-if="expandedId === item.candidate_id" class="candidate-publish" :data-testid="'candidate-publish-panel-' + item.candidate_id">
              <p class="scope-helper publish-warning" data-testid="publish-warning">{{ publishWarningText() }}</p>
              <p v-if="previewLoading" class="helper">正在拼装将要写入的配置…</p>
              <template v-else-if="preview">
                <div v-if="preview.blockers.length" class="field-error" data-testid="publish-blockers">
                  <span v-for="blocker in preview.blockers" :key="blocker">{{ blocker }}</span>
                </div>
                <ul v-if="preview.notes.length" class="publish-notes" data-testid="publish-notes">
                  <li v-for="note in preview.notes" :key="note">{{ note }}</li>
                </ul>
                <div class="preset-save-row">
                  <label :for="'publish-name-' + item.candidate_id">策略名称</label>
                  <input
                    :id="'publish-name-' + item.candidate_id"
                    v-model="publishName"
                    type="text"
                    maxlength="60"
                    :data-testid="'publish-name-' + item.candidate_id"
                  >
                </div>
                <div v-if="needsRanking" class="preset-save-row">
                  <label :for="'publish-ranking-' + item.candidate_id">排序指标</label>
                  <select
                    :id="'publish-ranking-' + item.candidate_id"
                    v-model="rankingValue"
                    :data-testid="'publish-ranking-' + item.candidate_id"
                  >
                    <option value="">请选择</option>
                    <option v-for="value in rankableValues" :key="value.id" :value="value.id">
                      {{ value.display_name }}
                    </option>
                  </select>
                </div>
                <div v-if="needsExit" class="preset-save-row">
                  <label :for="'publish-stop-' + item.candidate_id">止损比例（%）</label>
                  <input
                    :id="'publish-stop-' + item.candidate_id"
                    v-model.number="stopLossPct"
                    type="number"
                    min="1"
                    max="100"
                    step="1"
                    :data-testid="'publish-stop-loss-' + item.candidate_id"
                  >
                </div>
                <button
                  class="primary-button"
                  type="button"
                  :disabled="publishing || blockedReason(item) !== ''"
                  :data-testid="'candidate-publish-confirm-' + item.candidate_id"
                  @click="confirmPublish(item)"
                >{{ publishing ? '发布中…' : '确认发布（不会上线）' }}</button>
                <p v-if="blockedReason(item)" class="helper" data-testid="publish-blocked-reason">
                  {{ blockedReason(item) }}
                </p>
              </template>
            </section>
          </article>
        </div>

        <p v-if="publishResult" class="scope-helper" data-testid="publish-result">{{ publishResult }}</p>
      </section>
    </main>
  </div>
</template>
