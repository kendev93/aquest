# 研究管线性能档案（2026-10）

全市场策略池与个股研究的性能优化记录：瓶颈定位过程、实施的改动、生产环境
实测数据与后续方向。数据均可复现，基准方法见文末；后续再做性能工作时应
先读本文，避免重复定位或推翻已验证的结论。

## 起点

全市场约 4161 只可研究标的，单股研究（3000 trials × ≤10 组规则）约 8 分钟，
串行覆盖周期约 23 天。Numba 内核已覆盖寻优的模拟热路径（指标汇总形态，
`AQUEST_FAST_SIM` 开启），**模拟本身不是瓶颈**——时间消耗在并发默认全关、
进程内嵌线程池、逐候选全帧预处理与单股路径的进度上报上。

测量环境（单机 Docker 部署）：宿主 8 逻辑核（6 性能核 + 2 能效核）、Docker
VM 可见 8 核、内存 11.67 GiB；行情库 1.1 GB、5224 只、日线 2015-01-05 至
2026-10-09。基准股 SH601939（2857 根日线），`risk_trials=3000`、10 组规则、
收敛后实际 16519 个 coarse trial（所有组都在 min_trials=1500 提前停止，
3000 预算的一半从未用到）。

## 瓶颈定位

1. **隐形线程池**：DuckDB 连接默认按可见核数起内部线程池（`_open` 只配了
   `memory_limit`），BLAS/OpenMP 同理。实测单 worker 基线 CPU 峰值 358%
   （3.6 核）——多开股票并发时线程池相乘导致过度订阅，越开越卡，因此此前
   只敢用并发 1。
2. **单股路径的进度上报无节流**：`IndividualStockResearchService._report`
   每次调用都会位置参数式触发 `progress_callback(bounded, 100)`；全市场任务
   的 `_TickerProgress` 走池协调器有 1.5s 节流，但**单股重跑（mode=single）
   没有协调器**，每个 trial 直接一次 HTTP + 后端 DuckDB 写（10 条/秒 ≈ trial
   速率）。实测 worker CPU 只有 25-38%（在等）、backend 烧 62-90%（在写），
   coarse 阶段纯开销 1259s/股（每 trial ~77ms），把寻优整体拖慢约 4 倍。
3. **逐候选全帧预处理**：每次 `run_stock` 都重做双重深拷贝、复权、全帧
   sha256 指纹、ATR rolling、逐 bar 日期换算——同股 450 次规则对评估里这些
   只取决于 (标的, 窗口)，与规则无关。
4. **明细路径绕开内核**：`run_stock` 默认 `include_details=True`，规则对
   初筛的 450 次回测全走纯 Python 状态机（内核只接 metrics-only 形态）。
5. **spawn 开销（次要）**：每股约 11 个 spawn 进程池（规则对验证 1 + 每组
   规则寻优 1），微基准实测 ~1.1s/次，合计 ~12s/股（约 2%），全市场约 14 小时。

## 实施的改动

| 提交 | 内容 |
|---|---|
| `6ec2ad7` | P0：`AQUEST_DUCKDB_THREADS` 线程闸（`store_base._open`，键内嵌线程整进程一致）+ worker 进程 BLAS/OMP 上限 + 逐股 `stage_durations_seconds` 落日志 |
| `ee2d1d3` | 节流修复：worker `_dispatch.progress_callback` 按 1s 时间节流，单股路径不再逐 trial HTTP；全市场路径（已有协调器节流）不受影响 |
| `7cd9fa5` | P1：`run_stock(metrics_only=True)` 走 numba 内核 + `_build_search_summary`，返回满足 `_report_metrics` 读取面的轻量报告；规则对验证、串行回退、候选指标三处调用点切换，明细消费方（benchmark/fitness）保持全明细 |
| `0206671` | P2：`_build_context` 拆为规则无关底座（帧预处理/复权/指纹/窗口/ATR/日期，按 `(标的, 窗口, ATR 周期, 公司行动 repr)` 缓存，LRU=4）+ 规则尾部（每候选）；缓存键含公司行动 repr，「每次运行看到最新除权事件」契约原样保留；批量路径显式传帧不进缓存 |

