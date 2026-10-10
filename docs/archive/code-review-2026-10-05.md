# Indicator Lab 全模块代码审查（2026-10-05）

## 审查范围与方式

- **范围**：`selector_app/` 全部 25 个包、约 71k 行 Python（`web/` 8.6k、`market_data/` 15.6k、`strategy_discovery/` 8.9k、`portfolio_backtest/` 5.1k、`condition_research/` 5.2k 等）。
- **方式**：静态代码审查。11 个子代理分组通读（每组限读指定文件），产出候选缺陷；由主对话对其中 **12 条**逐条回读代码复核，其余按置信度与证据采纳。
- **历史审查基准**：2026-10-07 的说明记录当时工作树位于 `feature/code-review-followup` / HEAD `01a5670`；原始审查跟进起点记为 `main` HEAD `d61bd7e`。这些均为历史上下文，不代表当前分支。
- **本轮修复基准**：本次 2026-10-08 修复位于 `codex/audit-consistency-followup`，从 `main` HEAD `7714e4c` 开始；只涉及本文标记的四项跟进，不重跑前端检查。
- **历史描述**：各条原始“位置/后果/修法”记录的是原审查时的证据，不代表当前代码；带“当前状态”的段落按本次分支和工作树解释，原行号只作为发现路径。
- **上一份** `docs/code-review-2026-09-18.md` 的 H2/H3 不再沿用旧结论：当前 `condition_research/mining.py` 的置换候选池在标签置换前构造、显著性使用 `pvalue <= alpha`、默认置换数为 200；H3 的基线去重与 uint32 编码已有部分实现。旧版 8 倍耗时/内存测量不是当前实现证据，本次没有重测。H1（成交量单位）、M5（目录翻页截断）仍记录为已修复。
- **原始审查局限（2026-10-05）**：那一轮是静态审查，未跑数据实测或交叉验证；这不代表后续跟进没有执行测试。跟进验证见修复进度中的独立记录。

**分级**：P0 = 会产生错误数字/脏数据/安全问题；P1 = 可用性、健壮性、资源；P2 = 次要口径与代码质量。✅ = 本次已回读代码确认属实。

---

## 修复进度（同日跟进）

历史验证（第三轮结束后）：`pytest --cov=selector_app` 1418 passed、覆盖率 84.73%；这是 2026-10-05 的结果，不代表本次变更后的验证。

### 2026-10-06 跟进状态

本次补齐 7 个原审查项及请求体比例透传：P0-3 在线软空响应、P0-14 留出评估、P1-1~4 取消与终态竞态、P1-8 lifespan 收尾、P1-10 窗口缓存写入、P1-15 交易日历信任、P1-19 超时部分结果，以及 P1-21 的 `train_ratio` / `validation_ratio`。具体实现和新增回归见各条目与测试。日历覆盖采用有来源的持久化覆盖区间；旧库没有可信覆盖时回填会明确跳过，不会按行情 bar 推断或删除既有事件。

P0-6 的「当前挂牌集合造成幸存者偏差」仍是待数据验证的假设：当前适配器/数据库读取路径没有直接证据证明 universe 会按当前上市状态过滤，因此本报告不把幸存者偏差列为已确认缺陷，也没有据此改 universe 或新增每日横截面披露。

P1-9 跨任务族的全局重任务预算与 P1-7 失败状态码/API 错误体需要产品级契约：现有任务族有各自的 runner、资源预检、排斥组与前端错误处理。为避免改变并发行为或把兼容客户端的失败响应改成另一套语义，本次只完成边界盘点，未作全局串行化或状态码变更。它们仍待明确资源预算/任务所有权与可演进的错误响应契约。

本次最终验证数据见 2026-10-08 记录；不沿用上一轮测试数。

### 2026-10-07 分支基线与新增数据口径

本次工作树包含后续产品提交，审查结论按当前代码解释：策略池寻优算法版本为 v6；单股候选的样本数与最长持仓硬门槛只依据训练/验证结果筛选，留出结果仍只作描述与复核；默认最长持仓为 20 个交易日。全市场增量复用只复用同配置下的 `ready` / `partial`；`no_strategy`、`insufficient_history` 与 `failed` 都会重查（单股模式仅对未变行情的 `insufficient_history` 短路复用）。老票研究区间从 `RESEARCH_START`（2016-01-01）开始，2015 年只作指标预热。不要把旧版算法、2015 年正式样本口径或旧的耗时测量当成当前行为。

P1-15 现有两条补全口径必须分开写：相邻交易日的缺口可按开盘价**估算**因子；跨停牌缺口只有启用 `verify=true` 后，才用可选、子进程隔离的 Baostock 后复权因子做**正向确认**，默认不联网、不启用。因子能确认调整比例，不证明送股数与现金分红的实际分解；当前事件仍存 `share_ratio=factor-1, cash_per_share=0`。因此这条路径不能被表述为验证了真实派息或持仓现金流，若需要准确现金流还需权益分派明细源。`verify` 测试只用 stub，不做实时网络查询。没有返回因子的候选仍保持跳过，报告为「无法确认」，不冒称权威源确认无除权。

P1-21a 的整轮取消/异常边界保留；策略池 `01a5670` 将“任务提交前 raw 行情指纹”守卫置空，因为它与单股研究内部复权指标指纹不在同一口径。结果保存研究自身的数据指纹，后续跟踪再检测变化。不要把它描述成研究期间仍有提交前/提交后的跨口径指纹对账。策略发现的快照构建时间仍不计入搜索 `timeout_seconds`；checkpoint 恢复代码会核对数据指纹、明细模式和日期前缀，原先“缺配置/区间身份”的担忧尚无反例证实。P1-19 修复了超时未完成候选的独立标记和展示。

健康检查的新“疑似漏记除权/异常跳变”条目是线索，不是确认事件；只有上述验证/人工流程才能改变公司行动表。可选 Baostock 调用不可用时维护路由返回 503，且不写部分验证结果。数据库新增 `market_trading_calendar_coverage` 元数据表，schema 初始化时创建，不删旧公司行动；既有数据库升级后不会从日期行反推可信覆盖范围，因此旧历史区间在重新取得 provider 覆盖前会 fail-closed。备份/迁移需保留这张表，否则恢复后历史回填会再次因覆盖未知而拒绝运行。

### 验证记录

