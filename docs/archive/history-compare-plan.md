# 回测历史对比（#16）方案

> 目标：把 2~5 条历史记录摆在一起看——曲线归一叠绘 + 指标并排 + 口径警告。
> 数据层已经够（`backtest_history` 全量存 `result_json`），缺的只是取数、归一化和叠绘。

---

## 一、它回答什么问题

研究这件事本身是「反复做实验 + 比较实验」。做完 5 次组合回测、5 次条件研究，
现在没有任何地方能把它们摆在一起看。

对比页要回答两个问题：

1. **同类型多次实验**：改了一个参数/换了一个条件，这次比上次好在哪？（曲线叠绘 + 绩效并排）
2. **跨类型**：条件研究挖出的条件 A vs 波段研究的结论 B，谁更值得往下走？（只能比规模与显著性，见 §三）

---

## 二、现状（调研结论）

| 类型 | 曲线 | 顶层 `performance` | 区间字段 |
| :--- | :--- | :--- | :--- |
| `portfolio` | **有** `equity_curve[].total`（`date` 为 `YYYY-MM-DD` 字符串）+ `benchmark.totals`（`dates` 为 int YYYYMMDD） | 有（22 键，含 `start_cash`） | `start_date` / `end_date` + `bars` |
| `strategy_fitness` | 无 | 无，逐标的 × train/validation/test 三段 | 顶层 + 每条 `data_start/data_end` |
| `condition_research` | `equity_curve` 是**硬编码空数组** | 无，候选内 `validation/holdout/full_period` 的 `pooled_trades`/`per_stock` 中位数 | `start_date`/`end_date` + `splits` |
| `swing_research` | 无 | 无，只有 IC 类标量 | `date_range{start,end,days}` |
| `factor_test` | 有，但**不是净值**：顶层 `dates[]` + 每个 factor 的 `ic_cumulative[]` / `long_short.cumulative[]` | 无 | `date_range` + `dates` |
| `single_stock` | 存量数据可能有 | 可能有 | 同 portfolio 形状 |

- 写入是全量 `dict(result)`（`store_history.py:1043`），被裁剪的只有 `summary_json`——
  曲线**不在**摘要里，必须从 `read_backtest_history` 的完整 result 取。
- `single_stock` 已无写入方（`../architecture.md:53`），只可能是存量，只读兼容。
- 前端 `BacktestHistoryType` 漏了 `factor_test`，本轮补上。

---

## 三、三条硬口径（防误导，改不得）

### 3.1 不同口径的曲线绝不叠绘

曲线口径只有三种：

| kind | 含义 | 单位 | 归一化 |
| :--- | :--- | :--- | :--- |
| `equity` | 组合/单票资金曲线 | 累计收益率 % | 除以本金 |
| `factor_long_short` | 因子多空累计价差 | 价差（收益差） | **不归一**（无本金概念） |
| `factor_ic` | 因子累计 IC | 无量纲 | **不归一** |

选中记录里只叠绘**同口径**的子集，其余标注「曲线口径不同，未参与叠绘」，
但仍出现在指标表里。同口径不足 2 条 ⇒ 不叠绘，只画单条并说明。

### 3.2 净值归一化只除以本金

- 默认 `capital`：`value / start_cash`（`performance.start_cash`，取不到再看 request）。
- 取不到本金才退 `first`（除以首值），并在图上写明用了哪种。

不用「各自除以自己首值」当默认：本金不同的两次实验，收益率口径才是可比的。

### 3.3 跨类型比收益＝定性参照，不给排名、不给综合评分

统一指标表的每一格都带 `source`（中文口径来源）。
跨类型时顶部挂警告：**收益类数字口径不同，只能定性参照**。
`strategy_fitness` 取的是「逐标的 test 段中位数」，`condition_research` 取的是
「最强候选的验证段 pooled 交易」——它们都不是资金曲线收益，绝不排在一列里比大小。

---

## 四、指标并排

**统一语义行**（取不到显示「—」+ 悬停说明原因）：
总收益、年化收益、最大回撤、夏普、胜率、交易笔数、样本交易日、标的数、显著个数。

**各类型专属卡片**：每条记录另给 3~4 个自己那类特有的数字
（条件研究：候选数/合格数/最强提升/基线胜率；波段：噪声门槛/最强 IC/最强指标名；
因子：最强因子/IC/多空价差/价差胜率；适配性：通过率/逐标的中位数）。

诚实结论：**跨类型只能比规模（样本天数、标的数）与显著性，收益类只有同类型可比。**
这个结论本身要写在页面上，不要靠用户自己悟。

---

## 五、可比性警告（service 生成，前端只负责展示）

1. 曲线口径不一致 ⇒ 说明叠绘了哪几条、哪几条被排除。
2. 日期区间无交集 ⇒ 「区间不重叠，只能看形状，不能比较同期表现」。
3. 类型混合 ⇒ 「收益类口径不同，只能定性参照」。
4. 样本天数相差 ≥3 倍 ⇒ 提示长度差 N 倍（长样本的回撤天然更容易暴露）。
5. 曲线点数 < 2 ⇒ 不画，说明原因。

x 轴两种模式：`date`（默认，按并集日期对齐）/ `index`（相对交易日序号，看节奏）。

---

## 六、后端结构

- `selector_app/history_compare/`
  - `models.py`：曲线口径常量、归一化与 x 轴模式、`CompareMetric`/`CompareItem`/`HistoryCompareReport` 数据结构
  - `extract.py`：按类型抽曲线与指标（含 `single_stock` 只读兼容）
  - `service.py`：批量读取 + 组装报告 + 生成警告
- `market_data/store_history.py` 新增：
  - `list_backtest_history_all(limit_per_type)`：跨类型一次查询
  - `read_backtest_histories(ids)`：一次连接批量读完整记录（曲线只在完整 result 里）
- `web/routers/history_compare.py`：`GET /api/v1/history-compare/candidates`、
  `POST /api/v1/history-compare`（body：`history_ids` / `normalize` / `x_axis`）
- `web/schemas.py`：`HistoryCompareRequest`（2~5 条，去重后校验）
- `web/app.py` 挂载。

---

## 七、前端结构

- `web-ui/src/api/historyCompare.ts`
- `web-ui/src/utils/chart.ts`：抽出 `chartGeometry`（多序列共用纵轴 → points 字符串），
  来源 `StrategyTrackingPage.vue:162-188`，两处共用避免两份坐标换算。
- `web-ui/src/pages/HistoryComparePage.vue`：候选勾选（按类型分组，最多 5 条）
  → 曲线图（手写 SVG）+ 指标表 + 专属指标卡 + 警告区。支持 `?ids=a,b` 预选。
- 历史面板加「对比」入口：跳 `/history-compare?ids=<id>`。
- `App.vue` 路由映射 + 8 个页面 nav 各加一项（nav 每页各写一份，别漏）。

---

## 八、测试

- 单测：曲线抽取（三类 + 空/单点/无本金）、归一化两种模式、指标抽取逐类型、
  警告生成（口径不一致、区间不重叠、类型混合、长度差 3 倍）。
- 集成：candidates 端点、compare 端点（含 404/422）。
- 前端：`HistoryComparePage.spec.ts`（渲染 + 叠绘 + 警告）、`api/historyCompare.spec.ts`。

---

## 九、不做的事

- **不做综合评分**（不可证伪，门槛 2）。
- **不重跑任何回测**：只读历史，跑不动就明说。
- **不把 factor 的累计 IC 当净值画**（口径不同，见 §3.1）。
- 不新增历史类型、不动 `SCHEMA_VERSION`。
