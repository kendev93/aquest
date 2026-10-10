# 策略追踪（#1）实施方案

> 2026-09-21 起草。候选池 `docs/feature-backlog.md` §3.1。
> 两项前置（策略持久化、基准对照）已于 2026-09-21 上线，本文只覆盖**剩下的部分**。

---

## 一、它要回答的问题

现在所有能力都是一次性的：挖条件 → 回测 → 看结果 → 结束。它回答"历史上这套东西好不好"。
策略追踪回答另一个问题：**从今天起，它还灵不灵？**

我们做置换门槛、BH-FDR、因果无前视适配，全部是为了提高「结论在未来也成立」的概率，但产品里**没有任何东西去验证这个概率**。策略追踪就是那个验证器。

**核心看点只有两个，都是可证伪的**（不是收益曲线本身）：

1. **衰减比** = 观察段指标 / 研究段指标（年化、夏普各一个）。只展示，不给硬性判定阈值。
2. **观察段 vs 基准** = 观察段内策略收益 vs 同期标的池等权买入持有（复用已上线的 `benchmark.py`）。

多个策略并排时，再给一个**整体衰减分布**：「追踪的 N 个策略里，M 个在样本外衰减超过一半」。

---

## 二、重算路线：为什么最后选了「全量重跑」

> ⚠️ **本节推翻了起草时的判断。** 原以为可以靠 `SimulationCheckpoint` 把每日成本压到 O(新增天数)，读代码后确认**走不通**，理由记在下面，别再回头试。

### 2.1 那个走不通的想法

`portfolio_backtest/simulation.py` 确实接受 `SimulationCheckpoint`：它把 cash、持仓、挂单、冷却期、回撤、以及已输出的 equity/trades/states 行全部序列化，续跑时从 `last_date` 的下一天开始。

但它有一道 **行情指纹校验**（`_checkpoint_start_index`）：

```python
if checkpoint.data_fingerprint and data_fingerprint:
    if checkpoint.data_fingerprint != data_fingerprint:
        raise ValueError("模拟检查点行情指纹不匹配")
elif checkpoint.data_fingerprint != data_fingerprint:
    raise ValueError("模拟检查点缺少可验证的行情指纹")
```

而 `data_fingerprint` 是 `_portfolio_fingerprint(contexts)` —— 对**每只股票的行情帧**算哈希再汇总（`context.py`：`data_fingerprint = frame_fingerprint(frame)`）。

**每天收盘后行情都会多一根 bar，所以每只股票的帧都变了，指纹必然不匹配。** 这个 checkpoint 在 `strategy_discovery` 里能用，是因为那边全程共用同一份只读 `DiscoverySnapshot`，数据压根不会变——它生来就是「同一份数据内的断点续跑」，不是「跨数据版本的增量续跑」。

另外还有一道更硬的约束：`processed_dates` 必须恒等于 `dates[:len(processed_dates)]`，也就是必须是**从回测起点开始的完整前缀**。想造假绕过去就得塞一整段占位的 equity/states 行，账面上的好处（少算几天模拟）远不够抵收益。

**结论：续跑路线被指纹校验挡死，除非改引擎本身。**

### 2.2 追踪账户的起点

策略追踪分开记录两件事：

1. **研究参考**：按保存策略时固化的日期区间重放，冻结其绩效摘要。它只用于和上线后的表现对照。
2. **观察账户**：用户开始追踪时，以行情库最新可用交易日为上线日，用策略原设初始资金和空仓状态起跑。上线前的 K 线只用于计算均线等指标的预热值，不参与账户交易或盈亏。

每日重算沿用同一观察账户起点，从上线日重新模拟到最新交易日。因此漏跑的日期可以补齐，同一天重复重算也不会把历史研究收益带入观察账户。研究区间和观察区间可以不连续：用户保存策略后过一段时间才上线时，中间日期既不并入研究绩效，也不算作上线观察。

### 2.3 全量重算

追踪重算不使用跨行情版本的 `SimulationCheckpoint`。行情帧每天变化，指纹校验会拒绝旧检查点；每次从上线日重新模拟既保持确定性，也保证空仓初态一致。指标上下文仍读取上线日前的预热 K 线，但模拟器只收到上线日及之后的交易日期。

---

## 三、数据模型

### 3.1 `saved_strategies` 扩列（SCHEMA_VERSION 14 → 15）

| 列 | 类型 | 说明 |
| :--- | :--- | :--- |
| `tracking_status` | VARCHAR DEFAULT 'off' | `off` / `active` |
| `launched_at` | DATE | 用户开始追踪时，行情库最新可用的交易日 |
| `last_tracked_date` | INTEGER | YYYYMMDD，最后成功重算的交易日 |
| `last_error` | VARCHAR DEFAULT '' | 失败原因，失败时**停在旧日期**，绝不静默写"成功" |

迁移用现有的 `_migrate_schema` + `ALTER TABLE ADD COLUMN`（已有 `board`、`provider` 等先例）。

> ⚠️ **成本提醒**：bump `SCHEMA_VERSION` 会在下次启动触发整库备份，当前库 4.6GB 级。执行一次，之后不再触发。