- **上一轮记录（非本轮重跑）**：Python 1,505 tests passed、84.97% coverage；前端 Vitest 556 tests passed，coverage 为 85.00% statements、77.64% branches、85.88% functions、87.74% lines，`vue-tsc --build` 与 Vite production build 通过，构建有一个 bundle 超过 500 kB 的提示。日志分别记为 `/tmp/indicator-lab-python-2026-10-08-final.log` 与 `/tmp/indicator-lab-frontend-2026-10-07.log`。
- **本轮完整 Python 验证**：1,521 tests passed、85.00% total coverage，Ruff format 检查通过。首次静态门禁发现两处 import 排序问题及一个签名兼容 helper 类型错误；修正后，本轮目标回归 96 tests passed，Ruff format、Ruff 和 mypy 238 个源文件均通过。完整 pytest 日志：`/tmp/codex-audit-consistency-python-final.log`；修正后目标回归：`/tmp/codex-audit-consistency-targeted-final.log`；修正后静态门禁日志：`/tmp/codex-audit-consistency-static-final.log`。
- **浏览器页面验收**：由用户执行，本轮没有重跑浏览器 E2E。Docker 镜像构建与生产环境执行均未运行。

### 已修复

P0-2（`_has_new_data` 快照行数）、P0-4（显著门槛加 `ic_days`）、P0-5（归因 key 归一）、P0-7（回测侧缓存一并失效）、P0-8（保本单/回撤时长口径/下行差）、P0-9（过期持仓标记 `held_stale`）、P0-10（onset 窗口截断到本段）、P0-11（预热段波段不计入）、P0-13（空卖出规则当有效）、P0-15（横截面不足时跳过而非记 0）；P1-5（导入路由改同步）、P1-6（`cancelled` 可淘汰）、P1-11（在线写路径去重）、P1-12（provisional 不覆盖 completed）、P1-13（`count_many_bars` 加 price_basis）、P1-14（线程读缓存整体换 local）、P1-16（内存护栏跨平台）、P1-22（空帧列、假值兜底、公司行动缓存每轮清空）。

### 经复核推翻 / 撤回

- **P0-12 不成立**：`rolling.py` 读的 `drawdown_pct` 来自 `report.py:215`（`drawdown/peak`），与自算的 derived 同为小数，单位一致；「单位/符号混用」是误报。PF=999 与全库其它哨兵一致，保留不动。
- **P1-21a 撤回**：worker 里补捕 `TaskUserError` 会让「行情在研究期间变化」退化成单股失败，而 `test_service_aborts_if_market_data_changes_between_time_split_passes` 固定了**整轮中止**这一既定设计。代码里已就地注释说明。
- **P1-22 的 `_scanned_stocks` 撤回**：`scanned_stocks=0` 在历史记录里表示「没统计到」而非「确实扫了 0 只」，`test_library_falls_back_to_processed_for_scan_size` 固定了回退行为。同一处 `lift or -inf` 的假值问题已修（`lift` 是连续量，0 是合法值）。
- **P0-9 采用「标记」而非「丢弃」**：增量作业下多数版本 `fresh=False`，按审查建议丢弃会让模型组合几乎变空，比原问题更糟。

### 第二轮：三条回测口径已按确认结果落地

- **P0-1 已修**（口径：完整调整、现金分红入账不计税）。
  `portfolio_backtest/simulation.py` 在每个交易日的成交之前调用
  `_apply_corporate_actions`：先把每股派息按**调整前的股数**计入可用资金，再把股数
  乘以 `1 + share_ratio`，并把 `entry_price` 与 `highest_price` 按
  `(价 - 每股派息) / ratio` 换算到除权后的价格尺度；最高价若不换算，trailing stop
  会用除权前的价位去比除权后的行情，止盈会被提前触发。
  回归测试 `tests/unit/test_portfolio_backtest.py::test_positions_are_adjusted_on_ex_dividend_day`
  已存证：**禁用该逻辑时**，10 送 3 派 0.2 会让总资产在除权日从 100000 掉到 76154
  （-24%，正好对应"市值腰斩"的通俗说法），修复后总资产连续、股数为 1.3 倍。
  事件读取复用已有的标的级缓存；非批量适配器走按需查询，与 `_corporate_actions`
  同一份缓存（新增 `_corporate_actions_for_symbol`，不跨 mixin 引用）。
- **P1-17 撮合保持现状，改为写清限制**。`PORTFOLIO_BACKTEST_LIMITATIONS` 增补：
  「涨停板上的买单、停牌日的委托、以及超过当日成交量的下单都会被当作可成交，实盘
  拿不到这些成交」。原第 1 条「除权跳空只影响指标口径」已随修复改正。
- **P1-18 滑点单位保持元/股，只写明单位**。`web/schemas/portfolio.py` 的 `slippage`
  加了 `description="滑点，单位：元/股（按成交股数计）"` 并就地注释它与同一行
  `commission`（成交金额比率）不同基数。数值语义未动。

### 第三轮：买入持有基准漏算分红（实测发现，非审查项）

起因是用户截图问策略池三只票的「全段基准」准不准。实测（Docker volume
`indicator-lab_indicator-lab-data`，容器 `indicator-lab-backend-1`，
库 `/data/market/market.duckdb`，行情 2015-01-05 ~ 2026-09-30、5224 只标的）：

| 标的 | 页面显示的"全段基准" | raw 买入持有 | 复权后买入持有 |
|---|---|---|---|
| 东风股份 600006 | -8.17% | -8.17% | -0.13% |
| 白云机场 600004 | -33.39% | -33.39% | +13.40% |
| 浦发银行 600000 | -41.01% | -41.01% | +31.15% |

三个页面数字与 raw 口径**逐位吻合**，确认是漏复权：**每一次现金分红都被吞掉了**。
不是数据问题——事件表 54145 条、覆盖 5124/5224 标的（98%）、近五年每年 3000~4600 条，
`mark_adjusted` 完全可用，四处基准只是没去用它。

修复（基准一律走分红再投资口径，模拟侧一行未动）：

- `portfolio_backtest/context.py`：`_StockSeries` 新增 `adjusted_closes`，
  取自已存在的 `metric_frame`。context 里本来就为指标算好了复权序列，基准只是没取。
