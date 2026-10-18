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
`read_research_settings`）先试一次**只读打开**，成功就用它，失败**立刻**退回原来
的读写打开（`StrategyPoolStore._read_connection`），写路径不变。关键数字是
`_CONNECT_RETRY_DELAYS = (0.1, 0.25, 0.5, 1.0, 2.0)`：一次打不开就占住请求线程
约 3.85 秒才抛错，而 UI 每秒轮询多个池接口，线程池被这类等待填满之后整站接口
一起超时——这才是「页面卡死」的直接原因。

**只读打开必须带回退，别照搬「只读更安全」。** 报错里的
`Conflicting lock is held in PID 0` 指的是**同进程**冲突：研究 worker 一个进程里
既写池表又读池表，4 条线程并发时一条线程持有读写连接，另一条线程的只读打开就被
自己进程拒绝。第一版没有回退，结果 worker 侧一次任务级读抛错、整条全市场任务
4 分钟就失败（`worker_failed`）。后端没有这种同进程写事务，才稳定吃到只读的收益。
回归测试两条：只读快路径命中且不再要读写连接；只读被打回时能退回读写路径并读到
同样的数据。

## 生产实测（2026-10-16 13:0x，mode=full，risk_trials=3000，4 并发）

同一轮全市场任务里的连续 14 只（新 build，撤回连接复用之后）：

- 单股 total：62.9 / 113.7 / 136.8 / 162.9 / 169.2 / 182.3 / 182.7 / 184.6 /
  209.2 / 211.5 / 213.1 / 240.8 / 243.2 / 296.5，**均值 186s**；同日改前（旧
  build、同一任务）连续 16 只的均值是 **626s** → 单股计算量 **3.4x**，与 A/B
  的 3.6x 相称。
- 池页读接口在研究进行中 0.04-0.72s，backend 60 分钟内 `被其它进程占用` 与
  503 **各 0 次**（改前同一时段是 12s 超时 + 3 次 503）→ 只读打开的改动生效。
- 但墙钟速度只到约 1.5-1.9x：5 分钟窗口内新研究 4 只 → **75s/只墙钟**（改前
  139s/只），按剩余约 4200 只推算 **88-117 小时**（早期 14 只的均值给 91s/只，
  后一段 4 只的均值给 73s/只，所以给区间不给单点）。4 并发 × 186s
  理应 46s/只，实测 73-91s/只——瓶颈已经移到并发侧的固定开销：每股约 11 个
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
「生产实测」修正为 88-117 小时：单股计算确实降到 1/3.4，但墙钟只到 1/1.5-1.9，
并发侧的固定开销（spawn 池、快照、GIL）接过了接力棒。

## 调参指引

| 变量 | 建议值 | 说明 |
|---|---|---|
| `AQUEST_DUCKDB_THREADS` | 1 | 只注入策略 worker；研究查询都是单标的小查询 |
| `OPENBLAS_NUM_THREADS` 等 | 1 | worker 进程内已默认 |
| `AQUEST_STRATEGY_POOL_MAX_WORKERS` | 4（实测最优形状未定） | 评估进程总预算 = `min(核数, DISCOVERY_WORKERS)`，加股票数不加进程，只加 GIL 争用；见「第三轮」 |
| `AQUEST_DISCOVERY_WORKERS` | 8 才可能给到每只 2 进程 | 与上一行共同决定「几只 × 每只几进程」的形状；当前 4/4 是每只 1 进程 |

## 后续方向（未立项）
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

## 第三轮（2026-10-16）：并发形状与连接重试预算

生产跑起来后单股只降到 1/3.4，墙钟却只到 1/1.8。用 `docker exec` 直接数进程
定位（**别信 `/proc/*/cmdline`**：spawn 出来的子进程 cmdline 只有
`python -c from multiprocessing…`，按命令行找 `run.py` 会数出 0 个）：
一只股票的研究在 worker 里 84 秒内产生 8 个进程池、68 次进程创建，全部
**max_workers=1**。

原因是评估进程预算取 `min(可见核数, AQUEST_DISCOVERY_WORKERS)`，生产两个值都是
4：4 只股票并发 × 每只 1 个评估进程——**加股票数不会加计算进程**，只会让更多
父线程在同一个 GIL 上排队、并多开快照。所以「开到 6-8 就 ≈3 天」这条推算站不住：
总进程数被核数钉死，加并发只是换一种争用形状。当前 4 并发实测 75-100s/只墙钟，
按剩余约 4200 只是 **88-117 小时**（改前 139s/只 ≈ 157h）。

