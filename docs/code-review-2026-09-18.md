# Indicator Lab 代码审查汇总（两轮）

- 审查范围一：`8534db4..7146282`（13 个 commit）
- 审查范围二：`1c741d8..1401c2e`（9 个 commit）
- 复核基准：工作树 `1401c2e`（受版本控制的文件无改动；未跟踪的 `selector_app/swing_research/` 不属于本次审查范围）
- 复核方式：读代码 + 真实数据实测 + 单元测试/静态检查 + 子代理对抗审查
- 状态定义：**仍存在**（已在 HEAD 上复核）/ **已修复**（已在 HEAD 上确认）/ **未复核**（仅原始报告，未在 HEAD 上重看）

## 复核时跑过的检查

| 检查 | 结果 |
| --- | --- |
| `pytest -q` | 629 passed |
| `ruff format --check` / `ruff check` | 通过 |
| `mypy selector_app` | 110 files Success |
| 前端 `npm run typecheck` / `vitest` | 通过 |
| `npx playwright test` | 12 passed（范围一当年是 1 failed） |

---

## 1. 高优先级

### H1 成交量单位三路不一致，且本地导入会永久污染正确数据（仍存在）

- **提交**：`f45bf8b`（腾讯换算）、`b492fd1`（本地导入接进写库路径）；东财路径问题为既有
- **位置**：`selector_app/market_data/day_format.py:91-102`（股票/深市基金 `volume_coefficient=0.01`）、`day_format.py:183`；`selector_app/adapters/online_market_sync.py:552-554`（东财 f56 原样入库）、`:678-681`（腾讯 ×100）、`:334-343`（只判 688）；`selector_app/market_data/store_bars.py:195-206`、`:405-412`（在线永不覆盖 `source='local'`）
- **实测证据**：
  - 真实 `sh600000.day`（hikyuu test_data，6119 条）：2025-08-05 `close=13.75`、`amount=1,812,400,000`、`raw_volume=133,646,594` → `amount/volume=13.56 ≈ close` ⇒ **.day 成交量字段单位是「股」**；经仓库导入服务写库后变成 `1,740,850`（原始 174,085,000 的 1/100）。
  - 腾讯实时接口（`amount` 折元后 `amount/volume`）：`sh600000/sz000001/sh588000/sz159915` ≈ `close×100`（手，×100 正确）；`sh688009` ≈ `close`（股，不缩放正确）；**`sh689009` ≈ `close`（股），但 `_tencent_reports_shares` 只判 688 ⇒ 被放大 100 倍**（689 属 `classify_board=="star"`，且 `_EASTMONEY_STOCK_FILTER` 的 `m:1 t:23` 会选中它）。
  - 东财 f56 未能在本机对拍（push2his 不可达），按仓库自身 alias `成交量(手)` 与 akshare 文档推断为「手」⇒ 未换算则小 100 倍。
- **后果**：同一 `volume` 列混存三种口径；`_has_new_data`（`online_market_sync.py:1855-1875`）严格浮点比较 ⇒ 供应商一切换每根 bar 都判「有新数据」反复重写；因为本地行优先级最高且**没有删除/回退本地行的接口**，点一次「导入通达信行情」会把本来正确的在线成交量永久改成 1/100，同一条序列出现 100× 断崖；量比、成交量均线、换手、绝对量阈值策略全部失真（比值型指标如 VR/OBV 流不受影响，所以很隐蔽）。
- **修复**：① 定「股」为唯一口径；② `_coefficients` 的**成交量系数全部改 1.0**（价格系数不动）；③ 东财 f56 入库前 ×100；④ 腾讯改判 `startswith(("688","689"))` 或 `classify_board(...)=="star"`；⑤ 按真实来源写 `provider`；⑥ 增加「删除/回退本地行」的运维入口，否则已污染的库只能重建。
- **需要的测试**：真实 `.day` fixture 断言「入库 volume == 原始字段」且 `amount/volume ≈ close`；同一根 bar 走三条链必须相等；689 用例（现有 `test_day_format.py:82` 把 200000→2000 写进期望值，等于把 bug 固化）。

### H2 置换检验噪声门槛偏松，且 `significant` 与展示的 p 值自相矛盾（仍存在）