- `portfolio_backtest/benchmark.py`：`_price_ratio` 改用 `adjusted_closes`（缺则退回
  `closes`）；模块头写明"模拟用不复权价、基准用复权价"是两个刻意不同的口径。
  这一处同时修掉个股研究详情（`individual_research` 的 benchmark 来自
  `report.benchmark` → `_portfolio_benchmark` → `equal_weight_buy_hold`）。
- `strategy_discovery/snapshot.py`：快照数组加第 6 行 `adjusted_close`（写入端已有
  现成的 `metric_frames`，只是搬一行），`_SNAPSHOT_VERSION` 1 → 2 让旧快照重建；
  `_SnapshotBaseSeries` 读取时对旧形状兜底。策略发现页此前同样漏分红。
- `strategy_pool/service.py`：`_symbol_benchmark` 先 `mark_adjusted` 再算买入持有，
   `method` 标注"分红再投资口径"。新增 `BENCHMARK_CALIBER_VERSION` 写进落库的
   benchmark，`_prior_entries` 只把版本对得上的行算作"已有基准"——否则存量行因
   `benchmark_json IS NOT NULL` 的复用条件永远不会被重算，**改了代码页面上看不出变化**。
- `stock_groups/performance.py` + `service.py`：新增 `adjust_closes_for_dividends`，
  三处 `pivot_closes` 入口统一改成 `_adjusted_close_matrix`（长表阶段按标的批量复权），
  概览页与组内表现页一起覆盖，保持与组合回测基准同口径。事件批量查询按 500 只分块，
  取不到就退化成不复权而不是报错。

存证：`test_benchmark_credits_cash_dividends`、
`test_portfolio_benchmark_credits_cash_dividends`（端到端跑真实 service）、
`test_symbol_benchmark_credits_cash_dividends` 三条都做过反向验证——把修复换成
`if False` / 改回 `context.closes` 之后，端到端那条报 `Max relative difference: 0.125`、
策略池那条拿到 `-0.025`，正是页面上漏掉派息的那个数。

### 尚待明确或保留原行为

- **P0-6 幸存者偏差**：证据不足，当前作为待验证假设，不计入已确认缺陷。
- **P1-7 失败状态码与错误体**、**P1-9 跨任务族全局资源协调**：需要先明确 API 兼容策略和全局资源所有权/预算，再决定跨模块改动。
- **P1-19 的快照构建 deadline**、**P1-20**、以及其余 P2 观察：不属于本次已验证修复；仍需单独检查。P1-21 的整轮取消/异常行为保留为既有契约，不列为当前缺陷。checkpoint 身份错配尚未证实，当前恢复路径有指纹/日期前缀校验。

---

## P0：会导致错误结果或脏数据

### P0-1 组合回测跨除权日不调整持仓，送转让市值凭空腰斩、分红永不入账 ✅
- **位置**：`portfolio_backtest/context.py:215-226` 的注释把它写成设计选择；`portfolio_backtest/simulation.py`（1433 行）全文不出现 `share_ratio` / `cash_per_share` / `CorporateAction`，`_Position` 没有任何调整点（全包只有 `context.py` 与 `tracking.py` 提到公司行动）。
- **现状**：成交额一律用未复权价；事件表只喂给 `mark_adjusted()` 做指标口径。
- **为什么是 bug**：注释说「用 raw 成交」是对的，但它偷换了另一件事——持有者在除权日**真的会拿到送股和现金**。10 送 10 时次日 raw 价腰斩，股数不翻倍，持仓市值永久蒸发一半且不会「涨回来」；`cash_per_share` 的分红也永不进现金账户。跨越 6–7 月分红高峰的多年回测，净值系统性低估，股息率越高的票偏差越大。
- **修法**：日循环里先处理当日 `ex_date`：`size *= 1 + share_ratio`；`entry_price = (entry_price - cash_per_share) / (1 + share_ratio)`；`cash += 旧size * cash_per_share`（可按税率打折）；`highest_price` 同比例缩放。需要把 actions 通过 `_StockSeries` 透给模拟器。
- **置信度**：高（`context.py` 已经能批量取到 actions，只差往下传；测试里只覆盖「成交价用 raw」，没有一条覆盖持仓跨除权）。

### P0-2 在线同步的「有无变化」判定几乎恒为真，每轮全库重写 ✅
- **位置**：`adapters/online_market_sync.py:1376`（`count=2`）、`:2100-2118`（`_has_new_data`）、调用点 `:996-1000`、`:1529-1533`。
- **现状**：`existing.latest` 只取最后 2 根 bar，而 `_has_new_data` 遍历的是**整段回补窗口**的帧，窗口里只要有一个日期不在 `existing_by_date` 里就 `return True`。
- **后果**：① 每个标的每轮都走「updated」分支，`upsert_online_bars` 对最近窗口做 DELETE+INSERT ⇒ 全市场写放大；② 报表的 `unchanged_files` 恒为 0、`updated_files` 恒等于标的数，用户无法区分「已最新」与「真刷新」，也掩盖了真正没拉到数据的标的。
- **修法**：把「有无变化」下推到 SQL 全行比较（复用 `store_bars.py:550-582` 的 `changed_key_rows` 语义），或让快照行数 ≥ 回补窗口交易日数 + 2。
- **置信度**：高。

### P0-3 上游空响应当成「无变化 / 已完成」，`errors=0`
- **当前状态（2026-10-06）**：✅ 修复。缺失/null/格式错误的 provider payload 会触发 fallback 或可见失败，不会提交部分数据；显式有效空结果报告 `no_new_data` 且不宣称覆盖；完全没有待请求区间仍可报告 `already_complete`。已有行情在软失败时保留。见 `tests/unit/test_online_market_sync.py`。
- **位置**：`online_market_sync.py:666-680`（东财缺 `klines` 返回空帧）、`:740-745`（腾讯缺 `day` 键 `continue`）、`:1685`（`if frame.empty: break` 不记错误）、`:1523-1525`（标成 `already_complete`）。
- **后果**：停牌/退市标的、或两个源同时软失败（`{"rc":0,"data":null}` 正是同一文件里 `_request_catalog` 注释承认的限流签名）时，标的被静默跳过、`errors=0`；staging 路径下该 target 不进 `staged_bars`，merge 里彻底消失。
- **修法**：`data` 缺失即抛 `OnlineDataError`；两个源都空时记一次 failure，且不允许标 `already_complete`。
- **置信度**：中。