唯一还没试过的形状是「少几只、每只多几个进程」（例如 `POOL_MAX_WORKERS=4` 配
`DISCOVERY_WORKERS=8`，让预算分给每只 2 个进程），但它要改 `.env` 并重启，代价
是中断在跑的轮次；先在当前轮跑完拿到稳定均值，再决定要不要试。

实施：给库连接的重试预算加环境变量 `AQUEST_DUCKDB_CONNECT_RETRY_SECONDS`，
**只注入 strategy-worker**（compose 默认 25 秒，阶梯从 0.1 秒起翻倍、单步封顶
5 秒）。理由是两个调用方的代价相反：backend 的读打不开应当快速失败（页面下一秒
还会再轮询，占着请求线程反而把整站拖死），而研究里一次打不开就废掉整只标的、
白跑几分钟——生产实测：新开的一轮 23 分钟里 4 只整只失败（同期完成 24 只，约六分之一；失败
条目会在后续轮次重研究，不是数据丢失，但等于多跑一遍）。不设变量的进程行为完全
不变，回归测试是 `tests/unit/test_store_connect_retry.py`。

### 待验证的并发形状

进程总预算只有 8，可以选「4 只 × 2 进程」或「8 只 × 1 进程」：

- 后者内存按 `512 MiB + bars × (值数 × 32 + 信号数 × 16 + 2048)` 估
  （`estimate_ticker_working_set_bytes`），长历史标的会到 ~2 GiB/只，8 只就可能
  触顶，并且预算 8 是「空闲时」的口径，相乘之后不一定给得到 2；
- 前者要改 `.env`（现在 4/4）并重启，代价是中断在跑的轮次。

先让当前这轮（预算修复 + 4 并发）跑完拿实测；只有明显偏离 ~70h 才去调 4 只 ×
2 进程。

## 第四轮（2026-10-18）：一次写库失败判死三小时任务

### 现象

10:29:36 发起的 `mode=full` 增量更新跑到 13:26:43 停住，任务原因只有一个名字：
`策略 worker API 响应超时或暂不可用`。24 小时内 worker 侧这类 transient 失败 221 次、
「领取任务失败」184 次；同一段时间里浏览器的 `GET /jobs/{id}` 每 2 秒都拿到 200。
**只有要写库的请求饿死**，读请求照常——所以不是「后端挂了」，是后端答不上写。

### 定位

1. 逐标的结果落库走 `POST /internal/strategy-worker/jobs/{id}/pool-entry`，客户端预算
   是 5 次 × 5s + 退避（0.1/0.25/0.5/1.0）≈ 27 秒；耗尽后 `TransientWorkerApiError`
   从 `strategy_pool/service.py:903` 的 `entry_writer(record)` 一路冒到 `run_once`，
   **整任务判死**。研究节拍 75–100 秒/只，一次 5 分钟的后端不可用足够撞上去。
2. 后端为什么答不上写：traceback 里的报错文本是
   `Can't open a connection to same database file with a different configuration than
   existing connections`——这是**同一进程内连接配置不一致**，不是只读/读写模式冲突
   （我在 32.5 MB 那条下过结论时把它归给了 `f004085` 的只读优先，那是错的）。真实配置
   按容器实测：backend `memory_limit=5GiB`（从 cgroup 自动推导）、`threads=None`；worker
   同一份 memory_limit 再加 `threads=1`。`_open()` 带着这份 config，而 `_schema_version_on_disk`
   与 `maintenance.py` 里 4 处是裸 `duckdb.connect(...)`：同进程同文件，后一把一律被拒。
   最小复现：`duckdb.connect(p, read_only=True, config={"threads": 1})` 之后再
   `duckdb.connect(p, read_only=True)` 就抛这句——两把都是只读也照样拒，跟锁没关系。
   后端又没配 `AQUEST_DUCKDB_CONNECT_RETRY_SECONDS`（25s 那档当时只给了 strategy-worker），
   阶梯 3.85 秒走完就报成「行情数据库当前被其它进程占用」。并发 3 次列表请求实测 4.07 秒
   且必现 500。
3. 把后端压成这样的正是列表本身：`GET /entries` 一次回 **32.5 MB**（3350 行 × 9.7 KB），
   nginx 明确记了 `an upstream response is buffered to a temporary file`。字段占比
   `automatic_candidates` 35.3% / `tracking` 32.9% / `active_candidate` 25.6%，逐字段
   核对前端后确认这 94% 页面一行都没读（列表只用 `metrics`、`quality.badges`、
   `candidate_id`、有没有买卖规则、以及 `tracking.summary.holding` 的风控三档线）。