正确性保障：metrics_only 与全明细差分对拍（performance 逐键、成交序列、
指纹，并显式断言内核命中防静默回退）；底座缓存命中/双窗口/淘汰/批量绕过
四类测试；公司行动刷新契约测试原样通过；全量 CI（pytest 1701 + Ruff +
mypy strict）全绿。

## 实测数据

### 线程闸 A/B（同股同 seed，3000 trials，单进程基准）

| | 基线（全默认线程） | 上闸（threads=1 + BLAS=1） |
|---|---|---|
| SH601939 | 575s | 524s（-8.8%） |
| SZ300139 | 605s | 606s（持平） |
| worker CPU 峰值 | 358%（3.6 核） | 103%（1 核） |

结论：线程闸零代价（方差内略快），把「隐形并行」压掉后真实并发才有余量。
**先压隐形并行、再放真实并发，顺序不能反。**

### 单股生产对照（SH601939，mode=single，同 10 组规则同 16519 trials）

| 阶段 | 原始 | 节流+P1 | P2 | 累计 |
|---|---|---|---|---|
| rule_pair_validation | 134.3s | 36.6s | 23.9s | **-82%** |
| risk_search | 1864.9s | 673.1s | 621.4s | -67% |
| **total** | **2016.9s** | **733.2s** | **666.1s** | **-67%** |

逐组分解（10 组 coarse，16519 trials）定位了 risk_search 的构成：

- 修复前 coarse 阶段：墙钟 1649s，其中**真实评估仅 390s、纯开销 1259s**
  （每 trial ~77ms 的 HTTP + DuckDB 写税）；
- 修复后 coarse 阶段：墙钟 582s，真实评估 500s，开销已压到 **82s**
  （每 trial ~5ms）。

单股重跑的用户体感：约 34 分钟 → 约 11 分钟。

### spawn 开销

进程启动 + import（pandas/numba/aquest）≈ 1.0-1.2s，DuckDB 打开可忽略。
每股 ~11 池 ≈ 12s/股（~2%），全市场 ~14 小时——**不值得单独立项**，仅在
并发放开后作为 spawn 风暴的缓解手段考虑（跨股票常驻进程池，P3）。

### 阶段占比（P2 后，单股 666s）

- risk_search **621s（93%）**：真实评估 ~500s（每 trial ~30ms 的快照评估
  路径）+ 开销 ~82s + medium/full 精筛；
- rule_pair_validation 23.9s（3.6%）；
- final_replays 15.5s、mine_conditions 4.7s、prepare 0.7s。

## 第二轮（2026-10-16）：逐候选装配开销

P1+P2 之后 risk_search 仍占单股 90%+，但每候选 ~30ms 的构成一直没拆开。
这轮把「后续方向 2」做了：cProfile 挂在**同进程内联的评估路径**上（生产把候选
交给 spawn 出来的子进程，父进程 profile 只看得见 `select.poll`，需要把
`_evaluation_executor` 换成同步 `map` 才能测到真实工作）。

定位结果（基准股 SH601939，600 trial × ≤2 组规则，profile 放大后仍看得出比例）：