### P0-4 有效交易日不足仍判「显著」，并计入 significant_count
- **位置**：`factor_test/metrics.py:409-410`（不足 `MIN_IC_DAYS` 只追加一句 note）、`:579`（`significant = qvalue is not None and qvalue <= alpha`）、`factor_test/service.py:93-98,109`（按 `significant` 排序并统计）。
- **后果**：200 次置换的最小 p ≈ 0.005 < alpha，`ic_days < 60` 的因子照样打上「显著」徽章并排到榜首。归因侧是有这道闸的（`factor_attribution/metrics.py:133`），因子侧缺失，同源逻辑不一致。
- **修法**：note 非空或 `ic_days < MIN_IC_DAYS` 时 `significant = None`（照搬归因侧「未判定 ≠ False」语义），UI 不显示徽章。
- **置信度**：高。

### P0-5 因子归因的标的 key 未做大小写归一化，成交被静默剔除 ✅
- **位置**：`factor_attribution/values.py:61,91-97`（`f"{market}{code}"` 原样拼接后 `by_key.get`）、对照 `factor_attribution/service.py:169`（同一份数据却显式 `.lower()`）。
- **后果**：归档成交的 `market` 与行情列大小写不一致时，该股**全部成交**拿不到值、保持 NaN，被下游 `np.isfinite` 过滤 ⇒ 样本无声缩小；同时 `missing = needed - seen` 把这些本已读到行情的票报成「行情读不到」，误导用户去找数据问题。
- **修法**：key 统一 `str(market).strip().lower() + str(code).strip()`，`needed / seen / 帧 key` 三处同源。
- **置信度**：中（`_resolve_refs` 自己写了 `.lower()`，说明大小写混用在库里真实存在）。

### P0-6 因子面板 universe 的幸存者偏差假设（待数据验证）
- **当前状态（2026-10-06）**：未确认。当前 adapter/database 路径没有证明其按当前上市状态过滤的证据；需要用历史成分/退市证券数据验证后才能判断，暂不改 universe 或声称已存在幸存者偏差。
- **位置**：`factor_test/service.py:113-122`、`factor_test/panel.py:124-149`。
- **待验证后果**：只有当 refs 被当前上市状态过滤时，退市证券才会从历史横截面缺席并可能抬高结果；现有代码审查没有证明这一前提，也没有真实成分数据对拍。
- **待验证方案**：先用一组已退市证券和历史窗口核对 refs 与每日横截面；结果证明缺席后，再考虑用「窗口内出现过 bar」构建 universe 并披露每日有效横截面数。
- **置信度**：中。

### P0-7 第二份 `IndicatorCache` 在行情重写后不失效，回测静默用旧指标 ✅
- **位置**：`web/app.py:191`（`PortfolioBacktestService` 自带一份 `IndicatorCache(selected_store, max_memory_entries=512)`）vs `web/app.py:131-134`（`app.state.indicator_cache`）与 `web/routers/market_data/_shared.py:130-133`（只清 `app.state` 那一份）。
- **后果**：`_revisions` 是**每实例**的内存 memo。本地导入/在线重写行情（尤其「日期不变、数值变」的重写，正是 `_shared.py:118-128` 注释承认的场景）后，回测那份仍返回旧 `data_revision` ⇒ 命中旧缓存条目，回测/ML 拿到上一版行情算出的指标且不报错。表现为「重启后端才正常」。
- **修法**：全进程共用一个 `IndicatorCache`（回测侧用受限容量的包装），或让 `clear_memory()` 走进程级注册表广播。
- **置信度**：高。

### P0-8 绩效统计：保本单算亏损、回撤天数与回撤幅度不同源、999 哨兵 ✅
- **位置**：`backtest/performance.py:85-88`（`losses = ~wins`）、`:52-54` 对 `:143-151`、`:61-73`。
- **现状与后果**：
  1. `pnl == 0`（费用刚好抵消）被计入 `lose_trades`，把 0 塞进 `loss_returns` ⇒ `win_rate` 偏低、`avg_loss`/`max_loss` 被拉平、`win + lose` 口径失真；
  2. `max_drawdown` 取 `np.max(drawdown_pct)`（百分比），而 `max_dd_duration` 定位用 `np.argmax(drawdown)`（绝对金额）——先小资金深回撤、后大资金浅回撤时，报出来的回撤幅度与回撤天数描述的不是同一次回撤；
  3. `sortino` / `calmar` 用下行样本 `np.std(negative)`（丢掉非负观测的计数权重，且 `<0` 子集与 `volatility` 的 ddof=0 不同约定），单一负样本时 `np.std = 0` 直接掉进 `999.0` 哨兵，把「几乎没亏过」伪装成极强风险收益比。
- **修法**：`losses = pnl < 0` 并单列 `break_even`；duration 改用 `np.argmax(drawdown_pct)` 定位；下行差改成 `sqrt(mean(min(excess,0)**2))`；`999.0` 改为 `None`（`report._finite_number` 已会把 NaN 转 None）。
- **置信度**：高。

### P0-9 跟踪作业把过期版本的持仓无差别并入当日模型组合
- **位置**：`strategy_pool/tracking_runner.py:152-192`（`fresh` 只保护 `risk_exit_signals`，`summary["holding"]` 无条件 append）。
- **后果**：增量作业只重放部分版本，其余沿用旧快照；某只票因停牌/数据延迟/当日失败而 `as_of_date` 落后（`fresh=False`）时，它的持仓仍出现在当天结果里——前端会看到「昨天已平仓的标的还挂在当日持仓」，而同一函数的风险退出信号却用 `fresh` 拦住了，两处口径不一致。
- **修法**：持仓合并同样用 `if fresh:` 保护，或在 `project` 里带上 `as_of_date` 供上层剔除，`holding_snapshot_complete=False` 时明确降级。
- **置信度**：高（串行逻辑，增量作业是默认路径）。

### P0-10 `segment_onset` 的搜索窗口越界到后续波段
- **位置**：`swing_research/labels.py:63-86`（`travel = prices[segment.start_index:]`，未截断到 `end_index`）、调用点 `:147`、`:158-159`。
- **后果**：ZigZag 允许「推进幅度 < 阈值、回撤已达阈值」的腿，命中点落在后一段甚至无关 bar 上，`*onset` 被标在不代表该腿确认的日期；`*onset_ahead` 是唯一被标注为「实时可交易」的口径（`conditions.py:49`），错标会直接产出错误的交易条件，且 frame 变长时标签会漂移。
- **修法**：窗口限制为 `prices[start_index : end_index + 1]`，命中不到返回 `None`。
- **置信度**：中。

