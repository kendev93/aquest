# 标的检验台（#6，P1）

> 来源：候选池 §4.1。原名「ETF 分析」，改名有两个原因：我们只做股票，叫 ETF 是错的；
> 原功能的「综合评分」不可证伪，我们要做的是检验，不是打分。

## 一、它回答什么

选一只股票，回答三个能被核对的问题：

1. **它在我们已有的指标上，历史上取过什么值？** —— 时间轴，不是评分。
2. **把一组买卖规则放到它身上，会跑成什么样？** —— 单标的回测 + 买卖点画回 K 线。
3. **那些在「通过置换检验」的条件，放到这一只票上还成立吗？** —— 这是别人没有的部分，
   也是这个功能存在的理由。全市场挖出来的条件，在单只票上往往根本不成立；检验台就是
   把这件事当面说清楚的地方。

## 二、四条硬口径（改代码前先读）

1. **不做综合评分、不排名、不打标签。** 评分的权重谁定的？凭什么？抄别人的口径等于把
   可信度押在别人的定义上。只展示可核对的原始读数：指标值、自身历史分位、命中后的收益
   差、置换 p 值。
2. **分位是「自身历史分位」，不是「全市场分位」。** 单只票只跟自己的历史比才有意义，界
   面上必须写明口径，不许让人误读成横截面排名。
3. **单标的条件检验 = 差值 + 块置换 p 值。** 证据是「命中组前瞻收益均值 − 全样本均值」
   （百分点），**不是比值**（收益能取负，两负数相除会读成"差三倍"）。显著性用连续日期
   块置换（复用 `swing_research.scoring.block_permutations`，与因子检验同口径），一次检
   验多个条件时走 `benjamini_hochberg`。
4. **样本不足和不显著都要直说。** 命中样本 < `MIN_HIT_SAMPLES`(20) 标「样本不足」；p 值
   下界 `1/(1+置换次数)`；不显著就写「和随机挑几天没有区别」，不写「有效」「更有效」。

## 三、后端结构 `selector_app/instrument_lab/`

| 文件 | 职责 |
| :--- | :--- |
| `models.py` | 常量 + 数据类（`ValueTimeline` / `ConditionCheck` / `LibraryCondition` / `WorkbenchReport`） |
| `values.py` | 取一只票的指标序列 + 自身历史分位（复用 `BacktestService.prepare_for_ref` 的 `feature_values`，走指标缓存） |
| `conditions.py` | 条件库（从 `condition_research` 历史里抽 `significant=true` 的候选）+ 单标的求值 + 前瞻收益 + 块置换 + BH |
| `service.py` | 三个入口：`values()` / `condition_library()` / `check_conditions()` / `backtest()` |

### 关键实现选择

- **取值与求值都走 `BacktestService.prepare_for_ref`**：它已经有 `feature_values`（日期 ×
  指标值 DataFrame）和 `formula_results`，而且吃指标缓存，不另起一套计算路径。为了只取
  值不回测，构造 `BacktestConfig(always_in_market=True, close_at_end=True)` 且不带规则，
  这样 `prepare` 的两条「规则不能为空」校验都能过。
- **前瞻收益**与因子检验同口径：`close[t+h] / close[t] - 1`，当日收盘 → h 日后收盘。
- **置换只搬动标签**：统计量 `mean(hit) − mean(all)`，`mean(all)` 在置换下不变，所以只需
  重排 `fwd` 再取命中位置的均值。块置换保留收益自相关；单侧 p（按观测值方向计数）。
- **条件库去重**：按 `side + left_value + operator + right_value/right_constant` 去重，保留
  lift 最高的一条，上限 `MAX_LIBRARY_CONDITIONS`(60)。

## 四、接口 `GET/POST /api/v1/instrument-lab/`

| 端点 | 说明 |
| :--- | :--- |
| `POST /values` | 指标取值序列 + 末值 + 自身历史分位 |
| `GET /conditions/library` | 历史里通过置换检验的条件（供勾选） |
| `POST /conditions/check` | 在这只票上检验勾选的条件 |
| `POST /backtest` | 单标的回测（直接吃既有的 `BacktestRequest`） |

行情 K 线不新造接口，复用 `/api/v1/market-data/local/{market}/{code}/bars`。

## 五、前端 `web-ui/src/pages/InstrumentLabPage.vue`

四个分区：行情（复用 `MarketCandlestickChart`）/ 指标时间轴 / 买卖规则回测（复用
`RuleGroupEditor`）/ 条件检验。展示逻辑抽到 `utils/instrumentLab.ts` 里单测，页面只留胶水
（`#16` 已经踩过 v8 行覆盖映射失真的坑）。

`MarketCandlestickChart.vue` 加**可选** `markers` prop（日期 + 买卖方向），用来把回测成交
点画回 K 线；老页面不传就不画，行为不变。

## 六、不做

- 综合评分 / 标签 / 排名（口径 1）。
- 多标的对比：`#16` 已有归一化叠绘，重复且没有不可替代的口径。
- 「自动挑出这只票最优的条件」：一次检验 N 个条件已经要 BH 校正，再自动挑就是挑选偏差，
  违背可证伪原则。
- 复活独立的单股回测页：它的价值在「买卖点画回 K 线」，放在检验台里就够了。
