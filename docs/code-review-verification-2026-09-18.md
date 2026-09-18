# 对 `docs/code-review-2026-09-18.md` 的复核结论

- 复核基准：工作树 `1401c2e`（受版本控制文件除 `app.py`/`schemas.py` 的 swing_research 接线外无改动）
- 复核方式：读代码 + 本机真实数据实测 + 子代理交叉核查
- 结论统计（共 34 条）：**属实 26 条 / 部分属实 6 条 / 不属实 2 条**
- 3 条高危（H1/H2/H3）**全部属实，且均已在本机独立复现**

---

## 一、高优先级：三条全部属实

### H1 成交量单位三路不一致 —— 属实（本机独立复现证据链）

代码事实：
- `day_format.py:91-102` `_coefficients`：股票 `volume_coefficient=0.01`，即 `.day` 原始字段 × 0.01。
- `online_market_sync.py:599-604`：东财 f56 原样入库，**无换算**。
- `online_market_sync.py:678-681`：`if not _tencent_reports_shares(...): volume *= 100`；`_tencent_reports_shares`（:335-341）只判 `SH` + `688`。

**本机实测（2026-09-18，腾讯 `newfqkline` 接口）：**

| 标的 | close | volume(原始) | amount(万元) | amount×1e4/volume | 判定 |
| --- | --- | --- | --- | --- | --- |
| sh600000 | 13.75（2025-08-05） | 1,336,466 | 181,239.99 | 1356 ≈ close×100 | **手** |
| sh688009 | 4.67 | 21,756,487 | 10,120.91 | 4.65 ≈ close | **股** |
| **sh689009** | 38.00 | 7,260,599 | 27,266.85 | 37.56 ≈ close | **股** ← 代码只判 688 |
| sh588000 | 1.74 | 46,069,653 | 802,068.66 | 174.1 ≈ close×100 | 手 |
| sz159915 | 3.39 | 18,523,198 | 624,737.56 | 337.3 ≈ close×100 | 手 |
| sh510300 / sz300750 / sh603288 / sh900932 | — | — | — | 均 ≈ close×100 | 手 |

**关键对拍**：报告给的 `.day` 同日同标的原始 volume = **133,646,594**，腾讯同日 = **1,336,466**，比值恰好 100。
→ `.day` 原始字段是「股」，×0.01 后写库为「手」；腾讯 ×100 后写库为「股」。**两条链入库后相差 100 倍，确凿。**

其他确认：
- `_has_new_data`（:1855-1875）确为严格浮点 `!=` ⇒ 供应商一切换每根 bar 判「有新数据」。
- `routers/market_data.py` 全部路由清单中**没有任何删除/回退日线的接口**，污染不可逆。
- 东财 f56：本机 `push2his` 返回 RemoteDisconnected，与报告一致地**未能实测**；「手」属合理推断。
- **报告没提到的额外问题**：`_coefficients` 内部本身也不统一 —— 沪市 fund 是 `1.0`、深市 fund 是 `0.01`、index/bond 是 `1.0`。就算不动全局口径，这几个也自相矛盾。

### H2 置换检验噪声门槛 —— 属实（假阳性率数字本机复现）

机制逐条对上：
- `_candidate_for_mask:611` `outcome_rate <= baseline_rate → return None` ⇒ 进入零分布池的**只有真实标签下 lift>1 的掩码**。
- `_permutation_max_lifts:166-169` 置换时**再次**施加 `rate <= baseline → continue` ⇒ 双重筛选。
- `threshold = np.quantile(null_max_lifts, 0.95)`（:501-503）；`significant = lift > threshold`（models.py:225-231）。
- 20 次置换时 0.95 分位 ≈ 第 2 大噪声值 ⇒ 排进前二即「显著」（≈2/21）；而 `p=(1+k)/(1+n)`（:186-187）要求严格最大才 ≤ 1/21=0.048 ⇒ **两者自相矛盾**。

**本机实测（60 个纯噪声数据集，6 特征/300 样本）：**