### P0-11 分段统计把指标预热期的波段算进研究区间
- **位置**：`individual_research/service.py:508-515`（`warm_start = window_start - atr_window*3 - 10`）、`:552-560`、`:130-137`。
- **后果**：`atr_window` 默认 14 ⇒ 预热段约两个月，预热期内存在的完整波段被计入 `up_segments/down_segments/completed_segments` 与中位数振幅。页面「分析 N 个交易日」与统计的波段样本不是同一段；改一个纯指标参数会连带改变分段结论。
- **修法**：`_segmentation_summary` 前按 `offset = window_start - warm_start` 过滤落用户区间内的 segment。
- **置信度**：中。

### P0-12 滚动本体（fitness）的回撤单位/符号混用，且零亏损时 PF=999（已复核：单位一致，属误报）
- **位置**：`strategy_fitness/rolling.py:68-69,88-94,141-149,155`。
- **后果**：记录自带 `drawdown_pct`（百分点或负值）与自算的小数口径在同一 `max` 前缀里混用，再与约束在 `[0,1]` 的阈值（`models.py:37-42`）比较 ⇒ 要么全部判不适合、要么因取值为负而恒通过；`profit_factor = 999.0` 给「样本期内没出现过亏损」白送一分。
- **修法**：统一到小数并取 `abs()`；无亏损时用 `None` + note，而非 999。
- **置信度**：中。

### P0-13 发布时把「空字典卖出规则」当有效规则，产出永不平仓的策略
- **位置**：`candidate_library/publish.py:84-93`（`_has_no_exit` 只判 `is not None`），同文件 `:109` 的 `_is_empty_rule` 却专门把 `{}` 判为「没有规则」。
- **后果**：卖出规则序列化成 `{}` 时 blocker（`:232`）不触发，发布成功，而策略实际只能靠 `close_at_end` 了结，净值与最大回撤失去意义。
- **修法**：`if not _is_empty_rule(as_mapping(base.get("sell_rule"))) or base.get("sell_signal") is not None: return False`。
- **置信度**：中（同一文件内两个函数的 none/empty 判定口径不一致）。

### P0-14 留出段没有「空段/样本不足」闸门，零成交也照出结论 ✅
- **当前状态（2026-10-07）**：服务优先用 completed/raw 行情的实际日期切分；候选 holdout 单独输出 `holdout_evaluation.status`、原因和约束结果，页面显示“通过/未通过/样本不足/无法评估”。候选入选与综合排序仍只看 validation，holdout 是描述性复验，不改 validation 的 `passed_constraints` 或排序。缺可用交易日、无成交或低于已有 `min_trades` / `min_instruments` 配置时明确标为不可评估/样本不足，不报复验成功。
- **位置**：`condition_research/time_split.py:38-43`（只要求每段 ≥ 1 自然日，且按自然日而非交易日）、`service.py:452-459`（`summarize_backtests([])` 返回全 0/None）、`service.py:375-381`（约束校验只作用于 validation，从不重算 holdout）。
- **后果**：holdout 落在假期或区间偏短时实际成交为 0，`passed_constraints` 也不会翻 False，候选带着 `holdout.closed_trades = 0` 的「复验结论」进入结果页。对照同文件滚动复验的 `MIN_WINDOW_DAYS = 30` 硬门槛，主三段反而最松。
- **修法**：按交易日历切分并给每段设最小交易日；holdout 结果也过一遍 `candidate_constraint_failures`，不足时显式写 `holdout_blocked`。
- **置信度**：中。

### P0-15 当日横截面宽度 < `top_n` 时，`top_n_lift` 恒为 0 却被当证据展示
- **位置**：`condition_research/ranking.py:38`（`MIN_CROSS_SECTION = 5`）对 `models.py:150`（`ranking_top_n` 上限 50）；`ranking.py:243-262`（`ordered_returns[:top_n]` 与 `[-top_n:]` 在 `len <= top_n` 时都退化成全样本）。
- **后果**：这些天的两个方向 lift 恰好为 0，混进均值后系统性拉低 `top_n_lift`，「能否抢到槽位」的证据变成假的 0；合格天数越少偏差越大，与「样本越少越应保守」的方向相反。
- **修法**：只把 `len(pair) > top_n` 的天计入 `daily_lift_*`，并在 evidence 里回写有效天数。
- **置信度**：中。

---

## P1：可用性、健壮性、资源

### 取消机制四处漏电（建议合并成一件事修）
- **P1-1** ✅ `screening/jobs.py` / `screening/engine.py`：排队任务可在开跑前取消；扫描在标的边界协作停止，并排空在途线程；完成/异常与取消请求在同一锁下仲裁，取消请求先到则终态为 `cancelled`。
- **P1-2** ✅ `indicators/precompute.py` 与 market-data 路由：同步、本地导入和指标预计算向计算 worker 传递取消检查，在公式/区间边界停止；已提交行情保留，取消不回滚数据，也不提前释放仍在工作的线程。
- **P1-3** ✅ `condition_research/service.py` / `mining.py`：并行阶段定期检查取消、取消未启动 futures 并等待已运行任务收尾；串行候选枚举与标签置换循环有检查点。job 在工作排空前保持 `cancelling`。
- **P1-4** ✅ `strategy_discovery/duckdb_job_store.py`：成功完成事务读取 cancel flag 并原子决定 `cancelled` 或 `completed`；取消获胜时不写入结果 payload。

### P1-5 导入路由是 `async def`，导入期间整站无响应 ✅
- **位置**：`web/routers/data_transfer.py:91`（`async def data_transfer_import`）、`:119`（同步 `import_user_assets`，含解压 + 全量 DuckDB 写入）。
- **后果**：Starlette 只对 `def` 路由启线程池，这一个 `async def` 会把同步重活直接放到事件循环上 ⇒ 几百 MB 备份导入期间所有其它请求（历史列表、任务轮询、healthz、取消请求）全部排队。
- **修法**：改成 `def`，或 `await asyncio.to_thread(import_user_assets, store, staged)`。