- **提交**：`1401c2e`
- **位置**：`selector_app/condition_research/mining.py:611`（`outcome_rate <= baseline_rate → return None`）、`:365-374`（`record` 只登记过了该筛子的候选）、`:490-505`（用这批候选算 max-T 零分布）、`:501-505`（门槛 = null_max 的 0.95 分位）、`:186-187`（p=(1+k)/(1+n)）、`:225-231`（`significant = lift > 门槛`）；`selector_app/condition_research/models.py:83,85`（默认 20 次 / 0.95）
- **实测证据**：
  - 本人标定（纯噪声、6 特征/300 样本、120 次重复）：20 次置换误报 **15/120 = 12.5%**，200 次置换 4/60 = 6.7%（名义 5%）。
  - 子代理对照实验（8 特征/3000 样本、200 次重复）：20 次置换 **19.0%**，200 次 **13.5%**；把零分布池换成「与标签无关的完整掩码池」后降到 **7.5% / 7.0%** ⇒ 主因是**零分布只在真实标签筛出的候选子集上取最大值**（选择偏差），置换次数只解决其中一部分。
  - `significant` 与 p 值不一致（本人复现）：50 个纯噪声数据集里 5 个被判「显著」，其中 **3 个同时展示 p=0.0952 > 0.05**（子代理 60 个里 13 个显著、8 个 p>0.05）。机制：20 次置换时 0.95 分位≈第二大噪声值，排进前二即「显著」（≈2/21），而 p≤0.05 要求严格最大。
- **后果**：名义 5% 的门槛实际假阳性 12–19%，正是要挡的「纯运气条件」仍被标成「显著」，并因此进入留出期复验；同时 `significant=False` 被记成约束失败（`service.py:936-937`），而进入留出期的唯一条件是 `passed_constraints is True`（`service.py:1306`），即门槛是**硬闸门**，真实但样本量小的候选会被排除且用户无法覆盖。UI 还会出现「显著」徽标 + tooltip 里 p=0.095 的自相矛盾。
- **修复**：① 单独维护「只按 `min_support`/`min_instruments`/恒真恒假（与标签无关）筛出的掩码池」专供零分布与观察最大值；② 默认置换次数提到 ≥200（上限 500 够）；③ 只保留一个显著性口径，例如 `significant = permutation_pvalue <= 1 - significance_quantile`（或小样本门槛直接取 null_max 的最大值）；④ tooltip 把「0.95 分位」说成「纯运气最好的一次」是错的，需改措辞；⑤ 门槛建议降级为「标注 + 排序罚项」，或明确提示「被门槛拦下的候选未做留出期复验」。
- **需要的测试**：多 seed 假阳性率回归（断言 ≤8–10%）；与暴力枚举对拍 p 值；`significance_permutations>0` 时的 service 接线测试（现在唯一相关测试把检验设成 0）。

### H3 挖矿阶段峰值内存涨约 8 倍，可能打爆任务进程（仍存在）

- **提交**：`1401c2e`
- **位置**：`selector_app/condition_research/mining.py:361-374`（`candidates` 存全局位置、`baselines` 每个 key 存整条 `valid` bool + `group_codes[valid]` int64）、`:604-606`（`matched_global`）
- **实测证据**（子代理 tracemalloc）：n=100k/40 特征 **73.8MB → 593.4MB**；n=200k/60 特征 **456.6MB → 3875.4MB**；归因（n=45k/30 特征）`mining.py:373` 104.8MB、`:605` 34.7MB。旧实现每候选只存 1 字节/样本的 bool 掩码，现在存 8 字节/命中样本的 intp；`baselines` 的 key 数量随特征对增长（31 个特征 = 465 对）。
- **后果**：mining 跑在任务主进程（`selector_app/condition_research/service.py:427`，不在 `_run_phase` 的 worker 池里），而内存预检只为 worker 记账（`selector_app/strategy_discovery/resources.py`）⇒ 全市场研究可能 OOM 任务进程。
- **修复**：`baselines` 按需现算或按 `valid` 掩码去重（特征全有限时所有 key 的 `valid` 是同一个）；`matched` 改 uint32/bool；只对进入排序前 N 的候选做置换。

---

## 2. 中优先级