| 逐项 | 实测 | 结论 |
|---|---|---|
| 逐候选重查公司行动 | `_duckdb.connect` **2798 次 / 36.0s**（12.9ms/候选），加上每次 `read_session` 退出关连接 | 大头。共享快照把同一批 contexts 对象回给一次寻优的所有候选，而 `_run_prepared_contexts` 每候选都清缓存重查——等于每候选开一条只读库连接 |
| 逐候选净值/成交装配 | `_build_result` cum 11.1s，其中检查点深拷 `equity=tuple(dict(r) for r in equity)` 4.3M+4.6M 次调用 ~5.5s | risk_only 传的 `checkpoint_callback=None`（多保真度对 risk_only 关闭，fidelity 恒为 `search`），拷完即丢 |
| 明细路径日期文本 | `report._date_text` **182,841 次 / cum 19.5s**（每次两趟 pandas） | 只影响全明细路径（最终重放、跟踪），但比例可观；内核侧早就改成了批量切片 |
| 规则掩码全历史求值 | `contexts_for_config` 未进前列 | **不成立**：risk_only 的 contexts 按 (窗口, 规则, atr_period) 缓存复用，掩码不是逐候选求值 |

实施的改动：

1. 公司行动按**批次**刷新：同一批 contexts 只查一次，换批立刻重查（「每次运行看到
   最新除权事件」的粒度本来就是一次运行，`run` / `run_stock` / `replay_account`
   三个顶层入口的刷新原样保留）。
2. ~~两个 spawn 出来的 worker（发现寻优、规则对初筛）读连接复用~~
   **已撤回**：见下面「锁语义」一节——在共享卷上长期持有只读句柄会把 backend 的
   读写打开顶死。提速全部来自第 1 条（逐候选的查询本来就消失了 2999/3000 次），
   连接复用没有额外收益，只有风险。
3. `metrics_only` 且无检查点消费者时走 lightweight 装配：净值只留 total 数组，
   不建逐 bar 明细、不建明日挂单计划、不深拷检查点；指标汇总两种形态都读。
4. `_date_text` 改为纯字符串切片（与内核 `_fast_date_text` 同一等价口径）。

### 第二轮实测（生产路径，同股同 seed 同 trial 数）

`--risk-trials 1000 --candidate-limit 3` → 6 组规则 × 1000 trial = 6000 trial，
串行评估（evaluation_workers=1），三轮全部 `stop_reason=completed`：

| | 总时长 | risk_search | rule_pair_validation | final_replays | 每 trial |
|---|---|---|---|---|---|
| 改前 v0.1.3 | 246.7s | 206.1s | 6.8s | 32.2s | 34.3ms |
| 改后（含读连接复用，3 轮） | 56.9-66.9s | 39.8-49.4s | 6.6-6.9s | 8.7-9.7s | 6.6-8.2ms |
| **最终形态（撤回复用后 2 轮）** | **66.1-69.2s** | **45.6-49.5s（-77%）** | 6.8-7.6s | 10.4-11.9s | **7.6-8.3ms** |

读连接复用对时间没有可测贡献（两轮落在同一噪声带内），却会把 backend 顶死
（见下一节），所以撤回；提速全部来自「公司行动按批次刷新」+ lightweight 装配
+ 日期文本。初筛阶段本就不是逐候选开连接（每个规则对只跑 train/validation
两次回测），所以没跟着降——它在这条口径里只占 11%。

入选结果逐位一致：`results_by_return` / `results_by_sharpe` 各 10 条，
四轮（改前 1 + 改后 3）candidate_hash 序列相同，annual_return / sharpe /
max_drawdown / total_trades / sortino / turnover / executed_buy_orders 的 float
表示完全相同（最优一条 0.0316894473244389 / 0.856606282035552 /
0.05887209976283407 / 5 笔）。四轮之间唯一差异是
`research_search.search_metadata` 里的耗时字段——**这条管线在候选选择上是可复现的**，
以后做 A/B 只需要把摘要口径定到这两列上。

守住口径的测试：`test_lightweight_assembly_keeps_summary_metrics_identical`
（8 场景 × 排名/通用两形态，lightweight 与原装配的汇总逐键相等）、
`test_prepared_runs_refresh_actions_once_per_context_batch`（同批不重查、换批必重查）、
`test_date_text_matches_the_pandas_formatting_it_replaces`。

### 顺带查实：共享卷上的 DuckDB 锁语义（2026-10-16 事故）