### P1-6 任务字典不淘汰 `cancelled`，且满 100 条后回收「已完成」任务
- **位置**：`screening/jobs.py:356-367`（可淘汰集合只有 `completed`/`failed`）。
- **后果**：① 反复「提交→取消」会让 `_jobs` 单调增长，每条还挂着 report/payload ⇒ 内存泄漏；② 第 101 个任务终止时 `pop` 掉最早一条已完成记录，用户晚几秒查 `/results` 得到 404（任务其实成功了）。

### P1-7 任务结果查询对失败状态返回 500（错误原文暴露范围需逐路由确认）
- **位置**：`routers/strategy_discovery.py:470,521`、`factor_test.py:160`、`factor_attribution.py:154`、`strategy_fitness.py:170`、`formula_screen.py:166`、`condition_research.py:235`、`portfolio_backtest.py:228`（同一模板 8 处）；`state.error` 来自 `job_backend.py:376-385` 的 `TaskUserError(str(exc))`。
- **后果**：任务完成后发现 `failed` 的结果查询会返回 500；前端与告警无法区分用户错误、资源冲突与服务故障。当前已核实部分分支会把存储错误文案回给浏览器，但「所有内部错误原文暴露」未经证实，需逐路由验证。
- **修法**：统一 409 + `error.code = "task_failed"`，真正的服务故障才 500，并对非 `TaskUserError` 文案脱敏。

### P1-8 lifespan 收尾顺序：`runner.shutdown()` 排在四个 `await` 之后 ✅
- **当前状态（2026-10-06）**：✅ `finally` 先取消并 gather 全部自有 scheduler tasks，异常以 `return_exceptions` 收集；无论收尾 task 是否异常，拥有的 runner 都会 shutdown。外部借用的 runner/store 所有权未改变。
- **位置**：`web/app.py:313-341`（`finally` 里先 `await` 四个 task，最后才 `runner.shutdown()`，且只捕 `CancelledError`）。
- **后果**：任一先抛出别的异常 ⇒ 线程池永不 shutdown，进程 hang 或留下仍在写库的 worker 线程；store 连接也没有显式释放。

### P1-9 缺全局并发闸门，跨任务族的重活会叠加跑满 executor
- **位置**：`exclusive_group` 只存在于 `condition_research.py:166`、`individual_research.py:113`、`strategies.py:314`、`market_data/*`；缺于 `portfolio_backtest.py:154`、`factor_test.py`、`factor_attribution.py`、`strategy_fitness.py`、`swing_research.py`、`instrument_lab.py`。`concurrency.py:8` `MAX_WORKERS=8` 全站共享，而资源预检只对单任务做比较。
- **当前判断**：跨任务族确有多个独立 runner/资源预检，单项配置的预算不能证明并发组合安全。旧稿的 200k×60 / 3.9GB 估计来自旧 mining 实现，当前已加入基线去重与 uint32 编码，不能用该数值代表现状；本次没有重测并发峰值。

### P1-10 窗口结果会被写进持久层指标缓存 ✅
- **当前状态（2026-10-06）**：✅ 常规 `get_or_compute` 仅在完整序列上写持久缓存；持久读取与 `preload()` 都按 revision 总长度拒绝旧窗口污染项。`persist_results` 的年度全局偏移分块写入保持原有语义。无需清空全局缓存。
- **位置**：`indicators/cache.py:814-832`（内存层有 `full_history` 保护）对 `:1088-1131`（`_persistent_put_many` 没有该保护），`data_fingerprint` 用标的级版本号、不含窗口。
- **后果**：任何以窗口 frame 调用 `get_or_compute`（默认 `persist=True`）都会落脏条目，随后同 `end`、更短 `start` 的请求会命中它，拿到「由另一个窗口预热」的序列 ⇒ 同一配置有/无缓存结果不同。
- **备注**：现有两个调用方（`portfolio_backtest/context.py:499` 显式 `persist=False`、`backtest/service.py:319` 传全序列）恰好规避，属**尚未被触发**的缺口。

### P1-11 `upsert_online_bars` 缺去重，同键双写会撞唯一约束整批回滚
- **位置**：`market_data/store_bars.py:514-517`（只有 sort），对比本地写路径 `:288-290` 有 `drop_duplicates(..., keep="last")`；唯一约束见 `store_base.py:332`。
- **后果**：同一标的落进两个 board/instrument_type 分组时 concat 出重复键 ⇒ UNIQUE 冲突 ⇒ 整事务 ROLLBACK，同批几百只票的在线数据全部丢失。

### P1-12 写路径 DELETE 关联键不含 `bar_status`，provisional 行可覆盖 completed 行
- **位置**：`store_bars.py:587-597`、`store_bars.py:732-742`；而**所有**读路径一律过滤 `bar_status='completed'`（`:840-841/1260-1261/864/905/1345-1346/1378`）；provisional 由 `online_market_sync.py:2068-2073` 的可注入时钟判定（`staging.py:543`）。
- **后果**：先以「收盘后」的 `as_of` 同步、再以「盘中」`as_of` 重放同一区间（重试 / `--as-of` / 定时任务）时，completed 行被 provisional 替换，该日对所有选股、回测、画图和 `latest_trade_date()` 瞬间消失。

### P1-13 `count_many_bars` 未按 `price_basis` 过滤，与 `read_many_bars` 口径不一致
- **位置**：`store_bars.py:1337-1351` 缺 `read_many_bars:1242-1243` 里的 `bars.price_basis = ?`。
- **后果**：存在 hfq 历史的标的出现「计数 > 0、取出来的帧为空」，上层「有数据才处理」的前置判断失真，后续指标在空帧上静默产出 NaN。

### P1-14 `clear_thread_stock_cache` 只对调用线程生效
- **位置**：`market_data/adapter.py:202-206`（清的是 `threading.local()` 的当前槽）；调用点 `strategy_pool/service.py:347-378,496-500`，而那里的 services 按线程长期缓存在 `_research_local`。
- **后果**：跨标的不会互串（命中含 `(market, code)` 比较、返回 deep copy，**这点已确认不会串票**），但「开跑前清一下」的契约对非调用线程无效；其它线程里新建的、带 `cache_stock_reads=True` 的 adapter 会拿到上一轮行情快照且无人能失效。