### 3.2 新增 `strategy_tracking`

```sql
CREATE TABLE IF NOT EXISTS strategy_tracking (
    strategy_id     VARCHAR NOT NULL,
    trade_date      INTEGER NOT NULL,   -- YYYYMMDD
    equity          DOUBLE NOT NULL,
    cash            DOUBLE NOT NULL,
    positions_count INTEGER NOT NULL,
    benchmark_value DOUBLE,             -- 同期基准净值（用于观察段 vs 基准）
    drawdown        DOUBLE,
    PRIMARY KEY (strategy_id, trade_date)
)
```

主键 `(strategy_id, trade_date)` + **upsert** ⇒ 幂等：同一天跑几次结果一致，漏跑几天也能一次补齐。
只存每日一行净值，**不存交易明细**（明细在需要时从初态重算）。

放进 `ASSET_TABLES`，随用户资产备份导出。

---

## 四、重算服务

新模块 `selector_app/portfolio_backtest/tracking.py`：

```
run_tracking(strategy_id, *, store, service) -> TrackingResult
  1. 读策略 → 校验 tracking_status == active
  2. 研究参考按策略保存的固化区间回放；绩效摘要只冻结一次
  3. 上线日取当前行情库最新可用交易日
  4. request_json 的 start_date 改为上线日，end_date 改为最新交易日
  5. 准备含上线日前预热 K 线的 contexts；模拟器从上线日以 initial_cash 和空仓运行
  6. 取上线日起的观察曲线 → upsert 进 strategy_tracking
  7. 更新 last_tracked_date，清空 last_error
```

失败路径：捕获异常 → 写 `last_error` → **保留旧的 `last_tracked_date`** → 返回失败。第二天继续尝试补跑。

**上线流程**（用户点"开始追踪"）：
1. 用固化的研究区间回测并冻结研究段摘要，作为衰减比的分母
2. 取最新可用行情日作为 `launched_at`
3. 以该日为模拟起点，使用策略原设初始资金和空仓状态建立追踪账户
4. `tracking_status = active`，记录上线日的初始净值、现金和持仓数（初始为 0）

---

## 五、调度

仿 `AutomaticMarketSyncScheduler`，新增 `StrategyTrackingScheduler`：

- 每 15 分钟检查一次（与行情同步同频）
- 触发条件（全部满足才跑）：
  1. 存在 `tracking_status = 'active'` 的策略
  2. **没有活跃的数据任务**（`ACTIVE_DATA_JOB_DESCRIPTIONS`）——重算必须在行情更新之后，顺序反了会算出空结果
  3. 库中最新交易日 `> last_tracked_date`（同一天不重复跑）
- 串行处理每个 active 策略（组合回测吃内存，不能并发）

⚠️ 用户是本地部署，机器关机就会漏跑。**所以幂等补跑 + 手动「立即重算」按钮是刚需，不是可选项。**

---

## 六、前端

新页面 `/strategy-tracking`（`App.vue` 加路由 + 侧栏入口）。前端无图表库，净值曲线手写 SVG（参考 `MarketCandlestickChart.vue`）。

**总览表**（一行一策略）：名称 / 上线日 / 追踪天数 / 研究段年化·夏普 / 观察段年化·夏普 / 衰减比 / 观察段相对基准 / 当前持仓数 / 状态

**详情**（展开）：分段净值曲线（上线日画竖线切分，研究段与观察段用不同颜色）+ 当前持仓 + 观察段交易明细

**上线入口**：策略库每行加「开始追踪」（现在策略库在组合回测页内嵌）。追踪中显示「停止」。

两个必要的诚实提示：
- **观察段不足 20 个交易日时，衰减比旁边标注"样本过短，仅供参考"**——只跑 3 天的年化是噪声，不能让用户当成结论。
- 追踪期间标的退市/停牌要在持仓里标记，否则幸存者偏差会污染结论。

---

## 七、MVP 边界

**做**：开始/停止追踪、手动立即重算、每日自动重算、总览表、详情（分段曲线 + 持仓）、衰减比、观察段 vs 基准、整体衰减分布。

**不做**（留到完整版）：
- 暂停/恢复（只有 off/active 两态；要改规则 → 停掉这个，新开一个）
- 交易明细存库（按需从初态重算即可）
- 推送 / 提醒 / 导出
- #18 策略组合

**硬约束**：**已上线的策略不允许改规则。**改了就不是样本外了——策略库里已有的"改名"只改名称，不动 `request_json`。要改规则就是新开一个追踪。

---

## 八、验收标准

1. 保存策略 → 开始追踪 → 总览表出现该行，研究段指标与原始回测一致
2. 手动「立即重算」→ 观察段多出一行净值；**再点一次，结果不变**（幂等）
3. 模拟"漏跑三天"→ 手动重算一次补齐三天（补跑）
4. 故意让某策略的配置失效 → 状态停留在旧日期并显示错误，**不写成功**
5. 研究段指标在多次重算后**始终不变**（冻结）
6. 后端 mypy strict 通过、前后端测试通过、覆盖率不跌