现象：worker 与 backend 同时重启后发起全市场研究，页面冻结在「挖掘买卖条件」
（15/15 候选）不动；backend 的池页读接口 12s 超时、给 worker 的 claim 返回 503；
两个容器 load 只有 0.09——**没有任何一方在算，全在等锁**。

实测证据：

- `/proc/locks`：worker 容器里主进程 + 4 个挖掘子进程各持有
  `market.duckdb`、`market-indicator-cache.sqlite`(+`-shm`) 的
  `POSIX ADVISORY READ` 锁。
- 锁语义实验：一个进程持有只读句柄时，另一进程 `duckdb.connect(path)`（读写）
  直接 `IOException: Could not set lock on file … Conflicting lock is held`。
  只读-只读可以并存，**读写必须独占**。
- `docker compose stop strategy-worker` 后 5 秒内 backend 池页接口 0.06-0.12s 正常返回。

结论与影响面：backend 的池表/任务表读写走的是 `_connect()`（读写模式），
所以在研究期间只能靠研究进程「查询之间的空隙」挤进去。这解释了近期反复出现的
一类现象——「卡在排队等待策略 worker」「策略 / 风险标记报 `_duckdb.IOException`」
以及本次冻结。任何让研究进程**长时间持有连接**的改动（读连接复用、常驻进程池）
都会把它从「偶发」放大成「常驻」，所以这类优化必须与 backend 读写路径的改造
一起做（见后续方向 2、3）。

已实施的对症改动：池页的四个读（`list_entries` / `get` / `read_focus` /
`read_research_settings`）改走只读打开（`StrategyPoolStore._read_connection`，
与任务表读路径同一做法），写路径原样保留。关键数字是
`_CONNECT_RETRY_DELAYS = (0.1, 0.25, 0.5, 1.0, 2.0)`：一次打不开就占住请求线程
约 3.85 秒才抛错，而 UI 每秒轮询多个池接口，线程池被这类等待填满之后整站接口
一起超时——这才是「页面卡死」的直接原因，只读打开之间不冲突，读因此不再排队。
回归测试 `test_pool_page_reads_open_the_database_read_only` 钉住这个契约
（把四个读改回读写打开就会失败）。

## 生产实测（2026-10-16 13:0x，mode=full，risk_trials=3000，4 并发）

同一轮全市场任务里的连续 14 只（新 build，撤回连接复用之后）：

- 单股 total：62.9 / 113.7 / 136.8 / 162.9 / 169.2 / 182.3 / 182.7 / 184.6 /
  209.2 / 211.5 / 213.1 / 240.8 / 243.2 / 296.5，**均值 186s**；同日改前（旧
  build、同一任务）连续 16 只的均值是 **626s** → 单股计算量 **3.4x**，与 A/B
  的 3.6x 相称。
- 池页读接口在研究进行中 0.04-0.72s，backend 60 分钟内 `被其它进程占用` 与
  503 **各 0 次**（改前同一时段是 12s 超时 + 3 次 503）→ 只读打开的改动生效。
- 但墙钟速度只到约 1.8x：5 分钟窗口内新研究 4 只 → **75s/只墙钟**，按剩余
  ~4200 只推算约 **85-90 小时**（改前 UI 给的是 157 小时）。4 并发 × 186s
  理应 46s/只，实测 75s/只——瓶颈已经移到并发侧的固定开销：每股约 11 个
  spawn 进程池（~12s，占单股 6.5%）、快照构建、以及父线程 GIL 争用
  （池线程做的装配不再是可忽略的零头）。
- 残留冲突换了方向：worker 侧 60 分钟内 3 次 `Could not set lock` 重试、
  8 次 `MarketDataStoreError`，3 只标的因此整只失败——现在是 **backend 的写
  打开**顶住了研究进程的只读打开。写路径仍是独占的，所以「状态与行情分文件」
  才是把这条冲突清零的那一步。

## 全市场推算