### P1-15 gap_backfill 用「由 bar 自身反推」的交易日历判停牌，会把复牌暴跌误补成除权 ✅
- **当前状态（2026-10-07）**：✅ `gap_backfill.py` 必须用可信覆盖完整跨越 `prev_date` 到候选日的持久化 provider 日历区间；rolling refresh 保留老 sessions，derived rebuild 与未知来源不能覆盖权威 provenance，sessions 与 coverage 同事务提交。相邻日仍是“开盘价估算”来源；跨停牌默认跳过，可选 `verify=true` 用 Baostock 因子正向确认，missing factor 只标无法确认。未覆盖时拒绝/跳过，不退回 bars 日期、不删已有事件。
- **当前位置**：`market_data/gap_backfill.py:338-395` 验证跨停牌因子并标记来源，`:398-466` 编排读覆盖、分别写估算/验证事件；`market_data/store_bars.py:940-1085` 保存/读取带来源的覆盖区间；`market_data/baostock_adjust.py` 为可选的子进程查询实现。
- **后果**：某段时间全市场普遍缺 bar 时，两根 bar 中间的「交易日」在日历里也不存在 ⇒ `missed == 0` ⇒ 长期停牌后的复牌跌幅被当成除权，`share_ratio/factor_ratio` 写进 `corporate_actions`，静默污染该股全部历史指标（量与 RECORD 不可回收）。

### P1-16 macOS / Windows 上面板内存护栏静默失效
- **位置**：`factor_test/panel.py:45-53,72-77`（读 `/proc/meminfo`，失败返回 `None` ⇒ `memory_ceiling = 0` ⇒ `if memory_ceiling > 0` 直接跳过，只剩 3GB 硬顶）。
- **后果**：当前 OS 是 darwin ⇒ 内存检查从不生效；而注释自己写着 float32 面板在 scoring 时会被复制成 float64，峰值远超 3GB ⇒ 全市场长区间运行会被 OOM kill，而不是「提前拒绝」。

### P1-17 撮合不看涨跌停/停牌/成交量，`_StockSeries` 根本没有成交量列
- **位置**：`portfolio_backtest/simulation.py:1100-1107`（`_execution_price` 无条件成交）、`context.py:87-108`（`_StockSeries` 只有 OHLC + ATR）。
- **后果**：开盘即涨停照样买入、跌停照样卖出 ⇒ 动量/强势股策略系统性高估收益（恰好总是在最难成交的票上成交）；成交量上限无法实现。`models.py:32` 已把它列为声明过的限制，是否算缺陷取决于产品取向。

### P1-18 滑点单位疑似错误（元/股 vs 成交金额比率）
- **位置**：`portfolio_backtest/simulation.py:860,1019,1093`（`slippage = size * config.slippage`，只乘股数）；同源 `backtest/engine.py:181,214,280,356`。
- **后果**：与同一行的佣金（`size * price * rate`）口径不一致；schema 只写了 `ge=0, le=1000`（`web/schemas/portfolio.py:83`），用户填 `0.001` 的意图几乎必然是「千分之一成交额」，实际只收到 0.001 元/股 ⇒ 成本被低估一个量级，参数寻优会把滑点当免费噪声。

### P1-19 strategy_discovery：中断时的半程结果处理
- **当前状态（2026-10-07，部分修复）**：✅ 超时未完成 full 复核的候选单列在 `partial_by_sharpe` / `partial_by_return`，带 `achieved_fidelity` 且 `qualified=false`；不会进入正式榜单或自动候选池。结果保留请求、研究数据指纹、算法元数据与阶段耗时。超时页面显示未完成数量与精度状态。全量 full 已评估但违反约束的候选仍是失败，不会降精度兜底。快照构建不计入搜索 `timeout_seconds`；checkpoint 身份错配没有确认，回测恢复会校验数据指纹、明细模式和已处理日期前缀。
- **当前位置**：超时部分候选在 `strategy_discovery/service.py:670-724,758-770` 组装；模拟器在 `portfolio_backtest/simulation.py:652-665` 检查 checkpoint 指纹、明细模式和日期前缀。旧静态审查关于“只有 candidate_hash、可能跨配置/区间串用”的担忧未经反例验证，不作为当前缺陷结论。快照构建时间预算仍待产品/作业预算口径决定。

### P1-20 strategy_pool：多副本调度与写入串行性
- **位置**：`strategy_pool/tracking_scheduler.py:33-94`（进程内 `_WRITE_LOCK` + SELECT→INSERT 之间无 DB 唯一约束；且它是唯一没挂在 `owns_runner` 门禁下的 scheduler，`web/app.py:302-312`）、`store.py:214-219`（reset 与其它写入没整体串行，晚到的 upsert 会把刚删空的条目写回并留下孤儿版本）、`tracking_store.py:238-263`（`save_snapshot` 的校验与 UPDATE 是两条独立 SQL，无事务包裹）。

### P1-21 condition_research：worker 异常兜底与参数透传
- **当前状态（2026-10-07）**：比例透传已修：请求 schema 保持 0.6/0.2 默认，校验每项在 0~1 且总和小于 1，并传至 split。worker 端不捕获市场变动异常以降级成单股失败，保留整轮中止的既有契约；不把旧报告中该行为再标作待修缺陷。
- **位置**：`condition_research/worker_tasks.py:136-139,173,210,250`（三处 `except (ValueError, OSError)` 漏掉 `TaskUserError`，见 `screening/jobs.py:56`）⇒ 单只股票「行情在研究期间变化」会炸掉整轮研究、已跑的成百上千只结果全部丢弃；`web/schemas/condition.py:30,120-161` + `condition_research/models.py:75-76`（`train_ratio`/`validation_ratio` 在 config 里有定义、会写进历史，但请求体 `extra="forbid"` 且 `to_config()` 不透传 ⇒ 永久锁死在 0.6/0.2，与 P0-14 叠加后用户无法补救）。**后者置信度高**：全仓库仅 `web/schemas/condition.py` 与测试构造引用。`(ValueError, OSError)` 那条置信度中高。