### M1 通达信本地导入完成后界面结果全是 0（仍存在，范围二）
`selector_app/market_data/local_import.py:113-129` 返回 `imported_bars/replaced_bars/skipped_files/range_start/range_end`，而结果面板只认 `written_bars/rows`、`unchanged_files/skipped`、`updated_files/completed`、`actual_start/end`（`web-ui/src/pages/MarketDataPage.vue:761-763`）⇒ 导入 5000 根后显示「写入 K 线 0 / 无变化 0 / 更新标的 0」。
**修复**：给本地导入单独渲染结果块，或让后端 report 复用通用键名；补「点导入→断言结果数字」的前端测试。

### M2 本地导入复用在线的 scope，默认只导入股票（仍存在，范围二）
`web-ui/src/pages/MarketDataPage.vue:536-555` 复用 `syncScopePayload()`（默认 `['stock']`，`:61`），ETF/指数/债券被静默过滤，只体现在后端 `filtered_files`，而 UI 从不展示该字段；旁边「指数和债券暂无在线数据源」对本地导入是错的。
**修复**：本地导入独立 scope（或默认全部品种），并展示 discovered/selected/filtered/imported/replaced/skipped/failure_reasons。

### M3 过期 .day 无条件覆盖较新行情，且没有回滚手段（仍存在，范围二）
`selector_app/market_data/store_bars.py:195-297` 只按 (market,code,trade_date,price_basis) 删+插，不比较来源/时间；`:405-412` 在线同步跳过 local 行。实测：在线 close=9.2 → 导入旧文件（10.0）后读回 10.0，再同步也回不来。与 H1 叠加即为永久损坏。
**修复**：加「输入日期不得早于现有最新日期 / source 优先级」护栏或开关；结果里明确替换条数；提供回退入口。

### M4 导入的内存、取消、坏文件三条都不隔离（仍存在，范围二）
`selector_app/market_data/local_import.py:182-192` 一次性 `submit` 全部文件且 Future 永久持有结果（`_WRITE_BATCH_SIZE=64` 只约束 `pending_frames`；实测 400 文件峰值 +296MB，全市场外推 1.7–3GB）；取消只在 Future 完成时轮询，`with ThreadPoolExecutor` 退出用 `shutdown(wait=True)` 不 `cancel_futures` ⇒ 点取消仍跑完；`flush()`（`:219`）在每文件 try/except 之外，单个文件含 NaN/Inf 会由 `store_base.py:625` 抛 ValueError ⇒ 整次导入失败，>64 文件时前面批次已提交，留下「半套已覆盖 + 一条通用失败」。
**修复**：分批/有界 in-flight 提交；abort 路径 `shutdown(wait=False, cancel_futures=True)`；解析阶段校验有限非负并转成文件级失败。

### M5 东方财富目录翻页可能静默截断成 100 只（仍存在，范围二）
`selector_app/adapters/online_market_sync.py:390-397` 空页直接 break 且不校验累计行数 vs `total`；`:303` 在 `total` 缺失时回退 `page_rows >= page_size`，而 `page_size=5000`、服务端每页硬截 100 ⇒ 只取第一页，任务仍报 `errors=0`。
**修复**：循环后 `if total and len(rows) < total: raise`；回退用 `min(page_size, 100)`；补 `page_size=5000 + 无 total + 100 行页` 用例。

### M6 在线同步的范围选项悬空（仍存在，范围二）
`web-ui/src/pages/MarketDataPage.vue:41-56` 仍可选 指数/债券/B股，`selector_app/adapters/market_data_sync.py:681-690` 直接报错，TDX 已删无退路（这三项对**本地导入**合法，不能简单全删）。
**修复**：在线同步下禁用/隐藏这三项并修掉「股票/B股」标签。

### M7 `.day` 截断尾巴被静默丢弃（仍存在，范围二）
`selector_app/market_data/day_format.py:151-157` 的 `tail_bytes` 全仓无消费方，报告里也没有；实测 7 字节垃圾尾 → `imported_bars=1, errors=0`。
**修复**：`tail_bytes>0` 记入 `failure_reasons` 并提示「文件可能未下载完整」。