mode=full（全市场任务）从未受单股进度 bug 影响：用户实测均值约 8 分钟/股
（23 天覆盖口径），基准股实测 524-605s。P1+P2 都作用于 rule_pair 阶段
（-110s 上下），预计单股 **~400-420s**：

- 串行 4161 只 ≈ **18-19 天**（起点 23 天）；
- 线程闸已就位、内存峰值 ~1.2 GiB/股，`AQUEST_STRATEGY_POOL_MAX_WORKERS`
  与 `AQUEST_DISCOVERY_WORKERS` 开到 6-8 后 ≈ **3 天**（待实测）。

第二轮改动不动并发配置，直接把单候选成本从 34.3ms 压到 7.6-8.3ms。按生产实测口径
外推（4 只并发、单股 ~620s、剩余 4225 只 ETA 157h，risk_search 占 90%，整股
实测降到约 1/3.6）：**同配置同口径下 ETA 约 45h**——这条外推已被上面的
「生产实测」修正为 85-90 小时：单股计算确实降到 1/3.4，但墙钟只到 1/1.8，
并发侧的固定开销（spawn 池、快照、GIL）接过了接力棒。

## 调参指引

| 变量 | 建议值 | 说明 |
|---|---|---|
| `AQUEST_DUCKDB_THREADS` | 1 | 只注入策略 worker；研究查询都是单标的小查询 |
| `OPENBLAS_NUM_THREADS` 等 | 1 | worker 进程内已默认 |
| `AQUEST_STRATEGY_POOL_MAX_WORKERS` | 核数 × 0.7~1 | 回测是数组密集型，内存带宽先于核数成为天花板 |
| `AQUEST_DISCOVERY_WORKERS` | 与上同步放大 | 评估进程受共享预算约束（`EvaluationWorkerBudget`） |

## 后续方向（未立项）

1. **并发放开**：零开发，改环境变量即可。当前 `.env` 是 `POOL_MAX_WORKERS=4` +
   `DISCOVERY_WORKERS=4`，而共享评估进程预算取 `min(可见核数 8, DISCOVERY_WORKERS)`
   → 4 只并发每只只分到 1 个评估进程，容器实测只用 2.79/8 核、1.0/11.7 GiB。
   按文档建议开到 6-8 是下一步最便宜的一档（2026-10-16 用户决定：先不动配置，
   先把口径内的开销吃掉）。
2. **指标汇总本身**：改前每候选 `_build_search_summary` cum ~5.6ms（DataFrame +
   `PerformanceAnalyzer.compute` + `_average_holding_days`）、`_trade_fingerprint`
   ~1.5ms、`PortfolioBacktestRequest.model_validate` + `_context_config_cache_key`
   的 json.dumps 各一次。要再降就得把已验证的绩效公式改成数组口径——收益只剩
   risk_search 的几成，但要重写对拍的参照物，风险明显高于第一轮。
3. **spawn 池复用（P3）**：每股 ~11 个进程池、~12s（2%）。第一轮改动把单股压到
   1/4 后，这 12s 的**相对**占比回到 ~8%，值得重新评估；跨股票常驻池是原方案。
4. 内存是并发的天花板而不是核数：Docker VM 11.67 GiB、实测峰值 ~1.2 GiB/股。
5. ~~backend 的池表读改走只读连接~~ **已实施**（2026-10-16，见「锁语义」一节末）；
   写路径仍靠重试间隙，若仍嫌不稳就继续做下面第 6 条。
6. **可变状态与行情库分文件**：池条目、任务、跟踪、历史表都塞在 `market.duckdb`
   里，backend 要写、worker 要读，是同一类冲突的根。拆成独立的状态库后，研究进程
   永远只读行情文件，跨进程的独占冲突面归零。
7. **指标缓存不要多进程同时写**：挖掘子进程各持 SQLite 共享锁又想把结果写回缓存，
   是这次冻结的另一半。要么只让父进程持久化，要么把写入串行化并给足 busy 超时。