### P1-22 其余记账类
- `backtest/engine.py:140-149,316-318`：`prepared` 被过滤空后 `equity_frame` 无 `total` 列 ⇒ KeyError（当前唯一调用方不传日期窗口且有前置校验，故低危）。
- **当前状态（2026-10-08）**：✅ 候选与离线策略在同一 DuckDB 事务中写入；按候选 `updated_at` 做 compare-and-swap，只允许仍处于 adopted 且未变化的快照发布。任一步失败即回滚，事务内读回并返回两条记录。候选状态改变、并发重复发布与策略插入冲突均有回归覆盖。
- `stock_groups/service.py:237-302`：成员变动后曲线仍从最初入组日起算，后加入的标的在全窗口被剔除，却被归因成「新股或停牌」；被删成员的收益从整段历史消失。
- `instrument_lab/conditions.py:168,207`：`(previous.lift or -np.inf)` 与 `_as_int(...) or _as_int(...)` 的假值写法吞掉合法的 0 ⇒ 条件错序、把「0 只」显示成别的值。
- `portfolio_backtest/context.py:336-359`：`_corporate_action_cache` 挂在 mixin 实例上，非 batch 路径永不刷新 ⇒ 长驻 web 进程里补录分红后仍返回旧 actions。

---

## P2：次要观察

- `online_market_sync.py:724-776`：腾讯逐年拼接中间空洞无检测，且 `_requested_ranges:1713-1738` 只补首尾 ⇒ 中段空洞永久无法自愈。
- **当前状态（2026-10-08）**：✅ 目录分页、请求重试和退避均检查取消；目录退避使用注入的 sleep 并以短间隔等待，已排队的下一页/重试会停止。当前正在执行的网络请求仍会等待配置的请求超时后到达安全检查点。直接与 staged 规划均在目录工作前检查取消；兼容旧 provider 方法签名时通过签名检测决定是否传入取消回调。
- `online_market_sync.py:1148-1154,1419,1444,1472`：`detail_callback` 跨 8 个 worker 线程调用且无锁（`progress_lock` 只包住自增）。
- **当前状态（2026-10-08）**：✅ 每个在线 bar 行保留实际日线源，跨范围混合来源不会折叠成单一 provider；direct upsert 与 staging merge 共用逐行来源，标的汇总来源在多源时记为 `mixed`。新同步/回补可修正重叠范围内的旧标签，即使价格未变；本地行仍优先。历史库中未重取到的旧标签不做猜测。证券目录来源和日线来源分别记录。
- `factor_test/panel.py:190,234,264-282`：面板构建全程不 `sort_values("date")`（对照 `strategy_fitness/service.py:321` 显式排序），前向收益依赖「相邻行 = 相邻交易日」的前提未兜底；同处 `_forward_returns` 基准是 t 日收盘、与当日因子值同源，等价「信号与成交同刻完成」，但 `BOUNDARIES` 未声明。
- `condition_research/ranking.py:314` 对 `:331`：`sample_days` 在两个层级上口径不同（有效 IC 天数 vs 通过横截面门槛的天数）。
- **当前状态（2026-10-08）**：✅ 子集作业的分子、分母和完成摘要都按本次选中的逻辑版本计数；同一回放组共享计算但仍逐版本记账。全池规模另由 `pool_total_symbols` 提供，未选中版本的状态/持仓投影保持原逻辑。空选择不占用 worker；单版本写入失败不会把整个共用组都误记失败或让进度超过总数。
- `strategy_pool/tracking.py:60-66`：浮动盈亏用 `close/entry - 1`，未计费用/滑点，与平仓口径不可比。

---

## 已排查、未发现缺陷（供下轮少走弯路）

- `formulas/`（含 `custom.py` 求值）：除法走 `safe_divide`、空帧长度由 `_broadcast` 校验、信号由 `_signal_mask` 的 `np.isfinite` 兜底。
- `indicators/cache.py` 并发：字典操作全程在 `RLock` 内。
- `market_data/adjustment.py` 复权数学：`compute_factor_ratio` 参考价与 factor 定义、`build_factor_series` 的 `side="left"` 起算、累计连乘、同日多事件叠乘均正确（`apply_adjustment` 不动 volume/amount 是模块文档声明的选择）。
- `market_data/staging.py` merge：`_WRITE_LOCK` 串行 + `_stage_frame` 已按 key 去重 + DELETE+INSERT 幂等。
- `screening/export.py`：只做 DataFrame→JSON/CSV 字符串序列化，无文件写入与路径拼接。
- `statistics/deflated.py`：MinTRL 公式、`kurtosis(fisher=False)`、`ddof=1`、三个提前退出点与文献一致。
- `history_compare/*`、`candidate_library` 删除引用、`individual_research` 并行分片（严格按 `pairs` 原序产出，`zip(strict=True)` 成立）、`concurrency.py`/`cli.py`（线程池均有 lease 与 `shutdown(wait=True, cancel_futures=True)`）。
- `web/schemas/*`：无 alias/漏字段问题，普遍 `extra="forbid"`；写入类 SQL 全部参数化，`limit`/`page_size` 均有上限；无 WebSocket 端点。

---

## 当前后续工作

原审查提出的修复顺序是 2026-10-05 的建议，不能当成当前未修列表。已在基线提交或本轮完成的项目见上文当前状态；目前需要产品/架构决策的主要是 P1-7（任务失败 HTTP 状态码/错误体）、P1-9（跨任务族重任务资源预算）、P1-20（多副本调度/写入串行性）和 P1-19 的快照构建是否纳入搜索超时预算。P0-6 幸存者偏差仍待历史退市证券数据证明。

## 附注

原始 2026-10-05 静态审查没有运行测试，也没有改文件；那一轮的「后果」是静态推导。2026-10-08 跟进已执行上面的 Python/前端单测和静态门禁；浏览器页面验收留待用户执行。历史回测口径数据仅按各自条目所列当时证据表述，不能把旧统计或旧行号视为当前重新测量结果。

### 2026-10-08 本轮回归证据

- 候选发布：真实 DuckDB 的原子成功、CAS 冲突、策略插入失败回滚、重复发布和准备期间候选状态变化均有单测；成功响应包含 published candidate 与离线 strategy。
- 行情来源：Tencent 主源、East Money 回退、混合请求范围、direct upsert、staged merge、仅来源变化的重取和本地同日优先均有单测。检查只证明新拉取行的来源；旧数据未重新拉取时保留其原标签。
- 目录取消：页间取消、退避期间取消、取消前不发目录请求与无取消时正常重试均以注入 transport/sleep 覆盖；没有真实网络请求或等待。
- 追踪进度：部分选择、空选择、共用回放组中单项确认失败、跳过确认与预算归还均有集成回归；浏览器页面验收仍由用户执行。
- 完整 Python 与后续定向/静态验证的实际结果见上方「验证记录」；未运行真实行情网络请求、浏览器验收或生产操作。