### M8 桥接到组合回测时 `null` 规则被静默丢弃（仍存在，范围一）
`ConditionResearchPage.vue:468-469` 对「持续满仓/持有到期末」写 `null`，`PortfolioBacktestPage.vue:599-611` 用 `parseStoredRuleGroup` 返回 null 就保留自己的默认规则，提示语仍是「已载入买卖条件研究的规则与排序建议」⇒ 用户以为复现了研究基线，实际仍用组合页默认卖出条件 + 止损。
**修复**：对 null 的一侧显式提示「研究未设该侧规则，已保留当前默认，请确认」。

### M9 类型/板块按并集校验 + `/backfill` 只补股票（仍存在，范围一）
`selector_app/adapters/market_data_sync.py:683` 用 `_EASTMONEY_SUPPORTED_BOARDS` 并集校验 ⇒ `instrument_types=["fund"] + boards=["main"]` 通过校验后 0 目标、任务「成功」无解释；`selector_app/web/routers/market_data.py:388` 的 `/backfill` 仍硬编码 `instrument_types:["stock"]`，前端也不传类型 ⇒ 勾了 ETF 也补不到。
**修复**：对「类型与板块交集为空」直接报错；`/backfill` 支持类型参数。

---

## 3. 低优先级（合并）

### 数据口径 / 统计
- **L1 VR24 与 OBV 流的平盘口径**（`selector_app/formulas/technical.py:292-300`）：平盘日成交量计入分母、不计入分子（记 0 而非正量），停牌/一字板多的标的系统性偏低；`obv_flow20` 名为「资金流向」但单位是成交量占比。**仍存在**
- **L2 `support_ratio` 与界面「命中率」分母不同**：`selector_app/condition_research/mining.py:623` 用「有效子集 group 数」，界面用 `sample_totals[side].count`。目前界面只读 `support_count`，只影响直接消费该字段的 API 使用方。**仍存在**
- **L3 `max_dd_duration` 定义为「首个峰值→谷底」**（`selector_app/backtest/performance.py:150`），与「最长水下期」不同；且前端不展示该字段。**仍存在**
- **L4 `close_at_end` 的收官成本不进权益曲线**：`selector_app/backtest/engine.py:282` 的 `cash += …` 是死赋值（权益帧已生成），最后一笔平仓的佣金/印花税只体现在成交记录里。**仍存在**
- **L5 `fitness_rank_weight` 允许 1.0**（`selector_app/web/schemas.py:649`，前端最多 0.75）⇒ 指标排序被完全忽略，同分按 symbol 字典序，等价随机。**仍存在**
- **L6 置换的时间开销不进资源预算**：`selector_app/web/schemas.py:497` 注释「每次约等于 1/10 挖掘耗时」实测≈1/13；上限 500 次 × 60 特征/20 万样本外推约 40 分钟（未实测）。**仍存在**

### 前端
- **L7 数值输入缺校验**：`rankingTopN`/`initialCash`/佣金/滑点/公式参数清空后原样提交，后端返回英文 pydantic 文案且 notice 不标红（`web-ui/src/pages/ConditionResearchPage.vue` 的 `validate()` 与 `:909`）。**仍存在**
- **L8 ETF 模式文案**：`scopeNoun` 只用了 1 处（`:1039`），文件里仍有 26 处「股票」（如「最少覆盖股票数」「跨股票规则结果」「个股表现」）。**仍存在**
- **L9 TermHint**：表头弹层向上弹出（`web-ui/src/components/TermHint.vue:58`）落在 `.table-wrap`（`web-ui/src/styles.css:223` `overflow-x:auto`）内会被裁剪（CSS 推论，建议浏览器确认）；`role="tooltip"` 无 id/`aria-describedby`；`term: string` + `Record<string,…>` 写错键静默不渲染。**仍存在**
- **L10 422 提示退化**：FastAPI `detail` 是数组，直接传给 `FormulaScreenApiError(message: string)`（`web-ui/src/api/marketData.ts:38-42`）⇒ 显示 `[object Object]`；`vipdoc_path` 输入框无 maxlength。**仍存在**
- **L11 历史下拉显示 `created_at`（= started_at）而列表按 `finished_at DESC` 排序**（`selector_app/market_data/store_history.py:736`、`:809`）⇒ 长任务显示时间明显早于完成时间。**仍存在**