| 置换次数 | top1 被判显著 | 其中 p>0.05 | 名义 |
| --- | --- | --- | --- |
| 20（默认） | **7/60 = 11.7%** | 3 个（p=0.0952） | 5% |
| 200 | 3/60 = 5.0% | 0 | 5% |

→ 默认配置假阳性率是名义值的 2.3 倍，且出现「显著徽标 + p=0.095」的矛盾展示，与报告一致。

硬闸门确认：`service.py:936-937` `significant is False → failures.append("提升未超过噪声门槛")`；`_select_holdout_candidates:1306` 只收 `passed_constraints is True` ⇒ 未过门槛的候选**不做留出期复验**，用户无法覆盖。属实。

### H3 挖矿阶段峰值内存 —— 属实（本机实测比报告更严重）

- `baselines.setdefault(key, (valid, group_codes[valid]))`（mining.py:370）：key 数 = F + C(F,2)，40 特征 → **820 个 key**，每个存 `bool(n)` + `int64(n)` ≈ 9n 字节。
- `candidates` 存 `matched_global`（intp 位置数组）而非旧实现的 bool 掩码。

**本机 tracemalloc 实测：**

| 规模 | 峰值内存 |
| --- | --- |
| n=50k / 20 特征 | 156.4 MB |
| **n=100k / 40 特征** | **1131.6 MB** |

（报告报 593.4 MB，本机测得更高，可能因基线口径/数据分布不同；数量级结论一致，且**更严重**。）

- `_mine_candidates` 调用点在 `service.py:427`，位于 `as_completed` 循环**之后**，不在 `_run_phase` 的 worker 池内 ⇒ 主进程承受，内存预检（`strategy_discovery/resources.py`）管不到它。属实。
- 报告建议的「baselines 去重」确实有效：无 NaN 时所有特征的 `valid` 是同一个掩码。

---

## 二、中优先级

| 条目 | 结论 | 说明 |
| --- | --- | --- |
| M1 导入结果键名对不上 | **属实** | 后端 `imported_bars/replaced_bars/skipped_files/range_start/range_end`，前端只认 `written_bars/unchanged_files/updated_files/actual_start/end`，四组全错位 |
| M2 本地导入复用在线 scope | **属实** | 前端默认 `instrument_types=['stock']`，ETF/指数/债券被静默过滤，`filtered_files` 前端零命中 |
| M3 旧数据覆盖新数据 + 无回滚 | **属实** | `store_bars.py:262-271` 只按 (market,code,trade_date,price_basis) 删插；在线 upsert 跳过 `source='local'` 行 |
| M4 内存/取消/坏文件 | **部分属实** | (b) 取消不生效、(c) 坏文件毁整次导入**属实**；(a) 中「Future 永久持有结果」**不准** —— CPython 取走 result 后会释放，峰值 = 在途帧 + ≤64 待写帧 |
| M5 目录翻页静默截断 | **属实** | 是**另一条未修路径**：`total` 缺失时回退 `page_rows >= page_size`，而默认 `page_size=5000`、服务端硬截 100 ⇒ 只取第一页；且空页 `break` 后无累计行数校验。已有的回归测试只覆盖 `total` 存在的场景 |
| M6 范围选项悬空 | **属实** | 但措辞要修正：后端是**明确抛错**，不是「静默 0 目标」 |
| M7 `.day` 截断尾被吞 | **属实** | `tail_bytes` 全仓仅定义/计算/返回 + 测试，无生产消费方 |
| M8 桥接 null 规则静默丢弃 | **不属实（已修）** | `PortfolioBacktestPage.vue:604-611` 已有 `missing` 提示逻辑，`git log -S` 追到 **`5706dd5`**（报告自己标注为「未审」的提交）。且报告引用的 `ConditionResearchPage.vue:468-469` 是 `applyRecommendedRanking`，位置错位 |
| M9 并集校验 + `/backfill` 只补股票 | **属实** | (a) 结论成立但机制描述略偏：是 types 与 boards **两次独立差集**，不是「用并集校验」；(b) `routers/market_data.py:385-391` 硬编码 `["stock"]` 属实 |

---

