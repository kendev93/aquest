# 筛选方案保存 / 加载（backlog #16→§4.2）

> 目标：把公式选股页上**整套可复现的输入**存成命名方案，支持加载 / 另存为 / 覆盖 / 重命名 / 删除。
> 工作量低，属于「用了就回不去」的小功能。

## 一、现状（调研结论，决定了这个功能长什么样）

- 页面可配置状态集中在 `FormulaScreenPage.vue:45-60` 的 `reactive<ScreenFormState>`：
  `mode / ruleMode / selectedSignals / conditions / combineMode / minimumMatches /
  universe / universeFile / instrumentTypes / boards / workers / period / formulaText /
  formulaParameters`。其余（`errors / loading / job / results / showAdvanced` …）是纯 UI 状态，不存。
- 页面**没有**频率 / 分组 / 排序 / 分页（backlog 原文照抄 ETFQuest，与本项目现状不符）——
  周期只有一个 `period='daily'` 选项。方案里存它，但不为不存在的字段预留。
- 已有一个半成品：**「指标组合」存在浏览器 localStorage**（`utils/formulaScreen.ts`
  的 `loadSavedConditionSets` / `saveConditionSet`）。它只存规则部分，不含标的池 / 板块 /
  并发，且清缓存即失、换浏览器不在。
- 后端已有同类先例：`saved_strategies`（策略库，DuckDB）+ `routers/strategies.py` 的 CRUD 约定。

## 二、设计决策

1. **存后端，不存 localStorage**。理由是它和策略库是同一类东西——用户的研究成果，
   不是浏览器偏好；清缓存 / 换浏览器就没了的功能不值得做。新建 `screen_presets` 表，
   CRUD 与迁移照抄 `saved_strategies` 的写法（`SCHEMA_VERSION` 15 → 16）。
2. **方案取代旧的「指标组合」**，页面上只留一套保存入口（两套并存必然让人问"我该点哪个"）。
   旧 localStorage 组合不删数据：页面检测到有旧组合时显示「导入为筛选方案」按钮，
   由用户显式点一次才写库。
3. **只存输入，不存结果**。扫描结果依赖当时行情，把结果一起存下来，下次加载的人会以为
   这些结果还成立。方案里不出现任何 result / job / summary。

## 三、四条硬口径（改动前先读这段）

1. **加载只还原设置，不自动重跑。** 加载完提示「已载入方案 X，结果需要重新扫描」。
   自动重跑会让人以为表格里的结果是方案自带的。
2. **payload 白名单裁剪 + 上限**，未知字段一律丢弃、不报错也不猜：
   `conditions ≤ 50`、`selected_signals ≤ 200`、`formula_parameters ≤ 50`、
   序列化后 JSON ≤ 32 KB。`workers` 钳到 1..8，`minimum_matches` 钳到 1..条件数。
   宁可少存一个字段，也不往库里写一堆将来没人认识的东西。
3. **名字唯一、≤ 60 字符**，重名返回 422 中文提示（照抄 `strategy_name_taken`）。
   方案条数上限 200，超了拒绝并提示先删旧的——方案是给人翻的，攒 500 个等于没有。
4. **条件引用的指标不见了要明说，不许静默替换。** 指标改名 / 自定义公式换了之后，
   方案里的 `left_value` 可能对不上当前元数据。页面现有的
   `normalizeConditionsToAvailableValues()` 会静默 fallback 成第一个可用指标——
   加载方案时必须把「N 条条件引用的指标已不存在」讲出来，否则用户以为加载的还是原来那套条件。

## 四、结构

### 后端
- `market_data/store_base.py`：`SCHEMA_VERSION` → 16，`_initialize()` 建 `screen_presets`
  （`preset_id` PK / `name` UNIQUE / `note` / `created_at` / `updated_at` / `payload_json`）。
- `market_data/store_history.py`：`SCREEN_PRESET_TABLE` + CRUD
  （`save_screen_preset` / `list_screen_presets` / `read_screen_preset` /
  `update_screen_preset` / `delete_screen_preset` / `screen_preset_name_taken`）；
  `ASSET_TABLES` 加 `screen_presets`（用户资产，导出备份要带上）。
- `market_data/store.py`：facade 转发。
- `screen_presets/payload.py`：`normalize_payload()` 白名单裁剪，返回 `(payload, notes)`。
- `screen_presets/service.py`：`ScreenPresetService`，条数上限与重名校验。
- `web/routers/screen_presets.py`：`GET /api/v1/screen-presets`（列表不含 payload）、
  `GET /{id}`、`POST`、`PATCH /{id}`、`DELETE /{id}`。异常约定照抄 strategies.py
  （`ValueError → 422`、找不到 → 404、存储异常 → 503，统一 `{"data": ...}`）。

### 前端
- `types.ts`：`ScreenPresetSummary` / `ScreenPreset`。
- `api/screenPresets.ts`：照抄 `api/savedStrategies.ts` 的封装。
- `utils/screenPresets.ts`：`buildPresetPayload()`（表单 → snake_case）、
  `presetToFormState()`（payload → 表单 + 失效条件提示）。逻辑抽到这里是为了能单测到 100%，
  页面只留响应式胶水。
- `pages/FormulaScreenPage.vue`：方案区（下拉 / 加载 / 名称输入 / 保存为新方案 /
  覆盖 / 重命名 / 删除 + 旧组合导入）。移除旧的「指标组合」工具栏与保存行。

## 五、不做

- 不做方案的导出 / 导入文件（超出「小功能」范围）。
- 不做方案分享 / 多用户（本项目无用户体系）。
- 不做「方案对比」（那是 #16 历史对比的活，且方案没有结果可比）。
- 不把扫描结果、job 状态、当前页码写进方案。

## 六、测试

- 后端单测：payload 归一化（超长 / 未知字段 / 越界钳制）、重名、条数上限、
  覆盖保存不改变 created_at；旧库迁移（v15 真实 duckdb 文件 → 打开后表存在、版本 16）。
- 后端集成：五个端点 + 422 / 404 分支。
- 前端：`utils/screenPresets.spec.ts`（payload 往返、失效条件提示）、
  `api/screenPresets.spec.ts`、页面 spec 覆盖加载 / 保存 / 重命名 / 删除。