### 本地导入
- **L12 盘中导入丢当日 bar 并误报**：`local_adapter.py:307` 只写 completed，只含当日的文件会记成 `empty_day_file`（应提示「当日未收盘」）。**仍存在**
- **L13 不支持北交所 `bj/lday`**（`day_format.py:200-213`，`store_base.py:605` 只收 SH/SZ），只挂 bj 目录时 `discovered_files=0` 且不解释。**仍存在**
- **L14 `provider` 记名混乱**：本地导入写 `"tdx"`（与已删除的在线通道同名，`local_import.py:115`），腾讯回退写 `"eastmoney"`（`online_market_sync.py:1349`）⇒ 量纲问题在库里不可诊断。**仍存在**
- **L15 无增量跳过**：`local_import.py:109-112` 每次全量解析+全删插（旧 `LocalDayImporter` 有 size/mtime 跳过）。**仍存在**
- **L16 detect 接口暴露目录清单**：`routers/market_data.py:482-501` 无鉴权依赖，遍历 /host、/data、/mnt、/media；`map_host_path`（`local_adapter.py:55-71`）不做 `.resolve()`（下游有文件名强校验，不构成任意文件读取）。**仍存在**

### 组合回测「指定标的 / 固定股数」（范围二）
- **L20 `codes` 宇宙静默丢代码**：`selector_app/portfolio_backtest/context.py:287-320` 只有在「一个都解析不出来」时才报错（`:307`）；部分代码找不到、或被证券类型/板块过滤掉的代码无声消失，报告只显示解析后的数量。**修复**：把 missing/被过滤的代码作为诊断返回。**仍存在**
- **L21 结果里看不到建仓方式**：`PortfolioBacktestReport.to_dict`/`_risk_controls` 没有 `position_mode/fixed_size/codes`（请求快照里有，历史能还原），结果页无法显示「固定股数 1000」；汇总徽标对指定标的显示成 `CODES`（`web-ui/src/pages/PortfolioBacktestPage.vue:1022`）。**修复**：结果与 `risk_controls` 带上建仓方式/代码数，徽标显示「指定标的」。**仍存在**
- **L22 `fixed_size` 校验语义不一致**：`selector_app/web/schemas.py:649-650` 只要求 ≥100，`selector_app/portfolio_backtest/simulation.py:498-518` 用 `//lot* lot` 静默取整，而条件研究那边（`selector_app/condition_research/models.py:140-143`）直接拒绝非 100 倍数。**修复**：统一为「拒绝非整百」或「文档明确会向下取整」。**仍存在**

### 死代码 / 文档 / 测试
- **L17 死代码**：`turnover` 换算了但列被丢弃（`online_market_sync.py:680`，测试却断言 0.012）；`fetch_daily_history_for_asset`/`fetch_lof_instruments`/`fetch_index_instruments` 只剩测试调用；前端 `createMarketDataSync` 无生产调用；后端 `BacktestRequest` schema 已无路由（仅测试引用）。**仍存在**
- **L18 文档/类型残留**：`MarketDataProvider` 仍含 `"tdx"`（`selector_app/market_data/models.py:14`，运行期已拒绝）；`.env.example` 仍列 `TDX_HOST/PORT/KNOWN_HOSTS`；`docs/architecture.md:34` 仍说「不提供本地行情文件导入」；`docs/upstream-api-audit.md:23` 引用不存在的 `_fetch_catalog`；删导航后 6 个页面有错位缩进。**仍存在**
- **L19 测试质量**：`tests/unit/test_backtest_engine.py:96-104` 的「参考实现」与生产同表达式（对 duration 是同义反复）；`ConditionResearchPage.spec.ts` 有用例名与断言相反；`TermHint.spec.ts` 有恒真断言；导入测试从不校验 volume/amount；mining 无假阳性率校准测试。**仍存在**

---

## 4. 已修复（不要再照原报告改）

以下来自范围一报告，已在 HEAD 上逐条确认修复：