## 三、低优先级

**属实（16 条）**：L2（support_ratio 分母不同且前端不消费该字段）、L3（max_dd_duration 是首峰→谷底，前端零展示）、L5（行号偏差，实际 `schemas.py:693`，前端上限 0.75）、L6（注释原文「约等价于 1/10」）、L7（`validate()` 不覆盖 rankingTopN/initialCash/佣金/滑点/公式参数）、L8（`scopeNoun` 仅 1 处、26 处硬编码「股票」）、L9（弹层被 `.table-wrap` 裁剪 + 无 aria + 键写错静默不渲染，三点全中）、L10（422 detail 数组直传 `message: string`）、L11（仅条件研究页用 `created_at`，组合回测页用 started/finished 无此问题）、L12、L13、L15、L18（含 `.env.example` 的 TDX_* ）、L20、L21、L22。

**部分属实（5 条）**：
- **L1**：obv_flow20 命名属实；**VR24 描述错误** —— `technical.py:294-304` 平盘量按 **0.5 权重同时进分子和分母** `(up+0.5*flat)/(down+0.5*flat)`，不是「只进分母记 0」。停牌/一字板走 up/down 分支，不受影响。
- **L14**：report 字段写 `"tdx"` 属实，但**入库 provider 是 `"local"`**（`store_bars.py:290`），不与在线 tdx 冲突，严重性低于报告描述；腾讯回退记 `"eastmoney"` 属实。
- **L16**：目录遍历与无 `.resolve()` 属实；但「无鉴权」是**全项目一致**的情况（`selector_app/` 内 `Depends(`/token/api_key 零命中），不该算该接口独有缺陷。
- **L17**：`turnover` 在函数内**没有**被丢弃（原样 return），是**下游无人消费**，其余 3 项（三个 fetch 函数、前端 `createMarketDataSync`、后端 `BacktestRequest`）均属实。
- **L19**：②③④属实；①「同义反复」有 `:106-119` 的硬编码期望值兜底，非完全无效；⑤已有 3 个置换检验测试，确无多 seed 假阳性率校准。

**不属实（1 条）**：
- **L4**：`engine.py:282` 的 `cash +=` **不是死赋值** —— `:307-313` 把它写回 `totals[-1]`/`equity[-1]`，而 `equity_frame` 在 `:315` 才构建，收官费用**确实进了权益曲线**。

---

## 四、报告第 4 节「已修复」清单抽样

抽查 5 条，全部属实，该节可信度较高：
- `concurrency.visible_cpu_count` 在 `condition_research/resources.py:243` 与 `strategy_discovery/resources.py:190` **两处共用** ✓
- `snapshot.py:793` 的 `_snapshot_warmup_bars` 确用 `recommended_bars` ✓
- `profiles.py:74` `profile_from_config` 确委托 `profile_from_overrides` ✓
- `mining.py:544/605` 确用 `sample_positions` 映射回全局 ✓
- `_SNAPSHOT_VALUE_COUNT` 已无命中 ✓

---

## 五、给修复顺序的建议（在原报告基础上调整）

1. **H1** 建议前置，但要先拍板一件事：**定「股」还是定「手」**。定「股」的话东财也需 ×100，而东财是主源、改动面最大；定「手」的话腾讯要去掉 ×100、`.day` 不动，改动最小但和现有注释意图相反。**无论哪种，都先补「删除/回退本地行」的运维入口**，否则已污染的库只能重建。
2. **H2** 的四项修复里，「零分布改用 label-independent 完整掩码池」是关键，单把置换次数提到 200 只能把 11.7% 压到 5%，且耗时 ×10。
3. **H3** 的 `baselines` 去重投入产出比最高（无 NaN 时所有 key 的 `valid` 同一个掩码）。
4. 中优先级里 **M5** 建议跟着 H1 一起修（都影响数据正确性）；**M8 可以从清单里划掉**。
5. 低优先级里 **L1 的 VR24 部分、L4 整条可以划掉**；**L16** 应从「本接口缺陷」降级为「全项目缺鉴权」的独立议题。