## 决策记录

- **P5（risk_trials 降回 1000）不采纳**（2026-10-11 用户决策）：提前退出
  机制保证多数组在 min_trials=1500 停止，3000 预算虽然一半闲置，但保持
  现状给收敛留了空间；数据已归档，未来若重新评估可直接引用。
- **P3（spawn 池复用）后置**：实测 12s/股（~2%）低于立项门槛（<10s/股则
  后置，实测处于边缘）；真正价值在并发放开后的 spawn 风暴缓解。
- **寻优/初筛 worker 的读连接复用已撤回**（2026-10-16，加了又撤）：共享卷上研究
  进程长期持有只读句柄，会把 backend 以读写模式打开池表的请求顶死（实测
  `Could not set lock … Conflicting lock is held`）；而逐候选的查询本来就已被
  「公司行动按批次刷新」消掉，复用连接不再带来收益，只增加冻结面。
- **规则掩码只评窗口不立项**（2026-10-16）：profile 里 `contexts_for_config`
  根本没进前列——risk_only 的 contexts 按 (窗口, 规则, atr_period) 缓存复用，
  掩码不是逐候选求值。原判断来自「全历史求值再切窗」这一步确实存在，但它
  每次寻优只做几遍，不做也不亏。
- **逐候选装配瘦身采纳**（2026-10-16 用户决策：在不牺牲结果最优性的前提下优化，
  改前后最好结果要一致）：做法是只删无人认领的工作（重查、深拷、明细），
  不动任何 trial 数、约束、评分或采样序列；用同股同 seed 的生产路径三轮对拍
  证明入选候选与绩效数字逐位一致。

## 基准方法

- 单股全流程：`scripts/benchmark_research_search.py`（只读库，evaluation_workers=1）；
  A/B 时缩减符号数，种子固定保证同 trial 数。
- 生产对照：`POST /api/v1/strategy-pool/jobs`（mode=single）+ worker 日志的
  `个股研究阶段耗时` 行 + 任务结果 JSON 里的 `risk_search_runs` 分组元数据。
- 轮次吞吐量：页面「策略池任务记录」逐条显示耗时与新研究/复用拆分，上方汇总
  由 `GET /api/v1/strategy-pool/jobs/stats` 按任务类型各取最近 60 条终态记录给
  平均与中位。每只新研究标的耗时 = 任务时长 ÷（`completed_symbols` −
  `skipped_symbols`）；时长从 worker 认领（`state_json.started_at`）起算，更早
  入库的行没有该字段、含排队，页面会标「含排队」，跨版本对比要分开看。
- 资源采样：`docker stats --no-stream` 每 10-15s（worker/backend 分开）；
  后端请求量用 `docker compose logs backend` 按 URL 聚合计数。
- 注意：risk_search 阶段有 ±10% 的运行间波动，跨轮对比必须同股同 seed 同
  trial 数；`nproc` 会读 `OMP_NUM_THREADS`，在已上闸的容器里显示 1，不代表
  真实核数。
- 逐候选 profile：生产的候选评估跑在 spawn 出来的子进程里，父进程 cProfile 只
  看得见 `select.poll`。要看真实装配开销就把
  `StrategyDiscoveryService._evaluation_executor` 换成同步 `map`（保留真实快照
  构建，只把进程池换成内联），并按子进程同样的方式构造 worker 服务
  （`DiscoverySnapshot(snapshot_path)` + `pool_read_connections=True`），否则
  测到的是回退路径。
- 结果一致性对拍：`scripts/benchmark_research_search.py` 的输出里，
  `results_by_return` / `results_by_sharpe` 的 candidate_hash 序列与绩效字段是
  可比口径；`risk_search_runs[].search_metadata` 含耗时字段，跨轮必然不同，
  不要整段比哈希。第二轮就是按这个口径确认四轮（改前一轮 + 改后三轮）入选结果
  逐位一致。