| 原问题 | 现状 |
| --- | --- |
| 前 N 名槽位诊断用错下标空间（`7146282`） | 已修：`mining.py` 用 `sample_positions`/`flatnonzero(valid)` 映射回全局；子代理 200/200 随机试验与暴力实现一致 |
| ETF 成本两套解析、只改佣金时被收股票印花税（`7534cc2`） | 已修：`profiles.py:56-82` 的 `profile_from_config` 改为委托 `profile_from_overrides`（逐项判定），README 同步 |
| 长预热指标在策略发现快照路径失效（`806ffd1`/`2fdde96`） | 已修：`snapshot.py` 的 `_snapshot_warmup_bars` 改用 `recommended_bars` |
| `annual_trend24` 无上限外推（`806ffd1`） | 已修：`technical.py` 对 `trend_slope*252` 做上下限裁剪 |
| 策略实验室 worker 不感知 CPU 亲和性（`c42d9bb` 只修了一半） | 已修：抽出 `concurrency.visible_cpu_count`，两处共用（`test_strategy_discovery_resources.py` 也改成 patch 它） |
| 沪市 ETF 日线下载市场前缀错（`8a70a24`） | 已修：`fetch_daily_history` 改用调用方传入的 market（`online_market_sync.py:498`） |
| 研究→组合回测桥接不传日期区间（`59dc589`） | 已修：payload 带 `date_mode`，消费端同步 `form.dateMode` |
| 快照预检内存常量 21 与实际 44 列脱节（`8a70a24`） | 已修：`_SNAPSHOT_VALUE_COUNT` 已移除，`estimate_discovery_resources` 由 catalog 推导 |
| e2e 红灯（严格模式标题重复 + 过期 payload 断言） | 已修：`npx playwright test` 12 passed |
| 卖出候选「加入基础规则并重测」用 AND（与后端 OR 冲突） | 已修：`ConditionResearchPage.vue:643` 对卖出置 `combine_mode:'any'` |
| 卖出候选的「前 N 名胜率」提示语错误 | 已修：`topNTitle` 对卖出候选给出专门说明 |
| 结果分页不复位（旧页码 + 空表体） | 已修：新增 `watch(() => report.value, () => stockPage.value = 1)` |

> 说明：这些多数由 `5706dd5`（范围一与范围二之间的「季度版本迭代」，我当时明确标注未审）修复。若要补全链条，建议单独审一遍该提交。

---

## 5. 建议修复顺序

1. **H1 成交量口径**：先定「股」，再改 `_coefficients` 成交量系数 + 东财 ×100 + 腾讯 689；同时补「删除/回退本地行」入口，否则已污染的库只能重建。
2. **H2 置换检验**：零分布改用 label-independent 完整池 + 默认次数 ≥200 + 统一 `significant`/p 口径 + tooltip 措辞 + 校准测试。
3. **H3 挖矿内存**：`baselines` 去重/按需、`matched` 改 uint32、只对靠前候选做置换。
4. **M1/M2/M4**（导入结果展示、scope、内存/取消/坏文件）与 **M3**（覆盖护栏）——决定这个新功能能否安全跑全市场。
5. **M5/M6/M7/M8/M9**，再到低优先级清单；L19 的测试质量问题建议跟随对应修复一起补。

---

## 附：逐提交索引

| commit | 相关发现 |
| --- | --- |
| `8534db4` | 未发现问题（回撤持续期修复正确） |
| `8a70a24` | 已修：市场前缀；L2/L6 等既有项 |
| `c42d9bb` | 已修：CPU 亲和性 |
| `7534cc2` | 已修：ETF 费率 |
| `d3ebd3b` | 加权排序实现与文档一致，未发现新问题 |
| `f01e3be`/`806ffd1`/`2fdde96`/`4af499f` | 已修：快照预热、年化趋势封顶；L1 |
| `59dc589` | 已修：桥接日期；M8 仍存在 |
| `9d2c6c6` | 单边规则语义正确 |
| `d478931` | 已修：卖出候选合并 OR |
| `7146282` | 已修：前 N 名下标、卖出候选提示语 |
| `5706dd5`（未审） | 修掉了范围一多数高/中优项 |
| `1c741d8` | 删除干净；结果分页复位已修；L20–L22 仍存在 |
| `1d8d41f` | 测试修正有效 |
| `1dee56a`/`ce11271` | TDX 删除彻底；M5、M6、L17 相关 |
| `f45bf8b` | H1 的腾讯侧（689）、L17 |
| `b492fd1` | H1 的本地导入侧、M1–M4、M7、L12–L16 |
| `1401c2e` | H2、H3、L6、L19 |