### 改动与数字

- 列表按白名单投影：每行 9.7 KB → **2.9 KB**（3.6 倍）。买卖规则只留「有没有」，
  `config` / 规则树 / `split_metadata` / 跟踪快照里那整套 `candidate` 一律不回，
  完整内容仍由 `/detail` 与 `/versions/{id}/curve|trades` 提供。
- gzip 放在 **nginx** 而不是 backend 中间件：同一个 uvicorn 进程要随时应答 worker 的
  落库，把压缩 CPU 花在它身上等于继续饿它。上线体积 32.5 MB → **约 1.2 MB**（≈29 倍）。
  `gzip_proxied any` 是必须的——反代回来的响应默认不压。SSE 的 `text/event-stream`
  不在 `gzip_types` 里，进度流不受影响。
- 逐标的落库改成 `DeferredPoolEntryWriter`：只对 transient 失败让步，结果按写入顺序
  攒在内存里继续研究，恢复后顺带补齐，任务收尾再兜一轮；仍送不出去的条数如实写进
  结果（`unpersisted_entries`）。连续攒到 256 条才认输——那些结果只在本进程内存里，
  默默攒着等于丢掉几千只标的的产出。
- 打开行情库只剩一个入口 `open_database(path, read_only=...)`，配置只有一份来源；
  `store_base` 的 schema 探测与 `maintenance` 的 4 处裸 connect 全部改走它。回归测试用
  `AQUEST_DUCKDB_THREADS=1` 把配置差异做出来，再让 schema 探测与 `read_session` 撞在
  同一进程里——改之前那条测试是红的。
- 页面「卡到浏览器弹页面无响应」还有一处纯算法账：状态档排序的比较器每次线性扫整份
  当天状态表，3350 行 × 3350 条实测一次 **1.4 秒**，而列表每刷新一次就重排一遍、特别关注
  面板再排一遍。改成先建一次索引并记住每个标的算出的状态之后 **12 毫秒**，排名逐行一致
  （含同一证券代码出现多行时取第一条的旧口径）。标的少的时候完全看不出来，全市场跑到
  几千只才暴露。
- 空闲领取退避：0.5s → 1s → 2s → 3s 封顶，领到任务立刻回到 0.5s。一次领取要做两把库
  连接（清理失联任务 + 认领），空闲按 0.5 秒轮就是每秒 4 把，全市场页的读和 worker 的
  落库都跟它在同一个文件上排队；代价只是新任务起跑最晚晚 3 秒。

### 部署后复测（2026-10-18 23:5x，只带列表投影 + gzip 的那次重建）

- `GET /entries`：直连 8123 回 **8.88 MB**（原 32.5 MB），经 nginx 压缩上线 **1.21 MB**，
  ttfb 0.8–1.5 秒（原 1.7–4.1 秒且并发必现 500）。
- 重启后 90 分钟内后端 `different configuration` 计数 0——但要说清这次采样并没有带上
  连接配置的修复，而且窗口里没有正在跑的研究任务，所以它只说明「空闲时不再撞」，不能
  当成对配置修复的验证；验证要等带上 `open_database` 的镜像 + 一个正在跑的 full 任务。
  同窗口剩下的失败集中在启动压缩期（`POST /jobs/claim` 拿 503，worker 重试即可）。

### 没做的

- 状态表与行情还在同一个 DuckDB 文件里（选项 5）。`strategy_jobs` 的认领/心跳/进度是
  常态写库最高频的一张表，却和几千标的的行情读挤同一个独占文件。拆开的三处必须先设计：
  整库压缩与备份只认单文件（`maintenance.py` 的 `{stem}.bak-*` 与 `COPY FROM DATABASE`
  校验含 `data_meta.schema_version`）、数据导出只走 `ASSET_TABLES` 一条连接、以及
  `tracking_store.py:190` 在写跟踪快照的同一条连接里读 `daily_bars`——跨文件就不是一个
  事务了。
- 后端要不要也吃长阶梯：现在配置的 bug 修了，剩下的只是真·跨进程占用；给页面读加 25 秒
  等待等于把「快速报错」换成「长时间白屏」，暂不改。
- 跟踪任务的 writer 仍一击致命：它的返回值有语义（`ack.saved` 用来计数跳过），攒着不发
  就会把账算错，要单独设计。

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
